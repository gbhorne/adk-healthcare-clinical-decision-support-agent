"""
agents/patient_context/agent.py
Agent 1 — Patient Context

Responsibilities:
  1. Accept a patient_id as input
  2. Query Cloud Healthcare API FHIR $everything to retrieve all resources
  3. Parse FHIR Bundle into a PatientSnapshot
  4. Publish PatientContextMessage to patient-context-ready topic
  5. Emit audit event to audit-events topic

FIXES APPLIED:
  F-AGE  — Age calculation now uses full date comparison (month/day aware),
            not just year subtraction.
  F-ENC  — Encounter parsing no longer silently overwrites previous encounters.
            The first Encounter's id/reason is retained; subsequent encounters
            are ignored (FHIR $everything returns most-recent first by default).
  F-BP   — Blood pressure component vitals now serialize the component dict as
            a JSON string so downstream prompt formatting always receives a
            scalar string, not a raw dict.
"""

import hashlib
import logging
import uuid
from datetime import date, datetime
from typing import Any, Optional

import google.auth
import google.auth.transport.requests
import requests as http_requests
from google.adk.agents import Agent

from shared.config import config
from shared.models import (
    AgentStatus,
    AllergyRecord,
    AuditEventMessage,
    Condition,
    LabResult,
    Medication,
    PatientContextMessage,
    PatientSnapshot,
    VitalSign,
)
from shared.pubsub_client import publish_message

logger = logging.getLogger(__name__)

AGENT_NAME = "patient_context_agent"
SERVICE_ACCOUNT = f"sa-patient-context@{config.project_id}.iam.gserviceaccount.com"


# ── FHIR Helpers ──────────────────────────────────────────────────────────────

def _get_auth_token() -> str:
    """Get a fresh OAuth2 bearer token using Application Default Credentials."""
    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    credentials.refresh(google.auth.transport.requests.Request())
    return credentials.token


def _fhir_everything(patient_id: str) -> dict:
    """
    Call FHIR $everything on a patient resource.
    Returns the raw FHIR Bundle response as a dict.
    """
    url = f"{config.fhir_base_url}/Patient/{patient_id}/$everything"
    token = _get_auth_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/fhir+json",
    }

    response = http_requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()

    logger.info(
        "FHIR $everything for patient %s returned %d bytes",
        patient_id,
        len(response.content),
    )
    return response.json()


# ── FHIR Parsers ──────────────────────────────────────────────────────────────

def _parse_patient(resource: dict) -> dict:
    """Extract PHI fields from a FHIR Patient resource."""
    name_obj = resource.get("name", [{}])[0]
    given = " ".join(name_obj.get("given", []))
    family = name_obj.get("family", "")
    full_name = f"{given} {family}".strip()

    mrn = None
    ssn = None
    for ident in resource.get("identifier", []):
        coding = ident.get("type", {}).get("coding", [{}])[0]
        code = coding.get("code", "")
        if code == "MR":
            mrn = ident.get("value")
        elif code == "SS":
            ssn = ident.get("value")

    dob = resource.get("birthDate")
    age = None
    if dob:
        try:
            # FIX F-AGE: month/day-aware — born Dec 1990, queried Jan 2026 → 35 not 36
            dob_date = date.fromisoformat(dob)
            today = date.today()
            age = (
                today.year
                - dob_date.year
                - ((today.month, today.day) < (dob_date.month, dob_date.day))
            )
        except ValueError:
            age = datetime.utcnow().year - int(dob.split("-")[0])

    phone = None
    for telecom in resource.get("telecom", []):
        if telecom.get("system") == "phone":
            phone = telecom.get("value")
            break

    address_parts = []
    for addr in resource.get("address", []):
        line = ", ".join(addr.get("line", []))
        city = addr.get("city", "")
        state = addr.get("state", "")
        postal = addr.get("postalCode", "")
        address_parts.append(f"{line}, {city}, {state} {postal}".strip(", "))
    address = "; ".join(address_parts) if address_parts else None

    return {
        "name": full_name or None,
        "mrn": mrn,
        "ssn": ssn,
        "dob": dob,
        "age": age,
        "gender": resource.get("gender"),
        "phone": phone,
        "address": address,
    }


def _parse_encounter(resource: dict) -> dict:
    """Extract encounter ID and reason from a FHIR Encounter resource."""
    reason_text = None
    for reason in resource.get("reasonCode", []):
        reason_text = reason.get("text") or (
            reason.get("coding", [{}])[0].get("display")
        )
        if reason_text:
            break
    return {
        "encounter_id": resource.get("id"),
        "encounter_reason": reason_text,
    }


def _parse_condition(resource: dict) -> Condition:
    coding = resource.get("code", {}).get("coding", [{}])[0]
    return Condition(
        name=resource.get("code", {}).get("text") or coding.get("display", "Unknown"),
        icd10_code=coding.get("code"),
        clinical_status=resource.get("clinicalStatus", {})
            .get("coding", [{}])[0].get("code", "active"),
        onset_date=resource.get("onsetDateTime"),
    )


