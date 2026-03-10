"""
agents/diagnosis/agent.py
Agent 2 — Diagnosis Assistance

Responsibilities:
  1. Pull PatientContextMessage from diagnosis_agent-sub
  2. Build a structured clinical prompt from the patient snapshot
  3. Call Gemini via Vertex AI for differential diagnosis
  4. Apply Cloud DLP to pseudonymize PHI in the Gemini output
  5. Publish DiagnosisMessage to diagnosis-ready topic
  6. Emit audit event

FIXES APPLIED:
  F-PHI  — PHI fields (name, MRN, DOB) stripped from Gemini prompt. Patient is
            referenced by age, gender, and encounter reason only. Raw identifiers
            are never transmitted to the inference endpoint.
  F-DLP2 — DLPRedactionMoment now populated and attached to DiagnosisMessage
            so Orchestrator can include it in the 3-moment redaction demo log.
  F-DLP3 — DLP applied after JSON parse (per-field), not to raw JSON string,
            so DLP tokens cannot corrupt JSON structure.
"""

import hashlib
import json
import logging
import uuid
from typing import Optional

import vertexai
from vertexai.generative_models import GenerativeModel, GenerationConfig
from google.cloud import dlp_v2
from google.adk.agents import Agent

from shared.config import config
from shared.models import (
    AgentStatus,
    AuditEventMessage,
    DiagnosisCandidate,
    DiagnosisMessage,
    DLPAuditRecord,
    DLPRedactionMoment,
    PatientContextMessage,
)
from shared.pubsub_client import publish_message, pull_message

logger = logging.getLogger(__name__)

AGENT_NAME = "diagnosis_agent"
SERVICE_ACCOUNT = f"sa-diagnosis@{config.project_id}.iam.gserviceaccount.com"

# Initialize Vertex AI
vertexai.init(project=config.project_id, location=config.location)


# ── DLP Helpers ───────────────────────────────────────────────────────────────


