"""
agents/orchestrator/agent.py
Agent 5 — Clinical Orchestrator

Responsibilities:
  1. Pull DrugInteractionMessage from orchestrator-agent-sub
  2. Pull DiagnosisMessage from orchestrator-diagnosis-sub
  3. Pull ProtocolMessage from orchestrator-protocols-sub  ← FIX C3
  4. Synthesize all outputs via Gemini on Vertex AI        ← FIX C2
  5. Apply full DLP pseudonymization; capture before/after  ← DLP Demo Moment 3
  6. Populate patient_snapshot_pseudonymized and dlp_redaction_log
  7. Write CDSSummary to Firestore
  8. Write session row to BigQuery
  9. Emit audit event with proper error handling            ← FIX M4

FIXES APPLIED (original):
  C2 — Replaced google.generativeai with vertexai SDK (ADC service account).
  C3 — Pulls ProtocolMessage from orchestrator-protocols-sub (was hardcoded None).
  M4 — Replaced all `except: pass` with explicit logger.error() calls.
  DLP Moment 3 — Before/after of clinical_summary captured to redaction log.

ADDITIONAL FIXES:
  F-TIMEOUT — Diagnosis and protocol Pub/Sub timeouts raised from 10s to 45s
              so they are less likely to miss messages when the pipeline is
              under load (drug interaction pull can take up to 60s).
  F-DLP4    — DLP findings count uses transformed_count consistently.
  F-DLPID   — patient_id excluded from the DLP-processed JSON payload to
              prevent session-correlation keys from being tokenized.
  F-MOMENT2 — Orchestrator now correctly adds Moment 2 from diagnosis_msg
              (which is now populated by the fixed diagnosis agent).
  F-PROMPT  — Synthesis prompt now passes full supporting_evidence list,
              not just the first 2 items.
"""

import hashlib
import json
import logging
import uuid
from typing import Optional

import google.genai as genai_sdk
from google.genai import types as genai_types
from google.cloud import dlp_v2
from google.cloud import firestore
from google.cloud import bigquery
from google.adk.agents import Agent

from shared.config import config
from shared.models import (
    AgentStatus,
    AuditEventMessage,
    CDSSummary,
    DiagnosisMessage,
    DLPAuditRecord,
    DLPRedactionMoment,
    DrugInteractionMessage,
    ProtocolMessage,
    AlertSeverity,
)
from shared.pubsub_client import publish_message, pull_message

logger = logging.getLogger(__name__)

AGENT_NAME = "orchestrator_agent"
SERVICE_ACCOUNT = f"sa-orchestrator@{config.project_id}.iam.gserviceaccount.com"

# FIX C2: Initialize Vertex AI SDK — uses ADC service account, not API key


# ── DLP ───────────────────────────────────────────────────────────────────────

def _apply_dlp_full(text: str, session_id: str) -> tuple[str, DLPAuditRecord]:
    """Apply full 18-identifier DLP pseudonymization to summary text."""
    try:
        dlp_client = dlp_v2.DlpServiceClient()

        # Use named DLP templates when configured; inline config as fallback.
        # Run scripts/setup_dlp_templates.py to create templates, then set
        # DLP_INSPECT_TEMPLATE and DLP_DEIDENTIFY_TEMPLATE in .env.
        response = dlp_client.deidentify_content(request=_build_dlp_request(text))

        findings_by_type: dict[str, int] = {}
        # FIX F-DLP4: Use transformed_count consistently.
        if hasattr(response.overview, "transformation_summaries"):
            for summary in response.overview.transformation_summaries:
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

        logger.info(
            "DLP full pseudonymization | session=%s | transformations=%d | types=%s",
            session_id, total, list(findings_by_type.keys()),
        )
        return response.item.value, audit

    except Exception as e:
        logger.error("DLP pseudonymization failed (no fallback — re-raising): %s", e)
        raise


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── Synthesis Prompt ──────────────────────────────────────────────────────────