def _parse_medication(resource: dict) -> Medication:
    med = resource.get("medicationCodeableConcept", {})
    coding = med.get("coding", [{}])[0]
    dose_instruction = resource.get("dosageInstruction", [{}])[0]
    dose_and_rate = dose_instruction.get("doseAndRate", [{}])[0]
    dose_qty = dose_and_rate.get("doseQuantity", {})

    dose_str = None
    if dose_qty:
        dose_str = f"{dose_qty.get('value')} {dose_qty.get('unit', '')}".strip()

    return Medication(
        name=med.get("text") or coding.get("display", "Unknown"),
        rxnorm_code=coding.get("code"),
        dose=dose_str,
        route=dose_instruction.get("route", {})
            .get("coding", [{}])[0].get("display"),
        frequency=dose_instruction.get("text"),
        status=resource.get("status", "active"),
    )


def _parse_allergy(resource: dict) -> AllergyRecord:
    coding = resource.get("code", {}).get("coding", [{}])[0]
    reaction = resource.get("reaction", [{}])[0]
    manifestation = reaction.get("manifestation", [{}])[0]
    manifestation_coding = manifestation.get("coding", [{}])[0]

    return AllergyRecord(
        substance=resource.get("code", {}).get("text") or coding.get("display", "Unknown"),
        rxnorm_code=coding.get("code"),
        criticality=resource.get("criticality", "unknown"),
        severity=reaction.get("severity"),
        reaction=reaction.get("description") or manifestation_coding.get("display"),
    )


def _parse_observation(resource: dict) -> Optional[dict]:
    """
    Parse a FHIR Observation into either a LabResult or VitalSign dict.
    Returns {"type": "lab"|"vital", "data": LabResult|VitalSign} or None.
    """
    category_codes = [
        c.get("code", "")
        for cat in resource.get("category", [])
        for c in cat.get("coding", [{}])
    ]

    loinc_coding = resource.get("code", {}).get("coding", [{}])[0]
    loinc_code = loinc_coding.get("code")
    display_name = resource.get("code", {}).get("text") or loinc_coding.get("display", "Unknown")

    interpretation_code = (
        resource.get("interpretation", [{}])[0]
        .get("coding", [{}])[0]
        .get("code", "N")
    )

    effective = resource.get("effectiveDateTime")

    # Handle component observations (e.g. blood pressure panel)
    if resource.get("component"):
        components = {}
        for comp in resource["component"]:
            comp_name = comp.get("code", {}).get("coding", [{}])[0].get("display", "")
            comp_val = comp.get("valueQuantity", {})
            components[comp_name] = f"{comp_val.get('value')} {comp_val.get('unit', '')}".strip()

        import json as _json
        obs_type = "vital" if "vital-signs" in category_codes else "lab"
        vital = VitalSign(
            name=display_name,
            loinc_code=loinc_code,
            # FIX F-BP: serialize component dict to string so downstream
            # prompt builders always receive a scalar, never a raw dict.
            value=_json.dumps(components),
            unit="panel",
            interpretation=interpretation_code,
            recorded_at=effective,
        )
        return {"type": obs_type, "data": vital}

    # Handle scalar observations
    value_qty = resource.get("valueQuantity", {})
    if not value_qty:
        return None

    value = value_qty.get("value")
    unit = value_qty.get("unit", "")
    ref_range = None
    if resource.get("referenceRange"):
        ref_range = resource["referenceRange"][0].get("text")

    if "laboratory" in category_codes:
        lab = LabResult(
            name=display_name,
            loinc_code=loinc_code,
            value=value,
            unit=unit,
            interpretation=interpretation_code,
            reference_range=ref_range,
            collected_at=effective,
        )
        return {"type": "lab", "data": lab}

    if "vital-signs" in category_codes or "survey" in category_codes:
        vital = VitalSign(
            name=display_name,
            loinc_code=loinc_code,
            value=value,
            unit=unit,
            interpretation=interpretation_code,
            recorded_at=effective,
        )
        return {"type": "vital", "data": vital}

    return None


# ── Bundle Parser ─────────────────────────────────────────────────────────────

