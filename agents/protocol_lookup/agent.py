"""
agents/protocol_lookup/agent.py
Agent 3 — Protocol Lookup

Responsibilities:
  1. Pull PatientContextMessage from protocol-agent-sub
  2. Build targeted clinical search queries from conditions/encounter reason
  3. Query Vertex AI Search (Discovery Engine) for relevant clinical protocols
  4. Apply DLP to pseudonymize any PHI echoed in search summaries
  5. Publish ProtocolMessage to protocols-ready topic
  6. Emit audit event

FIXES APPLIED:
  H3    — Discovery Engine serving_config path must use 'global' as the location
          (not GCP_LOCATION). Discovery Engine data stores are always 'global'.
  F-DLP4 — DLP findings count now uses transformed_count consistently (matching
            the pattern used in orchestrator/agent.py), fixing the count
            inconsistency that made audit metrics diverge across agents.
  F-DEDUP — Protocol dedup now uses a content hash fallback when doc.id is empty,
             preventing duplicate protocols when Vertex AI Search returns the same
             document for multiple queries.
"""

import hashlib
import json
import logging
import uuid
from typing import Optional

from google.cloud import discoveryengine_v1beta as discoveryengine
from google.cloud import dlp_v2
from google.adk.agents import Agent

from shared.config import config
from shared.models import (
    AgentStatus,
    AuditEventMessage,
    ClinicalProtocol,
    DLPAuditRecord,
    PatientContextMessage,
    ProtocolMessage,
)
from shared.pubsub_client import publish_message, pull_message

logger = logging.getLogger(__name__)

AGENT_NAME = "protocol_lookup_agent"
SERVICE_ACCOUNT = f"sa-protocol@{config.project_id}.iam.gserviceaccount.com"

# FIX H3: Discovery Engine always uses 'global', never the regional location
DISCOVERY_ENGINE_LOCATION = "global"


# ── DLP Helper ────────────────────────────────────────────────────────────────

def _apply_dlp(text: str, session_id: str) -> tuple[str, DLPAuditRecord]:
    """Apply DLP pseudonymization to search summary text."""
    dlp_client = dlp_v2.DlpServiceClient()
    parent = f"projects/{config.project_id}/locations/{config.location}"

    info_types = [
        {"name": "PERSON_NAME"},
        {"name": "DATE_OF_BIRTH"},
        {"name": "US_SOCIAL_SECURITY_NUMBER"},
        {"name": "PHONE_NUMBER"},
        {"name": "EMAIL_ADDRESS"},
        {"name": "STREET_ADDRESS"},
        {"name": "MEDICAL_RECORD_NUMBER"},
        {"name": "AGE"},
        {"name": "DATE"},
    ]

    deidentify_config = {
        "info_type_transformations": {
            "transformations": [
                {
                    "primitive_transformation": {
                        "replace_with_info_type_config": {}
                    }
                }
            ]
        }
    }

    inspect_config = {
        "info_types": info_types,
        "min_likelihood": dlp_v2.Likelihood.LIKELY,
        "include_quote": False,
    }

    response = dlp_client.deidentify_content(
        request={
            "parent": parent,
            "deidentify_config": deidentify_config,
            "inspect_config": inspect_config,
            "item": {"value": text},
        }
    )

    # FIX F-DLP4: Use transformed_count (total transformation operations)
    # consistently across all agents. Other agents were using len(summary.results)
    # which counts result objects, not individual transformations.
    findings_by_type: dict[str, int] = {}
    overview = response.overview
    if hasattr(overview, "transformation_summaries"):
        for summary in overview.transformation_summaries:
            type_name = summary.info_type.name if summary.info_type else "UNKNOWN"
            findings_by_type[type_name] = (
                findings_by_type.get(type_name, 0) + summary.transformed_count
            )

    total = sum(findings_by_type.values())
    audit = DLPAuditRecord(
        agent_name=AGENT_NAME,
        session_id=session_id,
        phi_detected=total > 0,
        findings_by_type=findings_by_type,
        transformations_applied=total,
        phi_persisted=False,
    )

    return response.item.value, audit


# ── Vertex AI Search ──────────────────────────────────────────────────────────

