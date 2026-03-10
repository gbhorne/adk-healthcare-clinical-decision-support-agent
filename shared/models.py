"""
shared/models.py
Pydantic models for all agent inputs, outputs, and Pub/Sub message payloads.
These are the contracts between agents — every message on every topic
must conform to one of these models.

CHANGES FROM ORIGINAL:
  - CDSSummary.patient_snapshot_pseudonymized now properly typed and documented
  - CDSSummary.dlp_redaction_log added — stores before/after PHI demo data
    for the three visible DLP transformation moments in the pipeline.
    This field is populated by the Orchestrator and stored in Firestore
    so the demo can show concrete redaction examples.
"""

from __future__ import annotations
from datetime import datetime
from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field
import uuid


# ── Enums ────────────────────────────────────────────────────────────────────

class AlertSeverity(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH     = "HIGH"
    MODERATE = "MODERATE"
    LOW      = "LOW"
    INFO     = "INFO"


class AlertType(str, Enum):
    DRUG_INTERACTION     = "DRUG_INTERACTION"
    ALLERGY_CONFLICT     = "ALLERGY_CONFLICT"
    CONTRAINDICATION     = "CONTRAINDICATION"
    CRITICAL_LAB         = "CRITICAL_LAB"
    SEPSIS_SCREEN        = "SEPSIS_SCREEN"
    DOSING_ADJUSTMENT    = "DOSING_ADJUSTMENT"
    PROTOCOL_DEVIATION   = "PROTOCOL_DEVIATION"


class AgentStatus(str, Enum):
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED  = "FAILED"


# ── Shared Sub-models ─────────────────────────────────────────────────────────

class LabResult(BaseModel):
    name: str
    loinc_code: Optional[str] = None
    value: float
    unit: str
    interpretation: str                  # "H", "HH", "L", "LL", "N"
    reference_range: Optional[str] = None
    collected_at: Optional[str] = None


class VitalSign(BaseModel):
    name: str
    loinc_code: Optional[str] = None
    value: Any                           # float or dict for panels like BP
    unit: str
    interpretation: Optional[str] = None
    recorded_at: Optional[str] = None


class Medication(BaseModel):
    name: str
    rxnorm_code: Optional[str] = None
    dose: Optional[str] = None
    route: Optional[str] = None
    frequency: Optional[str] = None
    status: str = "active"


class AllergyRecord(BaseModel):
    substance: str
    rxnorm_code: Optional[str] = None
    criticality: str                     # "high", "low", "unable-to-assess"
    severity: Optional[str] = None       # "mild", "moderate", "severe"
    reaction: Optional[str] = None
    cross_reactivity_note: Optional[str] = None


class Condition(BaseModel):
    name: str
    icd10_code: Optional[str] = None
    clinical_status: str = "active"
    onset_date: Optional[str] = None


class ClinicalAlert(BaseModel):
    alert_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    alert_type: AlertType
    severity: AlertSeverity
    title: str
    description: str
    affected_medication: Optional[str] = None
    recommendation: Optional[str] = None
    evidence_basis: Optional[str] = None
    requires_immediate_action: bool = False


class DLPAuditRecord(BaseModel):
    """Metadata written to audit log after each DLP run. Never contains PHI."""
    agent_name: str
    session_id: str
    phi_detected: bool
    findings_by_type: dict[str, int] = Field(default_factory=dict)
    transformations_applied: int = 0
    phi_persisted: bool = False          # Always False — PHI never written to logs


class DLPRedactionMoment(BaseModel):
    """
    Records one before/after DLP transformation for demo purposes.
    The 'before' text is the raw excerpt containing PHI tokens.
    The 'after' text is the same excerpt after DLP pseudonymization.

    IMPORTANT: 'before' text stored here uses placeholder tokens
    (e.g. 'Marcus Webb', '1966-03-14') — it is populated from the
    FHIR snapshot fields, not from any external source. It is stored
    in Firestore (inside CDSSummary) only so the demo can show the
    redaction. It is never written to BigQuery, Cloud Logging, or
    any append-only audit store.
    """
    agent_name: str
    moment_label: str                    # e.g. "Diagnosis Prompt", "Clinical Summary"
    before_excerpt: str                  # Raw text with PHI — stored in Firestore only
    after_excerpt: str                   # DLP-pseudonymized version
    phi_types_found: list[str] = Field(default_factory=list)
    transformations_applied: int = 0


# ── Patient Context (Agent 1 Output) ──────────────────────────────────────────

class PatientSnapshot(BaseModel):
    """
    Raw patient data from FHIR $everything query.
    Contains PHI — DLP pseudonymization applied before persistence.
    """
    patient_id: str
    mrn: Optional[str] = None
    name: Optional[str] = None           # PHI
    dob: Optional[str] = None            # PHI
    gender: Optional[str] = None
    age: Optional[int] = None
    ssn: Optional[str] = None            # PHI
    phone: Optional[str] = None          # PHI
    address: Optional[str] = None        # PHI
    encounter_id: Optional[str] = None
    encounter_reason: Optional[str] = None
    conditions: list[Condition] = Field(default_factory=list)
    medications: list[Medication] = Field(default_factory=list)
    allergies: list[AllergyRecord] = Field(default_factory=list)
    lab_results: list[LabResult] = Field(default_factory=list)
    vital_signs: list[VitalSign] = Field(default_factory=list)
    fhir_query_timestamp: str = Field(
        default_factory=lambda: datetime.utcnow().isoformat()
    )


class PatientContextMessage(BaseModel):
    """Pub/Sub message published to patient-context-ready topic."""
    session_id: str
    patient_id: str
    patient_snapshot: PatientSnapshot
    agent_status: AgentStatus = AgentStatus.SUCCESS
    error_message: Optional[str] = None
    published_at: str = Field(
        default_factory=lambda: datetime.utcnow().isoformat()
    )


# ── Diagnosis (Agent 2 Output) ────────────────────────────────────────────────

class DiagnosisCandidate(BaseModel):
    rank: int
    diagnosis: str
    icd10_code: Optional[str] = None
    probability: str                     # "High", "Moderate", "Low"
    supporting_evidence: list[str] = Field(default_factory=list)
    against_evidence: list[str] = Field(default_factory=list)
    recommended_workup: list[str] = Field(default_factory=list)


class DiagnosisMessage(BaseModel):
    """Pub/Sub message published to diagnosis-ready topic."""
    session_id: str
    patient_id: str
    differential_diagnoses: list[DiagnosisCandidate] = Field(default_factory=list)
    critical_findings_summary: Optional[str] = None
    gemini_model_used: str = ""
    gemini_prompt_hash: Optional[str] = None    # SHA-256 of prompt, not prompt itself
    gemini_output_hash: Optional[str] = None    # SHA-256 of output, for audit
    dlp_applied: bool = False
    dlp_redaction_moment: Optional[DLPRedactionMoment] = None  # Before/after demo
    agent_status: AgentStatus = AgentStatus.SUCCESS
    error_message: Optional[str] = None
    published_at: str = Field(
        default_factory=lambda: datetime.utcnow().isoformat()
    )


# ── Protocol Lookup (Agent 3 Output) ──────────────────────────────────────────

class ClinicalProtocol(BaseModel):
    protocol_id: str
    title: str
    source: str                          # e.g. "ACC/AHA 2023 Guidelines"
    summary: str
    key_recommendations: list[str] = Field(default_factory=list)
    relevant_diagnosis: Optional[str] = None
    evidence_level: Optional[str] = None  # "A", "B", "C"
    gcs_source_uri: Optional[str] = None


class ProtocolMessage(BaseModel):
    """Pub/Sub message published to protocols-ready topic."""
    session_id: str
    patient_id: str
    protocols_found: list[ClinicalProtocol] = Field(default_factory=list)
    search_queries_used: list[str] = Field(default_factory=list)
    vertex_search_engine_id: str = ""
    agent_status: AgentStatus = AgentStatus.SUCCESS
    error_message: Optional[str] = None
    published_at: str = Field(
        default_factory=lambda: datetime.utcnow().isoformat()
    )


# ── Drug Interaction (Agent 4 Output) ─────────────────────────────────────────

class DrugInteractionMessage(BaseModel):
    """Pub/Sub message published to drug-interactions-ready topic."""
    session_id: str
    patient_id: str
    patient_snapshot: Optional[PatientSnapshot] = None  # Carried forward from Agent 1
    alerts: list[ClinicalAlert] = Field(default_factory=list)
    medications_checked: list[str] = Field(default_factory=list)
    allergies_checked: list[str] = Field(default_factory=list)
    has_critical_alerts: bool = False
    agent_status: AgentStatus = AgentStatus.SUCCESS
    error_message: Optional[str] = None
    published_at: str = Field(
        default_factory=lambda: datetime.utcnow().isoformat()
    )


# ── Orchestrator (Agent 5 Output) ─────────────────────────────────────────────

class CDSSummary(BaseModel):
    """
    Final clinical decision support summary.
    DLP pseudonymization applied to all PHI fields before Firestore write.
    """
    session_id: str
    patient_id: str                      # Pseudonymized before persistence
    generated_at: str = Field(
        default_factory=lambda: datetime.utcnow().isoformat()
    )

    # Patient context — PHI fields replaced with DLP tokens before storage
    patient_snapshot_pseudonymized: Optional[dict] = None

    # Clinical outputs
    differential_diagnoses: list[DiagnosisCandidate] = Field(default_factory=list)
    recommended_protocols: list[ClinicalProtocol] = Field(default_factory=list)
    clinical_alerts: list[ClinicalAlert] = Field(default_factory=list)

    # Summary narrative from Gemini (DLP-cleaned before storage)
    clinical_summary: str = ""
    immediate_actions: list[str] = Field(default_factory=list)
    follow_up_recommendations: list[str] = Field(default_factory=list)

    # DLP demonstration log — stored in Firestore only, never in BQ/Cloud Logging
    # Shows the three before/after PHI redaction moments from the pipeline run
    dlp_redaction_log: list[DLPRedactionMoment] = Field(default_factory=list)

    # Metadata
    has_critical_alerts: bool = False
    alert_count: int = 0
    diagnosis_count: int = 0
    protocol_count: int = 0
    gemini_model_used: str = ""
    dlp_applied: bool = True
    firestore_path: Optional[str] = None


# ── Audit (Agent 6) ───────────────────────────────────────────────────────────

class AuditEventMessage(BaseModel):
    """Pub/Sub message published to audit-events topic by any agent."""
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str
    timestamp: str = Field(
        default_factory=lambda: datetime.utcnow().isoformat()
    )
    principal: str                       # Service account email
    agent_name: str
    action: str                          # e.g. "FHIR_QUERY", "GEMINI_INFERENCE", "DLP_INSPECT"
    resource_type: Optional[str] = None  # e.g. "Patient", "MedicationRequest"
    resource_id: Optional[str] = None
    fhir_query: Optional[str] = None     # Query string only, no PHI values
    gemini_prompt_hash: Optional[str] = None
    gemini_model: Optional[str] = None
    gemini_output_hash: Optional[str] = None
    dlp_findings_count: int = 0
    dlp_transformations: Optional[str] = None
    outcome: str = "SUCCESS"
    error_message: Optional[str] = None
    log_version: str = "1.0"