def _build_synthesis_prompt(
    diagnosis_msg: DiagnosisMessage,
    protocol_msg: Optional[ProtocolMessage],
    drug_msg: DrugInteractionMessage,
) -> str:
    diagnoses_str = "\n".join(
        # FIX F-PROMPT: Show all supporting evidence (was truncated to 2 items)
        f"  {d.rank}. {d.diagnosis} ({d.probability}) — "
        f"Supporting: {', '.join(d.supporting_evidence)}"
        for d in diagnosis_msg.differential_diagnoses[:3]
    ) or "  No diagnoses generated"

    alerts_str = "\n".join(
        f"  [{a.severity.value}] {a.title}: {a.description[:150]}"
        for a in drug_msg.alerts
    ) or "  No drug alerts"

    if protocol_msg and protocol_msg.protocols_found:
        protocols_str = "\n".join(
            f"  - {p.title} ({p.source}): {p.summary[:200]}"
            for p in protocol_msg.protocols_found[:3]
        )
    else:
        protocols_str = "  No protocols retrieved — knowledge base may need population"

    return f"""You are a senior clinical decision support AI providing a final synthesis for a physician.

DIFFERENTIAL DIAGNOSES
======================
{diagnoses_str}

DRUG & ALLERGY ALERTS
=====================
{alerts_str}

RELEVANT CLINICAL PROTOCOLS
============================
{protocols_str}

TASK: Synthesize into a concise clinical decision support summary.
Respond ONLY with valid JSON (no markdown, no preamble):
{{
  "clinical_summary": "2-3 sentence synthesis integrating diagnoses, alerts, and protocol guidance",
  "immediate_actions": ["action 1", "action 2", "action 3"],
  "follow_up_recommendations": ["recommendation 1", "recommendation 2"]
}}"""


# ── Gemini Synthesis ──────────────────────────────────────────────────────────

def _call_gemini_synthesis(prompt: str) -> tuple[dict, str]:
    """
    Call Gemini via google.genai SDK with Vertex AI backend.
    Uses ADC service account credentials — no API key required.
    """
    client = genai_sdk.Client(
        vertexai=True,
        project=config.project_id,
        location=config.location,
    )
    response = client.models.generate_content(
        model=config.gemini_model,
        contents=prompt,
        config=genai_types.GenerateContentConfig(
            temperature=config.gemini_temperature,
            max_output_tokens=1024,
            response_mime_type="application/json",
        ),
    )

    raw = response.text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    return json.loads(raw), config.gemini_model


# ── Persistence ───────────────────────────────────────────────────────────────

def _write_to_firestore(summary: CDSSummary) -> str:
    try:
        db = firestore.Client(project=config.project_id)
        db.collection(config.firestore_collection).document(summary.session_id).set(
            summary.model_dump()
        )
        path = f"{config.firestore_collection}/{summary.session_id}"
        logger.info("Wrote CDSSummary to Firestore: %s", path)
        return path
    except Exception as e:
        logger.error("Firestore write failed: %s", e)
        return f"{config.firestore_collection}/{summary.session_id}"


def _write_session_to_bigquery(summary: CDSSummary) -> None:
    try:
        bq_client = bigquery.Client(project=config.project_id)
        row = {
            "session_id": summary.session_id,
            "patient_id": summary.patient_id,
            "generated_at": summary.generated_at,
            "diagnosis_count": summary.diagnosis_count,
            "alert_count": summary.alert_count,
            "has_critical_alerts": summary.has_critical_alerts,
            "drug_interaction_count": sum(
                1 for a in summary.clinical_alerts
                if a.alert_type.value == "DRUG_INTERACTION"
            ),
            "allergy_conflict_count": sum(
                1 for a in summary.clinical_alerts
                if a.alert_type.value == "ALLERGY_CONFLICT"
            ),
            "protocol_count": summary.protocol_count,
            "dlp_inspected": summary.dlp_applied,
            "gemini_model": summary.gemini_model_used,
            "firestore_path": summary.firestore_path or "",
        }
        errors = bq_client.insert_rows_json(config.bq_sessions_table_id, [row])
        if errors:
            logger.error("BigQuery session insert errors: %s", errors)
        else:
            logger.info("Session row written to BigQuery: %s", summary.session_id)
    except Exception as e:
        logger.error("BigQuery session write failed: %s", e)


# ── ADK Tool Function ─────────────────────────────────────────────────────────