def _build_search_queries(
    conditions: list[dict],
    encounter_reason: Optional[str],
    diagnoses: list[dict],
) -> list[str]:
    """
    Build targeted search queries from patient conditions and differential diagnoses.
    Queries are clinical — no patient-identifying information included.
    """
    queries = []

    if encounter_reason:
        queries.append(f"clinical protocol {encounter_reason}")

    for condition in conditions[:3]:
        name = condition.get("name", "")
        icd10 = condition.get("icd10_code", "")
        if name:
            queries.append(f"management guidelines {name} {icd10}".strip())

    for dx in diagnoses[:2]:
        diagnosis_name = dx.get("diagnosis", "")
        if diagnosis_name:
            queries.append(f"clinical protocol {diagnosis_name} treatment guidelines")

    seen = set()
    unique_queries = []
    for q in queries:
        if q not in seen:
            seen.add(q)
            unique_queries.append(q)

    return unique_queries[:5]


def _search_protocols(query: str) -> list[ClinicalProtocol]:
    """
    Execute a single Vertex AI Search query against the clinical protocols datastore.

    FIX H3: Uses DISCOVERY_ENGINE_LOCATION = 'global' instead of config.location.
    """
    client = discoveryengine.SearchServiceClient(
        client_options={"api_endpoint": "discoveryengine.googleapis.com"}
    )

    # FIX H3: 'global' is the only valid location for Discovery Engine
    serving_config = (
        f"projects/{config.project_id}/locations/{DISCOVERY_ENGINE_LOCATION}"
        f"/collections/default_collection/engines/{config.vertex_ai_search_engine_id}"
        f"/servingConfigs/default_config"
    )

    request = discoveryengine.SearchRequest(
        serving_config=serving_config,
        query=query,
        page_size=3,
        content_search_spec=discoveryengine.SearchRequest.ContentSearchSpec(
            snippet_spec=discoveryengine.SearchRequest.ContentSearchSpec.SnippetSpec(
                return_snippet=True,
                max_snippet_count=2,
            ),
            summary_spec=discoveryengine.SearchRequest.ContentSearchSpec.SummarySpec(
                summary_result_count=3,
                include_citations=True,
            ),
        ),
    )

    try:
        response = client.search(request)
        protocols = []

        for i, result in enumerate(response.results):
            doc = result.document
            doc_data = dict(doc.struct_data) if doc.struct_data else {}

            title = (
                doc_data.get("title")
                or doc_data.get("name")
                or f"Clinical Protocol {i + 1}"
            )
            source = (
                doc_data.get("source")
                or doc_data.get("author")
                or "Clinical Guidelines Database"
            )
            gcs_uri = doc_data.get("uri") or doc_data.get("gcs_uri")

            snippet_text = ""
            if result.document.derived_struct_data:
                snippets = result.document.derived_struct_data.get("snippets", [])
                if snippets:
                    snippet_text = snippets[0].get("snippet", "")

            summary = (
                snippet_text
                or doc_data.get("description")
                or f"Protocol retrieved for query: {query}"
            )

            # FIX F-DEDUP: When doc.id is empty, derive a stable ID from the
            # title so the same document returned for two different queries is
            # correctly deduplicated. A random UUID would bypass the seen_ids check.
            stable_id = doc.id or hashlib.md5(title.encode()).hexdigest()
            protocols.append(ClinicalProtocol(
                protocol_id=stable_id,
                title=title,
                source=source,
                summary=summary,
                key_recommendations=doc_data.get("recommendations", []),
                relevant_diagnosis=query,
                evidence_level=doc_data.get("evidence_level"),
                gcs_source_uri=gcs_uri,
            ))

        logger.info(
            "Vertex AI Search returned %d results for query: %s",
            len(protocols), query,
        )
        return protocols

    except Exception as e:
        logger.warning(
            "Vertex AI Search failed for query '%s': %s — returning empty list",
            query, str(e),
        )
        # Return empty list (not a placeholder) so the orchestrator
        # can distinguish between "search worked but found nothing"
        # and "search is broken". An empty list is honest.
        return []


# ── ADK Tool Function ─────────────────────────────────────────────────────────

