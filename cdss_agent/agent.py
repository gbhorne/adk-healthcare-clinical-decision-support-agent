"""
cdss_agent/agent.py
ADK Root Agent — Clinical Decision Support System

This is the entry point for the ADK pipeline.
The root agent orchestrates all 6 sub-agents in sequence:
  1. Patient Context Agent  — FHIR $everything
  2. Diagnosis Agent        — Gemini differential (parallel)
  3. Protocol Lookup Agent  — Vertex AI Search (parallel)
  4. Drug Interaction Agent — RxNorm + allergy check
  5. Orchestrator Agent     — Synthesis + DLP + Firestore
  6. Audit Agent            — Cloud Logging + BigQuery

FIXES APPLIED:
  F-VALIDATE — config.validate() called at import time so a missing
               GCP_PROJECT_ID fails loudly at startup, not at first API call.

Usage:
  adk run cdss_agent          # ADK web UI
  adk api_server cdss_agent   # REST API mode
"""

import logging
from google.adk.agents import Agent

from shared.config import config

from agents.patient_context.agent import (
    patient_context_agent,
    fetch_patient_context,
)
from agents.diagnosis.agent import (
    diagnosis_agent,
    run_diagnosis_agent,
)
from agents.protocol_lookup.agent import (
    protocol_lookup_agent,
    run_protocol_lookup,
)
from agents.drug_interaction.agent import (
    drug_interaction_agent,
    run_drug_interaction_check,
)
from agents.orchestrator.agent import (
    orchestrator_agent,
    run_orchestrator,
)
from agents.audit.agent import (
    audit_agent,
    process_audit_events,
    write_audit_event_direct,
)

logger = logging.getLogger(__name__)

# FIX F-VALIDATE: Validate config at import time — missing GCP_PROJECT_ID
# raises a clear ValueError instead of an obscure API error later.
config.validate()


# ── Root Agent ────────────────────────────────────────────────────────────────

root_agent = Agent(
    name="cdss_root_agent",
    model=config.gemini_model,
    description=(
        "Clinical Decision Support System — HIPAA-compliant 6-agent pipeline on GCP. "
        "Accepts a patient_id and runs a complete clinical investigation: "
        "FHIR data retrieval, differential diagnosis, protocol lookup, "
        "drug interaction checking, clinical synthesis, and audit logging. "
        "All PHI is pseudonymized via Cloud DLP before persistence."
    ),
    instruction="""You are the Clinical Decision Support System (CDSS) root agent.

When given a patient_id, run the complete clinical pipeline in this order:

STEP 1 — Patient Context
Call fetch_patient_context(patient_id) to retrieve all FHIR resources
for the patient and publish them to the pipeline. Note the session_id returned.

STEP 2 — Parallel Analysis (run both)
Call run_diagnosis_agent() to generate a differential diagnosis from the patient context.
Call run_protocol_lookup() to retrieve relevant clinical protocols.
These run in parallel — call both before proceeding.

STEP 3 — Drug Interaction Check
Call run_drug_interaction_check() to check all medications for interactions,
allergy conflicts, and contraindications.

STEP 4 — Clinical Synthesis
Call run_orchestrator() to synthesize all outputs into a final clinical summary,
apply DLP pseudonymization, and persist to Firestore and BigQuery.

STEP 5 — Audit Flush
Call process_audit_events(batch_size=20) to write all pending audit events
to Cloud Logging and BigQuery.

After all steps complete, provide the user with:
- Session ID
- Top differential diagnosis
- Number and severity of drug/allergy alerts
- Firestore path of the clinical summary
- Confirmation that audit trail is written

If any step returns status FAILED, report the error clearly and continue
with remaining steps where possible.

Always maintain a professional, clinical tone. Never display raw PHI in responses.
Refer to patients by their patient_id only in your output.""",

    tools=[
        fetch_patient_context,
        run_diagnosis_agent,
        run_protocol_lookup,
        run_drug_interaction_check,
        run_orchestrator,
        process_audit_events,
        write_audit_event_direct,
    ],

    sub_agents=[
        patient_context_agent,
        diagnosis_agent,
        protocol_lookup_agent,
        drug_interaction_agent,
        orchestrator_agent,
        audit_agent,
    ],
)