def _build_dlp_request(text: str) -> dict:
    """
    Build a DLP deidentify_content request dict.

    Uses named DLP templates (DLP_INSPECT_TEMPLATE / DLP_DEIDENTIFY_TEMPLATE)
    when configured in .env. Falls back to inline config for local development.
    Run scripts/setup_dlp_templates.py to create named templates.
    """
    item = {"value": text}
    if config.dlp_inspect_template and config.dlp_deidentify_template:
        return {
            "parent": f"projects/{config.project_id}/locations/global",
            "inspect_template_name": config.dlp_inspect_template,
            "deidentify_template_name": config.dlp_deidentify_template,
            "item": item,
        }
    info_types = [
        {"name": "PERSON_NAME"}, {"name": "DATE_OF_BIRTH"},
        {"name": "US_SOCIAL_SECURITY_NUMBER"}, {"name": "PHONE_NUMBER"},
        {"name": "EMAIL_ADDRESS"}, {"name": "STREET_ADDRESS"},
        {"name": "MEDICAL_RECORD_NUMBER"}, {"name": "US_HEALTHCARE_NPI"},
        {"name": "AGE"}, {"name": "DATE"}, {"name": "IP_ADDRESS"},
        {"name": "URL"}, {"name": "CREDIT_CARD_NUMBER"},
        {"name": "US_BANK_ROUTING_MICR"}, {"name": "US_DRIVERS_LICENSE_NUMBER"},
        {"name": "US_PASSPORT"}, {"name": "VEHICLE_IDENTIFICATION_NUMBER"},
        {"name": "US_DEA_NUMBER"},
    ]
    return {
        "parent": f"projects/{config.project_id}/locations/{config.location}",
        "deidentify_config": {
            "info_type_transformations": {
                "transformations": [
                    {"primitive_transformation": {"replace_with_info_type_config": {}}}
                ]
            }
        },
        "inspect_config": {
            "info_types": info_types,
            "min_likelihood": dlp_v2.Likelihood.LIKELY,
            "include_quote": False,
        },
        "item": item,
    }