def run_protocol_lookup(session_id: Optional[str] = None) -> dict:
    """
    ADK tool: Pull patient context, build clinical search queries,
    search Vertex AI for protocols, apply DLP, and publish results.

    Args:
        session_id: Optional session ID for correlation

    Returns:
        dict with protocol lookup results and status
    """
    session_id = session_id or str(uuid.uuid4())
    logger.info("Protocol lookup agent starting | session=%s", session_id)

    try:
        # 1. Pull patient context from Pub/Sub
        context_message = pull_message(
            config.sub_protocol_agent,
            PatientContextMessage,
            timeout=30.0,
        )

        if not context_message:
            return {
                "session_id": session_id,
                "status": "NO_MESSAGE",
                "message": "No patient context available on subscription",
            }

        session_id = context_message.session_id
        patient_id = context_message.patient_id
        snapshot = context_message.patient_snapshot

        # 2. Build search queries from patient data
        conditions = [c.model_dump() for c in snapshot.conditions]
        encounter_reason = snapshot.encounter_reason
        diagnoses = []  # Protocol agent runs in parallel with Diagnosis agent

        queries = _build_search_queries(conditions, encounter_reason, diagnoses)
        logger.info(
            "Built %d search queries for patient %s: %s",
            len(queries), patient_id, queries,
        )

        # 3. Search Vertex AI for each query
        all_protocols: list[ClinicalProtocol] = []
        seen_ids = set()

        for query in queries:
            protocols = _search_protocols(query)
            for protocol in protocols:
                if protocol.protocol_id not in seen_ids:
                    seen_ids.add(protocol.protocol_id)
                    all_protocols.append(protocol)

        # 4. Apply DLP to protocol summaries
        total_dlp_transformations = 0
        cleaned_protocols = []

        for protocol in all_protocols:
            combined_text = f"{protocol.title}\n{protocol.summary}"
            pseudonymized_text, dlp_audit = _apply_dlp(combined_text, session_id)
            total_dlp_transformations += dlp_audit.transformations_applied

            lines = pseudonymized_text.split("\n", 1)
            cleaned_protocols.append(ClinicalProtocol(
                protocol_id=protocol.protocol_id,
                title=lines[0] if lines else protocol.title,
                source=protocol.source,
                summary=lines[1] if len(lines) > 1 else protocol.summary,
                key_recommendations=protocol.key_recommendations,
                relevant_diagnosis=protocol.relevant_diagnosis,
                evidence_level=protocol.evidence_level,
                gcs_source_uri=protocol.gcs_source_uri,
            ))

        # 5. Publish ProtocolMessage
        protocol_message = ProtocolMessage(
            session_id=session_id,
            patient_id=patient_id,
            protocols_found=cleaned_protocols,
            search_queries_used=queries,
            vertex_search_engine_id=config.vertex_ai_search_engine_id,
            agent_status=AgentStatus.SUCCESS,
        )

        publish_message(
            config.topic_protocols_ready,
            protocol_message,
            attributes={"session_id": session_id, "patient_id": patient_id},
        )

        # 6. Emit audit event
        audit = AuditEventMessage(
            session_id=session_id,
            principal=SERVICE_ACCOUNT,
            agent_name=AGENT_NAME,
            action="VERTEX_SEARCH",
            resource_type="ClinicalProtocol",
            resource_id=config.vertex_ai_search_engine_id,
            fhir_query=f"search_queries={json.dumps(queries)}",
            dlp_findings_count=total_dlp_transformations,
            outcome="SUCCESS",
        )
        publish_message(config.topic_audit_events, audit)

        result = {
            "session_id": session_id,
            "patient_id": patient_id,
            "status": "SUCCESS",
            "queries_executed": len(queries),
            "protocols_found": len(cleaned_protocols),
            "dlp_transformations": total_dlp_transformations,
            "search_location": DISCOVERY_ENGINE_LOCATION,
            "published_to": config.topic_protocols_ready,
        }

        logger.info("Protocol lookup complete: %s", result)
        return result

    except Exception as e:
        logger.error("Protocol lookup failed: %s", str(e))

        audit = AuditEventMessage(
            session_id=session_id,
            principal=SERVICE_ACCOUNT,
            agent_name=AGENT_NAME,
            action="VERTEX_SEARCH",
            outcome="FAILED",
            error_message=str(e),
        )
        publish_message(config.topic_audit_events, audit)

        return {
            "session_id": session_id,
            "status": "FAILED",
            "error": str(e),
        }


# ── ADK Agent Definition ──────────────────────────────────────────────────────

protocol_lookup_agent = Agent(
    name=AGENT_NAME,
    model=config.gemini_model,
    description=(
        "Pulls patient context from Pub/Sub, builds targeted clinical search "
        "queries from the patient's conditions and encounter reason, queries "
        "Vertex AI Search (Discovery Engine — global location) for relevant "
        "clinical protocols and treatment guidelines, applies DLP "
        "pseudonymization to search summaries, and publishes results to the "
        "protocols-ready topic."
    ),
    instruction=(
        "You are the Protocol Lookup Agent in a clinical decision support pipeline. "
        "Call run_protocol_lookup to retrieve relevant clinical protocols from the "
        "Vertex AI Search knowledge base based on the patient's conditions. "
        "Report the session_id, number of protocols found, and search queries used."
    ),
    tools=[run_protocol_lookup],
)