def parse_fhir_bundle(patient_id: str, bundle: dict) -> PatientSnapshot:
    """
    Walk a FHIR Bundle and construct a PatientSnapshot.
    """
    snapshot = PatientSnapshot(patient_id=patient_id)

    for entry in bundle.get("entry", []):
        resource = entry.get("resource", {})
        resource_type = resource.get("resourceType")

        if resource_type == "Patient":
            patient_data = _parse_patient(resource)
            for key, value in patient_data.items():
                setattr(snapshot, key, value)

        elif resource_type == "Encounter":
            # FIX F-ENC: Retain only the first encounter — FHIR $everything
            # returns entries in reverse-chronological order so the first
            # entry is the most recent. Overwriting would silently discard it.
            if snapshot.encounter_id is None:
                enc_data = _parse_encounter(resource)
                snapshot.encounter_id = enc_data["encounter_id"]
                snapshot.encounter_reason = enc_data["encounter_reason"]

        elif resource_type == "Condition":
            try:
                snapshot.conditions.append(_parse_condition(resource))
            except Exception as e:
                logger.warning("Failed to parse Condition: %s", e)

        elif resource_type == "MedicationRequest":
            try:
                snapshot.medications.append(_parse_medication(resource))
            except Exception as e:
                logger.warning("Failed to parse MedicationRequest: %s", e)

        elif resource_type == "AllergyIntolerance":
            try:
                snapshot.allergies.append(_parse_allergy(resource))
            except Exception as e:
                logger.warning("Failed to parse AllergyIntolerance: %s", e)

        elif resource_type == "Observation":
            try:
                result = _parse_observation(resource)
                if result:
                    if result["type"] == "lab":
                        snapshot.lab_results.append(result["data"])
                    else:
                        snapshot.vital_signs.append(result["data"])
            except Exception as e:
                logger.warning("Failed to parse Observation: %s", e)

    return snapshot


# ── ADK Tool Functions ────────────────────────────────────────────────────────

def fetch_patient_context(patient_id: str, session_id: Optional[str] = None) -> dict:
    """
    ADK tool: Fetch patient context from FHIR and publish to Pub/Sub.

    Args:
        patient_id: FHIR Patient resource ID (e.g. "patient-marcus-webb")
        session_id: Optional session ID; generated if not provided

    Returns:
        dict with session_id, patient_id, resource_counts, and status
    """
    session_id = session_id or str(uuid.uuid4())
    logger.info("Starting patient context fetch | patient=%s | session=%s", patient_id, session_id)

    try:
        # 1. Query FHIR $everything
        bundle = _fhir_everything(patient_id)
        fhir_query = f"Patient/{patient_id}/$everything"

        # 2. Parse into PatientSnapshot
        snapshot = parse_fhir_bundle(patient_id, bundle)

        # 3. Build Pub/Sub message
        message = PatientContextMessage(
            session_id=session_id,
            patient_id=patient_id,
            patient_snapshot=snapshot,
            agent_status=AgentStatus.SUCCESS,
        )

        # 4. Publish to patient-context-ready topic
        publish_message(
            config.topic_patient_context_ready,
            message,
            attributes={"session_id": session_id, "patient_id": patient_id},
        )

        # 5. Emit audit event
        audit = AuditEventMessage(
            session_id=session_id,
            principal=SERVICE_ACCOUNT,
            agent_name=AGENT_NAME,
            action="FHIR_QUERY",
            resource_type="Patient",
            resource_id=patient_id,
            fhir_query=fhir_query,
            outcome="SUCCESS",
        )
        publish_message(config.topic_audit_events, audit)

        result = {
            "session_id": session_id,
            "patient_id": patient_id,
            "status": "SUCCESS",
            "conditions_found": len(snapshot.conditions),
            "medications_found": len(snapshot.medications),
            "allergies_found": len(snapshot.allergies),
            "labs_found": len(snapshot.lab_results),
            "vitals_found": len(snapshot.vital_signs),
            "published_to": config.topic_patient_context_ready,
        }

        logger.info("Patient context complete: %s", result)
        return result

    except Exception as e:
        logger.error("Patient context failed for %s: %s", patient_id, str(e))

        # Emit failure audit event
        audit = AuditEventMessage(
            session_id=session_id,
            principal=SERVICE_ACCOUNT,
            agent_name=AGENT_NAME,
            action="FHIR_QUERY",
            resource_type="Patient",
            resource_id=patient_id,
            outcome="FAILED",
            error_message=str(e),
        )
        publish_message(config.topic_audit_events, audit)

        return {
            "session_id": session_id,
            "patient_id": patient_id,
            "status": "FAILED",
            "error": str(e),
        }


# ── ADK Agent Definition ──────────────────────────────────────────────────────

patient_context_agent = Agent(
    name=AGENT_NAME,
    model=config.gemini_model,
    description=(
        "Retrieves complete patient context from the FHIR R4 store using the "
        "FHIR $everything operation. Parses all clinical resources — conditions, "
        "medications, allergies, labs, and vitals — into a structured snapshot "
        "and publishes it to the patient-context-ready Pub/Sub topic."
    ),
    instruction=(
        "You are the Patient Context Agent in a clinical decision support pipeline. "
        "When given a patient_id, call the fetch_patient_context tool to retrieve "
        "all FHIR resources for that patient and publish the structured result "
        "to the Pub/Sub pipeline. Always include the session_id in your response "
        "so downstream agents can correlate their work."
    ),
    tools=[fetch_patient_context],
)