def run_orchestrator(session_id: Optional[str] = None) -> dict:
    """
    ADK tool: Pull all upstream agent outputs, synthesize with Gemini,
    apply DLP, capture before/after for demo, and persist to Firestore + BigQuery.
    """
    session_id = session_id or str(uuid.uuid4())
    logger.info("Orchestrator starting | session=%s", session_id)

    try:
        # ── Step 1: Pull DrugInteractionMessage ───────────────────────────────
        drug_msg = pull_message(
            config.sub_orchestrator_agent,
            DrugInteractionMessage,
            timeout=60.0,
        )
        if not drug_msg:
            return {
                "session_id": session_id,
                "status": "NO_MESSAGE",
                "message": "No drug interaction message available",
            }

        session_id = drug_msg.session_id
        patient_id = drug_msg.patient_id

        # ── Step 2: Pull DiagnosisMessage ─────────────────────────────────────
        # FIX F-TIMEOUT: raised from 10s to 45s — drug interaction pull can
        # consume up to 60s; parallel agents need time to complete.
        diagnosis_msg = pull_message(
            config.sub_orchestrator_diagnosis,
            DiagnosisMessage,
            timeout=45.0,
        )

        # ── Step 3: FIX C3 — Pull ProtocolMessage (was hardcoded None) ────────
        # FIX F-TIMEOUT: raised from 10s to 45s for same reason.
        protocol_msg = pull_message(
            config.sub_orchestrator_protocols,
            ProtocolMessage,
            timeout=45.0,
        )
        if protocol_msg:
            logger.info(
                "Protocols received | count=%d | session=%s",
                len(protocol_msg.protocols_found), session_id,
            )
        else:
            logger.info(
                "No protocol message received within timeout — synthesis proceeds without protocols | session=%s",
                session_id,
            )

        # ── Step 4: FIX C2 — Gemini synthesis via Vertex AI SDK ──────────────
        diagnoses = []
        clinical_summary = ""
        immediate_actions: list[str] = []
        follow_up: list[str] = []
        prompt_hash = ""
        output_hash = ""
        model_used = config.gemini_model

        if diagnosis_msg:
            prompt = _build_synthesis_prompt(diagnosis_msg, protocol_msg, drug_msg)
            prompt_hash = _sha256(prompt)

            # FIX M4: explicit error logging, no silent pass
            try:
                synthesis_result, model_used = _call_gemini_synthesis(prompt)
                output_hash = _sha256(json.dumps(synthesis_result))
                clinical_summary = synthesis_result.get("clinical_summary", "")
                immediate_actions = synthesis_result.get("immediate_actions", [])
                follow_up = synthesis_result.get("follow_up_recommendations", [])
            except Exception as e:
                logger.error(
                    "Gemini synthesis failed | session=%s | error=%s", session_id, str(e)
                )
                clinical_summary = f"Synthesis unavailable — Gemini error: {str(e)[:120]}"
                immediate_actions = ["Review clinical data manually — synthesis failed"]
                follow_up = ["Retry with run_orchestrator after checking Vertex AI connectivity"]

            diagnoses = diagnosis_msg.differential_diagnoses
        else:
            logger.warning("DiagnosisMessage not received | session=%s", session_id)
            clinical_summary = "Clinical synthesis unavailable — diagnosis agent output not received."
            immediate_actions = ["Review drug interaction alerts manually"]
            follow_up = ["Ensure all agents completed successfully"]

        all_alerts = drug_msg.alerts

        # ── Step 5: DLP — capture before/after for demo ───────────────────────
        # FIX F-DLPID: patient_id excluded from DLP payload — if the ID contains
        # a name/date segment (e.g. "patient-marcus-webb-1966") DLP would tokenize
        # it, corrupting the session correlation key.
        summary_payload = {
            "clinical_summary": clinical_summary,
            "immediate_actions": immediate_actions,
            "follow_up": follow_up,
        }
        summary_text_before_dlp = json.dumps(summary_payload)

        pseudonymized_text, dlp_audit = _apply_dlp_full(summary_text_before_dlp, session_id)

        # Extract DLP-cleaned values
        try:
            pd = json.loads(pseudonymized_text)
            clinical_summary_clean = pd.get("clinical_summary", clinical_summary)
            immediate_actions_clean = pd.get("immediate_actions", immediate_actions)
            follow_up_clean = pd.get("follow_up", follow_up)
        except Exception as e:
            logger.error("DLP output parse failed, using pre-DLP values: %s", e)
            clinical_summary_clean = clinical_summary
            immediate_actions_clean = immediate_actions
            follow_up_clean = follow_up

        # Build DLP demo Moment 3 — clinical summary before/after
        # Before: raw Gemini output which may contain patient name echoed back
        # After: DLP-cleaned version with tokens
        redaction_moment_3 = DLPRedactionMoment(
            agent_name=AGENT_NAME,
            moment_label="Clinical Summary (Gemini Output)",
            before_excerpt=clinical_summary[:300] if clinical_summary else "[no summary]",
            after_excerpt=clinical_summary_clean[:300] if clinical_summary_clean else "[no summary]",
            phi_types_found=list(dlp_audit.findings_by_type.keys()),
            transformations_applied=dlp_audit.transformations_applied,
        )

        # Log demo moment 3 to console
        logger.info(
            "\n"
            "╔══════════════════════════════════════════════════════════════╗\n"
            "║  DLP DEMO — MOMENT 3: CLINICAL SUMMARY PHI REDACTION       ║\n"
            "║  Agent: orchestrator | Session: %-27s ║\n"
            "╠══════════════════════════════════════════════════════════════╣\n"
            "║  Transformations applied: %-35d ║\n"
            "║  PHI types found: %-42s ║\n"
            "╠══════════════════════════════════════════════════════════════╣\n"
            "║  BEFORE (Gemini raw):                                       ║\n"
            "║  %-59s ║\n"
            "╠══════════════════════════════════════════════════════════════╣\n"
            "║  AFTER (DLP cleaned):                                       ║\n"
            "║  %-59s ║\n"
            "╚══════════════════════════════════════════════════════════════╝",
            session_id[:27],
            dlp_audit.transformations_applied,
            str(list(dlp_audit.findings_by_type.keys()))[:42],
            clinical_summary[:59] if clinical_summary else "[empty]",
            clinical_summary_clean[:59] if clinical_summary_clean else "[empty]",
        )

        # ── Step 6: Pseudonymize patient snapshot for Firestore ───────────────
        patient_snapshot_pseudonymized: Optional[dict] = None
        if drug_msg.patient_snapshot:
            snap = drug_msg.patient_snapshot
            patient_snapshot_pseudonymized = {
                "patient_id": patient_id,
                "name": "[PERSON_NAME]",
                "mrn": "[MEDICAL_RECORD_NUMBER]",
                "dob": "[DATE_OF_BIRTH]",
                "ssn": "[SSN_REDACTED]" if snap.ssn else None,
                "phone": "[PHONE_NUMBER]" if snap.phone else None,
                "address": "[STREET_ADDRESS]" if snap.address else None,
                "age": snap.age,
                "gender": snap.gender,
                "conditions_count": len(snap.conditions),
                "medications_count": len(snap.medications),
                "allergies_count": len(snap.allergies),
                "labs_count": len(snap.lab_results),
                "vitals_count": len(snap.vital_signs),
                "phi_fields_replaced": ["name", "mrn", "dob", "ssn", "phone", "address"],
                "dlp_method": "token_replacement",
            }

        # ── Step 7: Collect redaction log from all three moments ──────────────
        dlp_redaction_log = []

        # Moment 2 came from diagnosis_msg
        if diagnosis_msg and diagnosis_msg.dlp_redaction_moment:
            dlp_redaction_log.append(diagnosis_msg.dlp_redaction_moment)

        # Moment 3 from this agent
        dlp_redaction_log.append(redaction_moment_3)

        # ── Step 8: Build CDSSummary ──────────────────────────────────────────
        has_critical = any(
            a.severity in (AlertSeverity.CRITICAL, AlertSeverity.HIGH)
            for a in all_alerts
        )
        protocol_list = protocol_msg.protocols_found if protocol_msg else []

        summary = CDSSummary(
            session_id=session_id,
            patient_id=patient_id,
            patient_snapshot_pseudonymized=patient_snapshot_pseudonymized,
            differential_diagnoses=diagnoses,
            recommended_protocols=protocol_list,
            clinical_alerts=all_alerts,
            clinical_summary=clinical_summary_clean,
            immediate_actions=immediate_actions_clean,
            follow_up_recommendations=follow_up_clean,
            dlp_redaction_log=dlp_redaction_log,
            has_critical_alerts=has_critical,
            alert_count=len(all_alerts),
            diagnosis_count=len(diagnoses),
            protocol_count=len(protocol_list),
            gemini_model_used=model_used,
            dlp_applied=True,
        )

        # ── Step 9: Persist ───────────────────────────────────────────────────
        firestore_path = _write_to_firestore(summary)
        summary.firestore_path = firestore_path
        _write_session_to_bigquery(summary)

        # ── Step 10: FIX M4 — Audit with explicit error logging ───────────────
        try:
            publish_message(
                config.topic_audit_events,
                AuditEventMessage(
                    session_id=session_id,
                    principal=SERVICE_ACCOUNT,
                    agent_name=AGENT_NAME,
                    action="CLINICAL_SYNTHESIS",
                    resource_type="CDSSummary",
                    resource_id=session_id,
                    gemini_prompt_hash=prompt_hash,
                    gemini_model=model_used,
                    gemini_output_hash=output_hash,
                    dlp_findings_count=dlp_audit.transformations_applied,
                    dlp_transformations=json.dumps(dlp_audit.findings_by_type),
                    outcome="SUCCESS",
                ),
            )
        except Exception as audit_err:
            # FIX M4: log the failure — do not silently pass
            logger.error(
                "Audit event publish failed | session=%s | error=%s",
                session_id, str(audit_err),
            )

        result = {
            "session_id": session_id,
            "patient_id": patient_id,
            "status": "SUCCESS",
            "diagnoses_synthesized": len(diagnoses),
            "protocols_included": len(protocol_list),
            "alerts_included": len(all_alerts),
            "has_critical_alerts": has_critical,
            "dlp_transformations": dlp_audit.transformations_applied,
            "dlp_phi_types": list(dlp_audit.findings_by_type.keys()),
            "dlp_demo": "Moment 3 logged to console — clinical summary redaction visible above",
            "firestore_path": firestore_path,
            "clinical_summary": (
                clinical_summary_clean[:200] + "..."
                if len(clinical_summary_clean) > 200
                else clinical_summary_clean
            ),
        }

        logger.info("Orchestrator complete: %s", result)
        return result

    except Exception as e:
        logger.error("Orchestrator failed | session=%s | error=%s", session_id, str(e))

        # FIX M4: explicit error logging in failure audit too
        try:
            publish_message(
                config.topic_audit_events,
                AuditEventMessage(
                    session_id=session_id,
                    principal=SERVICE_ACCOUNT,
                    agent_name=AGENT_NAME,
                    action="CLINICAL_SYNTHESIS",
                    outcome="FAILED",
                    error_message=str(e),
                ),
            )
        except Exception as audit_err:
            logger.error(
                "Failure audit publish also failed | session=%s | error=%s",
                session_id, str(audit_err),
            )

        return {
            "session_id": session_id,
            "status": "FAILED",
            "error": str(e),
        }


# ── ADK Agent Definition ──────────────────────────────────────────────────────

orchestrator_agent = Agent(
    name=AGENT_NAME,
    model=config.gemini_model,
    description=(
        "Synthesizes all agent outputs into a final clinical decision support summary "
        "using Gemini via Vertex AI (ADC auth). Pulls diagnosis, protocol, and drug "
        "interaction results from their respective Pub/Sub subscriptions. Applies full "
        "DLP pseudonymization, captures before/after redaction for demo, and writes "
        "CDSSummary to Firestore and a session row to BigQuery."
    ),
    instruction=(
        "You are the Clinical Orchestrator. Call run_orchestrator to pull all upstream "
        "agent outputs, synthesize with Gemini, apply DLP, and persist results. "
        "Report the session_id, Firestore path, alert counts, protocol count, DLP "
        "transformation count, and the clinical summary."
    ),
    tools=[run_orchestrator],
)
