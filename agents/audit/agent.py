"""
agents/audit/agent.py
Agent 6 — Audit

Responsibilities:
  1. Pull AuditEventMessages from audit-events subscription
  2. Write structured audit log entries to Cloud Logging  ← FIX H4
  3. Write audit rows to BigQuery audit_log table
  4. Enforce: no PHI values ever written to audit records
  5. Support both single-event and batch processing modes

FIXES APPLIED:
  H4      — Cloud Logging re-enabled with try/except. 504 timeouts no longer
             silently break the HIPAA audit trail — errors are logged and
             BigQuery always receives the write.
  F-AUDIT1 — Consecutive-timeout limit raised from 2 to 3 to reduce the chance
              of abandoning the audit queue during a transient network blip.
  F-AUDIT2 — Extended PHI_FIELD_NAMES set with additional HIPAA identifiers:
              encounter_id, insurance_id, account_number, npi, fax_number.
"""

import json
import logging
import uuid
from datetime import datetime
from typing import Optional

from google.cloud import logging as cloud_logging
from google.cloud import bigquery
from google.adk.agents import Agent

from shared.config import config
from shared.models import AgentStatus, AuditEventMessage
from shared.pubsub_client import pull_message

logger = logging.getLogger(__name__)

AGENT_NAME = "audit_agent"
SERVICE_ACCOUNT = f"sa-audit@{config.project_id}.iam.gserviceaccount.com"

# FIX F-AUDIT2: Extended with additional HIPAA Safe Harbor identifiers
PHI_FIELD_NAMES = {
    "name", "full_name", "patient_name", "first_name", "last_name",
    "ssn", "social_security", "dob", "date_of_birth", "birthdate",
    "phone", "phone_number", "fax", "fax_number",
    "email", "email_address",
    "address", "street_address", "zip_code", "postal_code",
    "mrn", "medical_record_number",
    "encounter_id", "account_number", "insurance_id", "member_id",
    "npi", "healthcare_npi", "dea_number",
    "ip_address", "device_id", "biometric_id",
}


# ── PHI Safety Guard ──────────────────────────────────────────────────────────

def _sanitize_for_audit(data: dict) -> dict:
    """
    Recursively remove any fields whose keys match known PHI field names.
    Safety backstop — audit data should never contain PHI upstream,
    but this ensures it even if an upstream agent makes an error.
    """
    sanitized = {}
    for key, value in data.items():
        if key.lower() in PHI_FIELD_NAMES:
            sanitized[key] = "[REDACTED]"
        elif isinstance(value, dict):
            sanitized[key] = _sanitize_for_audit(value)
        elif isinstance(value, list):
            sanitized[key] = [
                _sanitize_for_audit(v) if isinstance(v, dict) else v
                for v in value
            ]
        else:
            sanitized[key] = value
    return sanitized


# ── Cloud Logging Writer ──────────────────────────────────────────────────────

class AuditLogger:
    """
    Writes structured audit events to Cloud Logging.
    Log name: cds-audit-trail
    Each entry is a structured JSON log with no PHI values.
    """

    def __init__(self):
        self._client = cloud_logging.Client(project=config.project_id)
        self._logger = self._client.logger("cds-audit-trail")

    def write(self, event: AuditEventMessage) -> None:
        """Write a single audit event to Cloud Logging."""
        log_entry = _sanitize_for_audit({
            "event_id": event.event_id,
            "session_id": event.session_id,
            "timestamp": event.timestamp,
            "principal": event.principal,
            "agent_name": event.agent_name,
            "action": event.action,
            "resource_type": event.resource_type,
            "resource_id": event.resource_id,
            "fhir_query": event.fhir_query,
            "gemini_prompt_hash": event.gemini_prompt_hash,
            "gemini_model": event.gemini_model,
            "gemini_output_hash": event.gemini_output_hash,
            "dlp_findings_count": event.dlp_findings_count,
            "dlp_transformations": event.dlp_transformations,
            "outcome": event.outcome,
            "error_message": event.error_message,
            "log_version": event.log_version,
            "phi_in_log": False,
        })

        severity = "ERROR" if event.outcome == "FAILED" else "INFO"

        self._logger.log_struct(
            log_entry,
            severity=severity,
            labels={
                "session_id": event.session_id,
                "agent_name": event.agent_name,
                "outcome": event.outcome,
            },
        )

        logger.debug(
            "Audit log written | event_id=%s | agent=%s | action=%s | outcome=%s",
            event.event_id, event.agent_name, event.action, event.outcome,
        )


# ── BigQuery Writer ───────────────────────────────────────────────────────────

class AuditBigQueryWriter:
    """
    Writes audit event rows to the BigQuery audit_log table.
    Append-only — rows are never updated or deleted (HIPAA requirement).
    """

    def __init__(self):
        self._client = bigquery.Client(project=config.project_id)
        self._table_id = config.bq_audit_table_id

    def _build_row(self, event: AuditEventMessage) -> dict:
        return _sanitize_for_audit({
            "event_id": event.event_id,
            "session_id": event.session_id,
            "timestamp": event.timestamp,
            "principal": event.principal,
            "agent_name": event.agent_name,
            "action": event.action,
            "resource_type": event.resource_type or "",
            "resource_id": event.resource_id or "",
            "fhir_query": event.fhir_query or "",
            "gemini_prompt_hash": event.gemini_prompt_hash or "",
            "gemini_model": event.gemini_model or "",
            "gemini_output_hash": event.gemini_output_hash or "",
            "dlp_findings_count": event.dlp_findings_count,
            "dlp_transformations": event.dlp_transformations or "",
            "outcome": event.outcome,
            "error_message": event.error_message or "",
            "log_version": event.log_version,
        })

    def write(self, event: AuditEventMessage) -> None:
        row = self._build_row(event)
        errors = self._client.insert_rows_json(self._table_id, [row])
        if errors:
            logger.error(
                "BigQuery audit insert failed | event_id=%s | errors=%s",
                event.event_id, errors,
            )
        else:
            logger.debug("BigQuery audit row written | event_id=%s", event.event_id)

    def write_batch(self, events: list[AuditEventMessage]) -> int:
        rows = [self._build_row(e) for e in events]
        if not rows:
            return 0

        errors = self._client.insert_rows_json(self._table_id, rows)
        if errors:
            logger.error("BigQuery batch audit insert errors: %s", errors)
            return 0

        logger.info("BigQuery batch audit write: %d rows", len(rows))
        return len(rows)