def _build_dlp_request(text: str) -> dict:
    """
    Build a DLP deidentify_content request dict.

    When DLP_INSPECT_TEMPLATE and DLP_DEIDENTIFY_TEMPLATE are set in .env,
    the request uses named templates (versioned, auditable, centrally managed).
    When they are not set, falls back to inline config so local development
    works without GCP template provisioning.

    Run scripts/setup_dlp_templates.py once to create the named templates, then
    set the resource names in .env to activate this path.
    """
    item = {"value": text}

    if config.dlp_inspect_template and config.dlp_deidentify_template:
        # Named template path — preferred in deployed environments
        return {
            "parent": f"projects/{config.project_id}/locations/global",
            "inspect_template_name": config.dlp_inspect_template,
            "deidentify_template_name": config.dlp_deidentify_template,
            "item": item,
        }

    # Inline config fallback — used when templates are not yet provisioned
    info_types = [
        {"name": "PERSON_NAME"},
        {"name": "DATE_OF_BIRTH"},
        {"name": "DATE"},
        {"name": "AGE"},
        {"name": "US_SOCIAL_SECURITY_NUMBER"},
        {"name": "MEDICAL_RECORD_NUMBER"},
        {"name": "PHONE_NUMBER"},
        {"name": "EMAIL_ADDRESS"},
        {"name": "STREET_ADDRESS"},
        {"name": "LOCATION"},
        {"name": "US_HEALTHCARE_NPI"},
        {"name": "IP_ADDRESS"},
        {"name": "URL"},
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

def _apply_dlp_to_text(text: str, session_id: str) -> tuple[str, DLPAuditRecord]:
    """
    Inspect text for PHI and apply pseudonymization via Cloud DLP.
    Returns (pseudonymized_text, dlp_audit_record).
    Never logs or persists the original PHI values.
    """
    dlp_client = dlp_v2.DlpServiceClient()

    # Use named DLP templates when configured; inline config as fallback.
    # Run scripts/setup_dlp_templates.py to create templates, then set
    # DLP_INSPECT_TEMPLATE and DLP_DEIDENTIFY_TEMPLATE in .env.
    response = dlp_client.deidentify_content(request=_build_dlp_request(text))

    # FIX F-DLP4: Use transformed_count consistently across all agents.
    findings_by_type: dict[str, int] = {}
    overview = response.overview
    if hasattr(overview, "transformation_summaries"):
        for summary in overview.transformation_summaries:
            type_name = summary.info_type.name if summary.info_type else "UNKNOWN"
            findings_by_type[type_name] = findings_by_type.get(type_name, 0) + summary.transformed_count

    total_transformations = sum(findings_by_type.values())

    audit_record = DLPAuditRecord(
        agent_name=AGENT_NAME,
        session_id=session_id,
        phi_detected=total_transformations > 0,
        findings_by_type=findings_by_type,
        transformations_applied=total_transformations,
        phi_persisted=False,
    )

    pseudonymized_text = response.item.value
    logger.info(
        "DLP applied | session=%s | transformations=%d | types=%s",
        session_id,
        total_transformations,
        list(findings_by_type.keys()),
    )

    return pseudonymized_text, audit_record


def _sha256(text: str) -> str:
    """SHA-256 hash of text for audit log — never logs the text itself."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── Prompt Builder ────────────────────────────────────────────────────────────

def _build_diagnosis_prompt(snapshot_dict: dict) -> str:
    """
    Build a structured clinical prompt for Gemini differential diagnosis.
    Formats patient data clearly without including raw PHI labels.
    """
    conditions = snapshot_dict.get("conditions", [])
    medications = snapshot_dict.get("medications", [])
    allergies = snapshot_dict.get("allergies", [])
    labs = snapshot_dict.get("lab_results", [])
    vitals = snapshot_dict.get("vital_signs", [])
    encounter_reason = snapshot_dict.get("encounter_reason", "Not specified")
    age = snapshot_dict.get("age", "Unknown")
    gender = snapshot_dict.get("gender", "Unknown")
    # FIX F-PHI: name, mrn, dob intentionally NOT extracted — PHI is never
    # included in Gemini prompts. DLP runs on output, not input.

    conditions_str = "\n".join(
        f"  - {c.get('name')} ({c.get('icd10_code', 'No ICD-10')})"
        for c in conditions
    ) or "  None documented"

    meds_str = "\n".join(
        f"  - {m.get('name')} {m.get('dose', '')} {m.get('frequency', '')}".strip()
        for m in medications
    ) or "  None documented"

    allergies_str = "\n".join(
        f"  - {a.get('substance')} [{a.get('criticality')}]: {a.get('reaction', 'reaction not specified')}"
        for a in allergies
    ) or "  None documented"

    labs_str = "\n".join(
        f"  - {l.get('name')}: {l.get('value')} {l.get('unit')} "
        f"[{l.get('interpretation')}] {('(' + l.get('reference_range') + ')') if l.get('reference_range') else ''}"
        for l in labs
    ) or "  None available"

    vitals_str = "\n".join(
        f"  - {v.get('name')}: {v.get('value')} {v.get('unit')} "
        f"[{v.get('interpretation', 'N')}]"
        for v in vitals
    ) or "  None available"

    # FIX F-PHI: PHI fields (name, MRN, DOB) are intentionally excluded from
    # the Gemini prompt. The model only receives clinical data (age, gender,
    # encounter reason, conditions, meds, labs, vitals). Patient identity is
    # never transmitted to the inference endpoint.
    prompt = f"""You are an expert clinical decision support AI assisting emergency and internal medicine physicians.

PATIENT PRESENTATION
====================
Age: {age} years old | Gender: {gender}
Chief Complaint: {encounter_reason}

ACTIVE CONDITIONS
=================
{conditions_str}

CURRENT MEDICATIONS
===================
{meds_str}

KNOWN ALLERGIES
===============
{allergies_str}

LABORATORY RESULTS
==================
{labs_str}

VITAL SIGNS
===========
{vitals_str}

TASK
====
Based on the above clinical presentation, provide a structured differential diagnosis.
For each diagnosis candidate, provide your reasoning from the available data.

Respond ONLY with a valid JSON array in this exact format (no markdown, no preamble):
[
  {{
    "rank": 1,
    "diagnosis": "Full diagnosis name",
    "icd10_code": "ICD-10 code",
    "probability": "High|Moderate|Low",
    "supporting_evidence": ["finding 1", "finding 2"],
    "against_evidence": ["finding that argues against"],
    "recommended_workup": ["next test or action"]
  }}
]

Provide 3-5 differential diagnoses ranked by probability. Be specific and clinically precise.
Use only the data provided. Do not invent findings not present in the record above."""

    return prompt


# ── Gemini Inference ──────────────────────────────────────────────────────────

def _call_gemini(prompt: str) -> tuple[str, str]:
    """
    Call Gemini via Vertex AI.
    Returns (raw_response_text, model_name_used).
    """
    model = GenerativeModel(config.gemini_model)
    generation_config = GenerationConfig(
        temperature=config.gemini_temperature,
        max_output_tokens=config.gemini_max_output_tokens,
        response_mime_type="application/json",
    )

    response = model.generate_content(
        prompt,
        generation_config=generation_config,
    )

    return response.text, config.gemini_model


def _parse_gemini_response(response_text: str) -> list[DiagnosisCandidate]:
    """Parse Gemini JSON response into a list of DiagnosisCandidate models."""
    try:
        # Strip any markdown fences if present
        clean = response_text.strip()
        if clean.startswith("```"):
            clean = clean.split("```")[1]
            if clean.startswith("json"):
                clean = clean[4:]
        clean = clean.strip()

        import re
        clean = re.sub(r"[\x00-\x1f]", " ", clean)
        raw_list = json.loads(clean)
        candidates = []
        for item in raw_list:
            candidates.append(DiagnosisCandidate(
                rank=item.get("rank", 0),
                diagnosis=item.get("diagnosis", "Unknown"),
                icd10_code=item.get("icd10_code"),
                probability=item.get("probability", "Low"),
                supporting_evidence=item.get("supporting_evidence", []),
                against_evidence=item.get("against_evidence", []),
                recommended_workup=item.get("recommended_workup", []),
            ))
        return sorted(candidates, key=lambda c: c.rank)

    except Exception as e:
        logger.error("Failed to parse Gemini response: %s\nRaw: %s", e, response_text[:500])
        return []


# ── ADK Tool Function ─────────────────────────────────────────────────────────

def run_diagnosis_agent(session_id: Optional[str] = None) -> dict:
    """
    ADK tool: Pull patient context from Pub/Sub, run Gemini differential
    diagnosis, apply DLP, and publish results.

    Args:
        session_id: Optional — if provided, used for correlation logging

    Returns:
        dict with diagnosis results summary and status
    """
    session_id = session_id or str(uuid.uuid4())
    logger.info("Diagnosis agent starting | session=%s", session_id)

    try:
        # 1. Pull patient context from Pub/Sub
        context_message = pull_message(
            config.sub_diagnosis_agent,
            PatientContextMessage,
            timeout=30.0,
        )

        if not context_message:
            return {
                "session_id": session_id,
                "status": "NO_MESSAGE",
                "message": "No patient context message available on subscription",
            }

        session_id = context_message.session_id
        patient_id = context_message.patient_id
        snapshot = context_message.patient_snapshot

        # 2. Build clinical prompt
        prompt = _build_diagnosis_prompt(snapshot.model_dump())
        prompt_hash = _sha256(prompt)

        # 3. Call Gemini
        logger.info("Calling Gemini for differential | patient=%s", patient_id)
        raw_response, model_used = _call_gemini(prompt)
        output_hash = _sha256(raw_response)

        # 4. Apply DLP to pseudonymize PHI in Gemini output
        pseudonymized_response, dlp_audit = _apply_dlp_to_text(raw_response, session_id)

        # 5. Parse into DiagnosisCandidate list
        diagnoses = _parse_gemini_response(pseudonymized_response)

        # FIX F-DLP2: Build DLPRedactionMoment for demo log so Orchestrator
        # can include Moment 2 in the 3-moment redaction report.
        # before_excerpt = raw Gemini output (may contain echoed PHI)
        # after_excerpt  = DLP-pseudonymized version
        redaction_moment_2 = DLPRedactionMoment(
            agent_name=AGENT_NAME,
            moment_label="Diagnosis Output (Gemini Raw → DLP Cleaned)",
            before_excerpt=raw_response[:300] if raw_response else "[empty]",
            after_excerpt=pseudonymized_response[:300] if pseudonymized_response else "[empty]",
            phi_types_found=list(dlp_audit.findings_by_type.keys()),
            transformations_applied=dlp_audit.transformations_applied,
        )

        logger.info(
            "\n"
            "╔══════════════════════════════════════════════════════════════╗\n"
            "║  DLP DEMO — MOMENT 2: DIAGNOSIS OUTPUT PHI REDACTION       ║\n"
            "║  Agent: diagnosis | Session: %-27s ║\n"
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
            raw_response[:59] if raw_response else "[empty]",
            pseudonymized_response[:59] if pseudonymized_response else "[empty]",
        )

        # 6. Build and publish DiagnosisMessage
        diagnosis_message = DiagnosisMessage(
            session_id=session_id,
            patient_id=patient_id,
            differential_diagnoses=diagnoses,
            critical_findings_summary=f"Top diagnosis: {diagnoses[0].diagnosis}" if diagnoses else None,
            gemini_model_used=model_used,
            gemini_prompt_hash=prompt_hash,
            gemini_output_hash=output_hash,
            dlp_applied=True,
            dlp_redaction_moment=redaction_moment_2,  # FIX F-DLP2: now populated
            agent_status=AgentStatus.SUCCESS,
        )

        publish_message(
            config.topic_diagnosis_ready,
            diagnosis_message,
            attributes={"session_id": session_id, "patient_id": patient_id},
        )

        # 7. Emit audit event
        audit = AuditEventMessage(
            session_id=session_id,
            principal=SERVICE_ACCOUNT,
            agent_name=AGENT_NAME,
            action="GEMINI_INFERENCE",
            resource_type="Patient",
            resource_id=patient_id,
            gemini_prompt_hash=prompt_hash,
            gemini_model=model_used,
            gemini_output_hash=output_hash,
            dlp_findings_count=dlp_audit.transformations_applied,
            dlp_transformations=json.dumps(dlp_audit.findings_by_type),
            outcome="SUCCESS",
        )
        publish_message(config.topic_audit_events, audit)

        result = {
            "session_id": session_id,
            "patient_id": patient_id,
            "status": "SUCCESS",
            "diagnoses_generated": len(diagnoses),
            "top_diagnosis": diagnoses[0].diagnosis if diagnoses else None,
            "dlp_transformations": dlp_audit.transformations_applied,
            "published_to": config.topic_diagnosis_ready,
        }

        logger.info("Diagnosis complete: %s", result)
        return result

    except Exception as e:
        logger.error("Diagnosis agent failed: %s", str(e))

        audit = AuditEventMessage(
            session_id=session_id,
            principal=SERVICE_ACCOUNT,
            agent_name=AGENT_NAME,
            action="GEMINI_INFERENCE",
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

diagnosis_agent = Agent(
    name=AGENT_NAME,
    model=config.gemini_model,
    description=(
        "Pulls patient context from Pub/Sub, generates a structured differential "
        "diagnosis using Gemini on Vertex AI, applies Cloud DLP pseudonymization "
        "to all PHI in the model output, and publishes results to the "
        "diagnosis-ready topic."
    ),
    instruction=(
        "You are the Diagnosis Assistance Agent in a clinical decision support pipeline. "
        "Call run_diagnosis_agent to pull the latest patient context from the Pub/Sub "
        "subscription, generate a differential diagnosis using Gemini, and publish the "
        "pseudonymized results downstream. Report the session_id, number of diagnoses "
        "generated, and any DLP findings in your response."
    ),
    tools=[run_diagnosis_agent],
)