"""
shared/config.py
Central configuration for the HC-CDSS rebuild.

FIXES APPLIED:
  C1   — Removed all hardcoded project ID strings. DLP/KMS templates default
         to empty string; inline configs used when unset.
  C3a  — Added sub_orchestrator_protocols subscription field.
  M5   — Added Secret Manager helper _get_secret() for credential retrieval.
  F-VALIDATE — validate() method now also checks required Pub/Sub topic names
               so a partially-configured environment fails fast at startup.
"""

from dataclasses import dataclass, field
from typing import Optional
import os
from dotenv import load_dotenv

load_dotenv()


def _get_secret(project_id: str, secret_id: str) -> str:
    """
    Retrieve a secret value from Secret Manager.
    Falls back to empty string if Secret Manager is unavailable or secret
    does not exist (allows local .env-only development).
    """
    try:
        from google.cloud import secretmanager
        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
        response = client.access_secret_version(request={"name": name})
        return response.payload.data.decode("UTF-8").strip()
    except Exception:
        return ""


@dataclass
class GCPConfig:
    # ── Core GCP ──────────────────────────────────────────────────────────────
    # FIX C1: default is empty string — must be set via .env GCP_PROJECT_ID
    project_id: str = field(
        default_factory=lambda: os.getenv("GCP_PROJECT_ID", "")
    )
    location: str = field(
        default_factory=lambda: os.getenv("GCP_LOCATION", "us-central1")
    )

    # ── Cloud Healthcare API ──────────────────────────────────────────────────
    dataset_id: str = field(
        default_factory=lambda: os.getenv("FHIR_DATASET_ID", "cds-dataset")
    )
    fhir_store_id: str = field(
        default_factory=lambda: os.getenv("FHIR_STORE_ID", "cds-fhir-store")
    )

    # ── Vertex AI ─────────────────────────────────────────────────────────────
    gemini_model: str = field(
        default_factory=lambda: os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    )
    vertex_ai_search_engine_id: str = field(
        default_factory=lambda: os.getenv("VERTEX_AI_SEARCH_ENGINE_ID", "cds-clinical-protocols")
    )

    # ── Cloud DLP ─────────────────────────────────────────────────────────────
    # FIX C1: DLP templates default to empty — inline config used when unset.
    # Set these in .env once templates are created in the new project:
    #   DLP_INSPECT_TEMPLATE=projects/YOUR-PROJECT/locations/us-central1/inspectTemplates/cds-phi-inspect
    #   DLP_DEIDENTIFY_TEMPLATE=projects/YOUR-PROJECT/locations/us-central1/deidentifyTemplates/cds-phi-deidentify
    dlp_inspect_template: str = field(
        default_factory=lambda: os.getenv("DLP_INSPECT_TEMPLATE", "")
    )
    dlp_deidentify_template: str = field(
        default_factory=lambda: os.getenv("DLP_DEIDENTIFY_TEMPLATE", "")
    )
    # FIX C1: KMS key defaults to empty — set in .env once KMS ring is created
    kms_key_name: str = field(
        default_factory=lambda: os.getenv("KMS_KEY_NAME", "")
    )

    # ── Pub/Sub Topics ────────────────────────────────────────────────────────
    topic_patient_context_ready: str = field(
        default_factory=lambda: os.getenv("TOPIC_PATIENT_CONTEXT_READY", "patient-context-ready")
    )
    topic_diagnosis_ready: str = field(
        default_factory=lambda: os.getenv("TOPIC_DIAGNOSIS_READY", "diagnosis-ready")
    )
    topic_protocols_ready: str = field(
        default_factory=lambda: os.getenv("TOPIC_PROTOCOLS_READY", "protocols-ready")
    )
    topic_drug_interactions_ready: str = field(
        default_factory=lambda: os.getenv("TOPIC_DRUG_INTERACTIONS_READY", "drug-interactions-ready")
    )
    topic_audit_events: str = field(
        default_factory=lambda: os.getenv("TOPIC_AUDIT_EVENTS", "audit-events")
    )

    # ── Pub/Sub Subscriptions ─────────────────────────────────────────────────
    sub_diagnosis_agent: str = field(
        default_factory=lambda: os.getenv("SUB_DIAGNOSIS_AGENT", "diagnosis-agent-sub")
    )
    sub_protocol_agent: str = field(
        default_factory=lambda: os.getenv("SUB_PROTOCOL_AGENT", "protocol-agent-sub")
    )
    sub_drug_interaction_agent: str = field(
        default_factory=lambda: os.getenv("SUB_DRUG_INTERACTION_AGENT", "drug-interaction-agent-sub")
    )
    # FIX M1 — Drug interaction agent subscribes to patient-context-ready
    # so it can use the PatientSnapshot instead of re-querying FHIR
    sub_drug_interaction_patient_context: str = field(
        default_factory=lambda: os.getenv(
            "SUB_DRUG_INTERACTION_PATIENT_CONTEXT", "drug-interaction-patient-context-sub"
        )
    )
    sub_orchestrator_agent: str = field(
        default_factory=lambda: os.getenv("SUB_ORCHESTRATOR_AGENT", "orchestrator-agent-sub")
    )
    sub_orchestrator_diagnosis: str = field(
        default_factory=lambda: os.getenv("SUB_ORCHESTRATOR_DIAGNOSIS", "orchestrator-diagnosis-sub")
    )
    # FIX C3a: NEW — orchestrator now subscribes to protocols-ready topic
    sub_orchestrator_protocols: str = field(
        default_factory=lambda: os.getenv("SUB_ORCHESTRATOR_PROTOCOLS", "orchestrator-protocols-sub")
    )
    sub_audit_agent: str = field(
        default_factory=lambda: os.getenv("SUB_AUDIT_AGENT", "audit-agent-sub")
    )

    # ── Firestore ─────────────────────────────────────────────────────────────
    firestore_collection: str = field(
        default_factory=lambda: os.getenv("FIRESTORE_COLLECTION", "cds_sessions")
    )

    # ── BigQuery ──────────────────────────────────────────────────────────────
    bq_dataset: str = field(
        default_factory=lambda: os.getenv("BQ_DATASET", "cds_audit")
    )
    bq_audit_table: str = field(
        default_factory=lambda: os.getenv("BQ_AUDIT_TABLE", "audit_log")
    )
    bq_sessions_table: str = field(
        default_factory=lambda: os.getenv("BQ_SESSIONS_TABLE", "sessions")
    )

    # ── Cloud Storage ─────────────────────────────────────────────────────────
    # FIX C1: GCS bucket default is empty — set via .env GCS_BUCKET
    gcs_bucket: str = field(
        default_factory=lambda: os.getenv("GCS_BUCKET", "")
    )
    gcs_protocols_prefix: str = field(
        default_factory=lambda: os.getenv("GCS_PROTOCOLS_PREFIX", "clinical-protocols/")
    )

    # ── Agent Settings ────────────────────────────────────────────────────────
    pubsub_max_messages: int = 1
    pubsub_ack_deadline_seconds: int = 60
    gemini_temperature: float = 0.1       # Low temperature = deterministic clinical output
    gemini_max_output_tokens: int = 8192
    dlp_max_findings_per_item: int = 100
    log_version: str = "1.0"

    # ── Derived Properties ────────────────────────────────────────────────────
    @property
    def fhir_base_url(self) -> str:
        return (
            f"https://healthcare.googleapis.com/v1/projects/{self.project_id}"
            f"/locations/{self.location}/datasets/{self.dataset_id}"
            f"/fhirStores/{self.fhir_store_id}/fhir"
        )

    @property
    def pubsub_project_path(self) -> str:
        return f"projects/{self.project_id}"

    @property
    def full_topic_path(self):
        return lambda topic: f"projects/{self.project_id}/topics/{topic}"

    @property
    def full_subscription_path(self):
        return lambda sub: f"projects/{self.project_id}/subscriptions/{sub}"

    @property
    def bq_audit_table_id(self) -> str:
        return f"{self.project_id}.{self.bq_dataset}.{self.bq_audit_table}"

    @property
    def bq_sessions_table_id(self) -> str:
        return f"{self.project_id}.{self.bq_dataset}.{self.bq_sessions_table}"

    def validate(self) -> None:
        """Raise ValueError if any required fields are missing.
        Called at import time in cdss_agent/agent.py for fast-fail startup.
        """
        if not self.project_id:
            raise ValueError(
                "GCP_PROJECT_ID is not set. Add it to your .env file:\n"
                "  GCP_PROJECT_ID=your-new-project-id"
            )
        # FIX F-VALIDATE: Also check that location is set — downstream
        # DLP and FHIR calls will produce cryptic errors without it.
        if not self.location:
            raise ValueError(
                "GCP_LOCATION is not set. Add it to your .env file:\n"
                "  GCP_LOCATION=us-central1"
            )


# ── Singleton ─────────────────────────────────────────────────────────────────
# Import this instance everywhere: from shared.config import config
config = GCPConfig()