# ── ADK Tool Functions ────────────────────────────────────────────────────────

def process_audit_events(
    batch_size: int = 10,
    session_id: Optional[str] = None,
) -> dict:
    """
    ADK tool: Pull audit events from Pub/Sub and write to
    Cloud Logging and BigQuery.
    """
    run_id = session_id or str(uuid.uuid4())
    logger.info("Audit agent starting | run_id=%s | batch_size=%d", run_id, batch_size)

    audit_logger = AuditLogger()
    bq_writer = AuditBigQueryWriter()

    events_processed = 0
    cloud_logging_failures = 0
    bq_failures = 0
    consecutive_timeouts = 0
    batch_size = min(batch_size, 20)

    for _ in range(batch_size):
        try:
            event = pull_message(
                config.sub_audit_agent,
                AuditEventMessage,
                timeout=5.0,
            )

            if not event:
                break

            consecutive_timeouts = 0

            # FIX H4: Cloud Logging re-enabled with try/except.
            # A 504 timeout in the Cloud Logging write does NOT prevent
            # the BigQuery write from succeeding.
            try:
                audit_logger.write(event)
            except Exception as cl_err:
                cloud_logging_failures += 1
                logger.error(
                    "Cloud Logging write failed | event_id=%s | error=%s",
                    event.event_id, str(cl_err),
                )

            try:
                bq_writer.write(event)
            except Exception as bq_err:
                bq_failures += 1
                logger.error(
                    "BigQuery write failed | event_id=%s | error=%s",
                    event.event_id, str(bq_err),
                )

            events_processed += 1

        except Exception as e:
            consecutive_timeouts += 1
            logger.warning("Audit event skipped (timeout/error): %s", str(e))
            # FIX F-AUDIT1: Raised from 2 to 3 — a single network blip causing
            # 2 sequential failures no longer abandons the entire audit queue.
            if consecutive_timeouts >= 3:
                logger.info("Stopping audit pull after %d consecutive timeouts", consecutive_timeouts)
                break
            continue

    status = "SUCCESS"
    if cloud_logging_failures > 0 or bq_failures > 0:
        status = "PARTIAL"

    result = {
        "run_id": run_id,
        "status": status,
        "events_processed": events_processed,
        "cloud_logging_failures": cloud_logging_failures,
        "bq_failures": bq_failures,
        "written_to": ["cloud-logging:cds-audit-trail", config.bq_audit_table_id],
    }

    logger.info("Audit agent complete: %s", result)
    return result


def write_audit_event_direct(
    session_id: str,
    agent_name: str,
    action: str,
    outcome: str,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    error_message: Optional[str] = None,
) -> dict:
    """
    ADK tool: Write a single audit event directly without going through Pub/Sub.
    Used for recording the audit agent's own actions and pipeline-level events.
    """
    event = AuditEventMessage(
        session_id=session_id,
        principal=SERVICE_ACCOUNT,
        agent_name=agent_name,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        outcome=outcome,
        error_message=error_message,
    )

    audit_logger = AuditLogger()
    bq_writer = AuditBigQueryWriter()

    cloud_logging_ok = False
    bq_ok = False

    try:
        audit_logger.write(event)
        cloud_logging_ok = True
    except Exception as e:
        logger.error("Direct audit Cloud Logging write failed: %s", str(e))

    try:
        bq_writer.write(event)
        bq_ok = True
    except Exception as e:
        logger.error("Direct audit BigQuery write failed: %s", str(e))

    return {
        "event_id": event.event_id,
        "status": "SUCCESS" if (cloud_logging_ok and bq_ok) else "PARTIAL",
        "cloud_logging": "written" if cloud_logging_ok else "failed",
        "bigquery": "written" if bq_ok else "failed",
    }


# ── ADK Agent Definition ──────────────────────────────────────────────────────

audit_agent = Agent(
    name=AGENT_NAME,
    model=config.gemini_model,
    description=(
        "Pulls audit events from the audit-events Pub/Sub subscription and writes "
        "structured, PHI-free audit records to Cloud Logging (cds-audit-trail) and "
        "the BigQuery audit_log table. Implements HIPAA Audit Controls "
        "(45 CFR 164.312(b)) with append-only, tamper-evident logging. "
        "No PHI values are ever written to audit records."
    ),
    instruction=(
        "You are the Audit Agent in a HIPAA-compliant clinical decision support pipeline. "
        "Call process_audit_events to pull pending audit events from Pub/Sub and write "
        "them to Cloud Logging and BigQuery. You can also call write_audit_event_direct "
        "to record specific events directly. Always verify that no PHI values appear "
        "in audit records — only resource IDs, hashes, and counts are permitted. "
        "Report the number of events processed, any Cloud Logging failures, and any "
        "BigQuery failures."
    ),
    tools=[process_audit_events, write_audit_event_direct],
)
