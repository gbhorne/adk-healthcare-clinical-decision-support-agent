"""
agents/drug_interaction/agent.py
Agent 4 — Drug Interaction & Allergy Conflict

Responsibilities:
  1. Pull DiagnosisMessage from drug_interaction_agent-sub
  2. Re-query FHIR for current medications and allergies
  3. Check drug-drug interactions via RxNorm API
  4. Check allergy conflicts including cross-reactivity
  5. Check contraindications against active conditions and lab values
  6. Publish DrugInteractionMessage to drug-interactions-ready topic (with PatientSnapshot)
  7. Emit audit event

FIXES APPLIED:
  F-SNAP — PatientSnapshot pulled from PatientContextMessage (via
            sub_drug_interaction_patient_context) and attached to
            DrugInteractionMessage so Orchestrator can pseudonymize it.
  F-K+   — Potassium alert threshold raised from 5.0 to 5.5 mEq/L to match
            clinical consensus (5.0 causes alert fatigue on borderline-normal values).
  F-SULF — Sulfonamide/furosemide cross-reactivity downgraded from MODERATE
            to LOW with updated evidence note. 2022-2025 literature shows
            structural difference makes clinically meaningful cross-reactivity unlikely.
  F-LOINC — Added LOINC 33914-3 (eGFR MDRD) alongside CKD-EPI codes so
             older EHR systems are covered.
"""

import logging
import uuid
from typing import Optional

import google.auth
import google.auth.transport.requests
import requests as http_requests
from google.adk.agents import Agent

from shared.config import config
from shared.models import (
    AgentStatus,
    AlertSeverity,
    AlertType,
    AuditEventMessage,
    ClinicalAlert,
    DiagnosisMessage,
    DrugInteractionMessage,
    PatientContextMessage,
)
from shared.pubsub_client import publish_message, pull_message

logger = logging.getLogger(__name__)

AGENT_NAME = "drug_interaction_agent"
SERVICE_ACCOUNT = f"sa-drug-interaction@{config.project_id}.iam.gserviceaccount.com"

# RxNorm API base URL (public, no auth required)
RXNORM_BASE = "https://rxnav.nlm.nih.gov/REST"

# ── Known cross-reactivity rules ──────────────────────────────────────────────
# Maps allergen class -> list of drug classes/names with cross-reactivity risk
CROSS_REACTIVITY_RULES = {
    "penicillin": {
        "cross_reactive": ["amoxicillin", "ampicillin", "cephalosporin", "ceftriaxone",
                           "cefazolin", "carbapenem", "imipenem", "meropenem"],
        "note": "Penicillin allergy: 1-2% cross-reactivity with cephalosporins, <1% with carbapenems",
        "severity": AlertSeverity.HIGH,
    },
    "sulfonamide": {
        "cross_reactive": ["sulfamethoxazole", "bactrim", "trimethoprim-sulfamethoxazole",
                           "furosemide", "hydrochlorothiazide", "celecoxib"],
        # FIX F-SULF: Downgraded from MODERATE to LOW. 2022-2025 literature
        # (ACAAI, AAD) shows sulfonamide antibiotic allergy does NOT reliably
        # predict reaction to non-antibiotic sulfonamides (furosemide, thiazides)
        # due to structural differences. MODERATE caused alert fatigue.
        "note": "Sulfonamide antibiotic allergy: structural cross-reactivity with non-antibiotic sulfonamides (furosemide, thiazides) is unlikely per current evidence. Clinical assessment recommended but risk is low.",
        "severity": AlertSeverity.LOW,
    },
    "iodinated contrast": {
        "cross_reactive": ["contrast media", "iohexol", "iopamidol", "iodixanol"],
        "note": "Prior anaphylaxis to contrast — premedication required or avoid all iodinated contrast",
        "severity": AlertSeverity.CRITICAL,
    },
    "nsaid": {
        "cross_reactive": ["ibuprofen", "naproxen", "ketorolac", "indomethacin", "aspirin"],
        "note": "NSAID hypersensitivity: cross-reactivity among NSAIDs via COX-1 inhibition",
        "severity": AlertSeverity.HIGH,
    },
}

# ── Contraindication rules ────────────────────────────────────────────────────
# Maps medication name (lowercase) -> list of contraindication check dicts
CONTRAINDICATION_RULES = [
    {
        "medication": "metformin",
        "condition": "egfr_below_30",
        "title": "Metformin Contraindicated — eGFR < 30",
        "description": (
            "Metformin is absolutely contraindicated when eGFR < 30 mL/min/1.73m² "
            "due to risk of lactic acidosis. FDA recommends dose reduction when "
            "eGFR 30-45 and discontinuation below 30."
        ),
        "severity": AlertSeverity.CRITICAL,
        "recommendation": "Discontinue Metformin immediately. Consider alternative agents: "
                         "SGLT-2 inhibitors (if eGFR allows), GLP-1 agonists, or insulin.",
    },
    {
        "medication": "metformin",
        "condition": "egfr_below_45",
        "title": "Metformin Caution — eGFR 30-45",
        "description": (
            "Metformin use requires caution when eGFR is 30-45 mL/min/1.73m². "
            "FDA recommends dose reduction. Monitor renal function every 3-6 months."
        ),
        "severity": AlertSeverity.HIGH,
        "recommendation": "Reduce Metformin dose. Monitor renal function closely. "
                         "Consider transition to alternative agent.",
    },
    {
        "medication": "lisinopril",
        # FIX F-K+: Condition renamed potassium_above_5_5 to match threshold.
        "condition": "potassium_above_5_5",
        "title": "ACE Inhibitor Risk — Hyperkalemia",
        "description": (
            "Lisinopril (ACE inhibitor) can worsen hyperkalemia. "
            "Current potassium >= 5.5 mEq/L — monitor closely or consider "
            "switching to a calcium channel blocker."
        ),
        "severity": AlertSeverity.HIGH,
        "recommendation": "Check potassium trend. Consider switching antihypertensive "
                         "if potassium remains elevated above 5.5 mEq/L.",
    },
    {
        "medication": "glipizide",
        "condition": "egfr_below_30",
        "title": "Sulfonylurea Risk — Renal Impairment",
        "description": (
            "Glipizide metabolites may accumulate in severe renal impairment, "
            "increasing hypoglycemia risk."
        ),
        "severity": AlertSeverity.MODERATE,
        "recommendation": "Use with caution. Monitor blood glucose closely. "
                         "Consider dose reduction or switch to insulin.",
    },
]


# ── FHIR Helpers ──────────────────────────────────────────────────────────────

def _get_auth_token() -> str:
    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    credentials.refresh(google.auth.transport.requests.Request())
    return credentials.token


def _fhir_get_resources(patient_id: str, resource_type: str) -> list[dict]:
    """Query FHIR for a specific resource type for a patient."""
    url = f"{config.fhir_base_url}/{resource_type}"
    token = _get_auth_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/fhir+json",
    }
    params = {"patient": patient_id, "_count": 50}

    response = http_requests.get(url, headers=headers, params=params, timeout=30)
    response.raise_for_status()

    bundle = response.json()
    resources = []
    for entry in bundle.get("entry", []):
        resources.append(entry.get("resource", {}))

    logger.info(
        "FHIR query %s for patient %s returned %d resources",
        resource_type, patient_id, len(resources)
    )
    return resources


# ── RxNorm Interaction Check ──────────────────────────────────────────────────

def _get_rxcuis(drug_name: str) -> list[str]:
    """Look up RxCUI codes for a drug name."""
    try:
        url = f"{RXNORM_BASE}/rxcui.json"
        response = http_requests.get(
            url, params={"name": drug_name, "search": 1}, timeout=10
        )
        response.raise_for_status()
        data = response.json()
        id_group = data.get("idGroup", {})
        return id_group.get("rxnormId", [])
    except Exception as e:
        logger.warning("RxCUI lookup failed for %s: %s", drug_name, e)
        return []


def _check_rxnorm_interactions(rxcuis: list[str]) -> list[dict]:
    """Check drug-drug interactions for a list of RxCUI codes."""
    if len(rxcuis) < 2:
        return []
    try:
        url = f"{RXNORM_BASE}/interaction/list.json"
        response = http_requests.get(
            url, params={"rxcuis": " ".join(rxcuis)}, timeout=15
        )
        response.raise_for_status()
        data = response.json()

        interactions = []
        for group in data.get("fullInteractionTypeGroup", []):
            for interaction_type in group.get("fullInteractionType", []):
                for pair in interaction_type.get("interactionPair", []):
                    severity = pair.get("severity", "N/A")
                    description = pair.get("description", "")
                    drugs = [
                        ic.get("minConceptItem", {}).get("name", "")
                        for ic in pair.get("interactionConcept", [])
                    ]
                    interactions.append({
                        "drugs": drugs,
                        "severity": severity,
                        "description": description,
                    })
        return interactions
    except Exception as e:
        logger.warning("RxNorm interaction check failed: %s", e)
        return []


# ── Allergy Conflict Check ────────────────────────────────────────────────────

def _check_allergy_conflicts(
    medications: list[dict],
    allergies: list[dict],
) -> list[ClinicalAlert]:
    """
    Check each medication against known allergies and cross-reactivity rules.
    """
    alerts = []

    for allergy in allergies:
        substance = allergy.get("code", {}).get("text", "").lower()
        criticality = allergy.get("criticality", "low")

        # Direct match check
        for med in medications:
            med_name = med.get("medicationCodeableConcept", {}).get("text", "").lower()
            if substance and med_name and substance in med_name:
                alerts.append(ClinicalAlert(
                    alert_type=AlertType.ALLERGY_CONFLICT,
                    severity=AlertSeverity.CRITICAL,
                    title=f"ALLERGY CONFLICT: {med_name.title()} — {substance.title()}",
                    description=(
                        f"Patient has documented allergy to {substance} (criticality: {criticality}). "
                        f"Current medication {med_name} matches or contains the allergen."
                    ),
                    affected_medication=med_name,
                    recommendation=f"Discontinue {med_name} immediately. Select alternative.",
                    requires_immediate_action=True,
                ))

        # Cross-reactivity check
        for allergen_class, rule in CROSS_REACTIVITY_RULES.items():
            if allergen_class in substance or substance in allergen_class:
                for med in medications:
                    med_name = med.get("medicationCodeableConcept", {}).get("text", "").lower()
                    for cross_drug in rule["cross_reactive"]:
                        if cross_drug in med_name:
                            alerts.append(ClinicalAlert(
                                alert_type=AlertType.ALLERGY_CONFLICT,
                                severity=rule["severity"],
                                title=f"Cross-Reactivity Risk: {med_name.title()}",
                                description=(
                                    f"Patient allergic to {substance}. "
                                    f"{med_name.title()} has known cross-reactivity risk. "
                                    f"{rule['note']}"
                                ),
                                affected_medication=med_name,
                                recommendation=(
                                    f"Assess cross-reactivity risk before administering {med_name}. "
                                    "Consider allergy consult or alternative agent."
                                ),
                                requires_immediate_action=rule["severity"] == AlertSeverity.CRITICAL,
                            ))

    return alerts


# ── Contraindication Check ────────────────────────────────────────────────────

def _check_contraindications(
    medications: list[dict],
    observations: list[dict],
) -> list[ClinicalAlert]:
    """
    Check medications against lab values for known contraindications.
    """
    alerts = []

    # Extract key lab values from observations
    lab_values: dict[str, float] = {}
    for obs in observations:
        loinc = obs.get("code", {}).get("coding", [{}])[0].get("code", "")
        value = obs.get("valueQuantity", {}).get("value")
        if value is not None:
            # eGFR — FIX F-LOINC: added 33914-3 (MDRD) used by older EHR systems
            if loinc in ("69405-9", "62238-1", "33914-3"):
                lab_values["egfr"] = float(value)
            # Creatinine
            elif loinc == "2160-0":
                lab_values["creatinine"] = float(value)
            # Potassium
            elif loinc == "2823-3":
                lab_values["potassium"] = float(value)
            # Lactate
            elif loinc == "2524-7":
                lab_values["lactate"] = float(value)

    egfr = lab_values.get("egfr")
    potassium = lab_values.get("potassium")

    for med_resource in medications:
        med_name = med_resource.get("medicationCodeableConcept", {}).get("text", "").lower()

        for rule in CONTRAINDICATION_RULES:
            if rule["medication"] not in med_name:
                continue

            condition = rule["condition"]
            triggered = False

            if condition == "egfr_below_30" and egfr is not None and egfr < 30:
                triggered = True
            elif condition == "egfr_below_45" and egfr is not None and 30 <= egfr < 45:
                triggered = True
            # FIX F-K+: Threshold raised to 5.5 mEq/L (clinical consensus).
            # Alerting at 5.0 flags borderline-normal values, causing alert fatigue.
            elif condition == "potassium_above_5_5" and potassium is not None and potassium >= 5.5:
                triggered = True

            if triggered:
                alerts.append(ClinicalAlert(
                    alert_type=AlertType.CONTRAINDICATION,
                    severity=rule["severity"],
                    title=rule["title"],
                    description=rule["description"],
                    affected_medication=med_name,
                    recommendation=rule["recommendation"],
                    evidence_basis=f"Lab value: {condition} | eGFR={egfr}, K+={potassium}",
                    requires_immediate_action=rule["severity"] in (
                        AlertSeverity.CRITICAL, AlertSeverity.HIGH
                    ),
                ))

    return alerts


# ── ADK Tool Function ─────────────────────────────────────────────────────────

def run_drug_interaction_check(session_id: Optional[str] = None) -> dict:
    """
    ADK tool: Pull diagnosis message, re-query FHIR for medications and allergies,
    check interactions and contraindications, publish alerts.

    Args:
        session_id: Optional session ID for correlation

    Returns:
        dict with alert summary and status
    """
    session_id = session_id or str(uuid.uuid4())
    logger.info("Drug interaction agent starting | session=%s", session_id)

    try:
        # 1. Pull diagnosis message from Pub/Sub
        diagnosis_message = pull_message(
            config.sub_drug_interaction_agent,
            DiagnosisMessage,
            timeout=30.0,
        )

        if not diagnosis_message:
            return {
                "session_id": session_id,
                "status": "NO_MESSAGE",
                "message": "No diagnosis message available on subscription",
            }

        session_id = diagnosis_message.session_id
        patient_id = diagnosis_message.patient_id

        # FIX F-SNAP: Pull PatientContextMessage so PatientSnapshot can be
        # forwarded to the Orchestrator for pseudonymization. Without this,
        # CDSSummary.patient_snapshot_pseudonymized is always None.
        patient_snapshot = None
        try:
            context_msg = pull_message(
                config.sub_drug_interaction_patient_context,
                PatientContextMessage,
                timeout=15.0,
            )
            if context_msg and context_msg.session_id == session_id:
                patient_snapshot = context_msg.patient_snapshot
                logger.info(
                    "PatientSnapshot loaded for forwarding | session=%s | meds=%d | allergies=%d",
                    session_id,
                    len(patient_snapshot.medications),
                    len(patient_snapshot.allergies),
                )
            else:
                logger.warning(
                    "PatientContextMessage not found or session mismatch — "
                    "snapshot will be absent from CDSSummary | session=%s", session_id
                )
        except Exception as snap_err:
            logger.warning("PatientSnapshot pull failed (non-fatal): %s", snap_err)

        # 2. Re-query FHIR for current medications, allergies, observations
        medications = _fhir_get_resources(patient_id, "MedicationRequest")
        allergies = _fhir_get_resources(patient_id, "AllergyIntolerance")
        observations = _fhir_get_resources(patient_id, "Observation")

        all_alerts: list[ClinicalAlert] = []

        # 3. RxNorm drug-drug interaction check
        med_names = [
            m.get("medicationCodeableConcept", {}).get("text", "")
            for m in medications
        ]
        all_rxcuis = []
        for med_name in med_names:
            if med_name:
                rxcuis = _get_rxcuis(med_name)
                all_rxcuis.extend(rxcuis)

        if len(all_rxcuis) >= 2:
            interactions = _check_rxnorm_interactions(all_rxcuis)
            for interaction in interactions:
                severity_str = interaction.get("severity", "moderate").lower()
                severity_map = {
                    "high": AlertSeverity.HIGH,
                    "moderate": AlertSeverity.MODERATE,
                    "low": AlertSeverity.LOW,
                    "n/a": AlertSeverity.INFO,
                }
                severity = severity_map.get(severity_str, AlertSeverity.MODERATE)
                drugs = interaction.get("drugs", [])

                all_alerts.append(ClinicalAlert(
                    alert_type=AlertType.DRUG_INTERACTION,
                    severity=severity,
                    title=f"Drug Interaction: {' + '.join(drugs)}",
                    description=interaction.get("description", ""),
                    affected_medication=" + ".join(drugs),
                    recommendation="Review medication list. Consider alternatives if interaction is clinically significant.",
                    evidence_basis="RxNorm Drug Interaction API",
                    requires_immediate_action=severity == AlertSeverity.HIGH,
                ))

        # 4. Allergy conflict check
        allergy_alerts = _check_allergy_conflicts(medications, allergies)
        all_alerts.extend(allergy_alerts)

        # 5. Contraindication check
        contraindication_alerts = _check_contraindications(medications, observations)
        all_alerts.extend(contraindication_alerts)

        has_critical = any(
            a.severity in (AlertSeverity.CRITICAL, AlertSeverity.HIGH)
            for a in all_alerts
        )

        # 6. Publish DrugInteractionMessage
        drug_message = DrugInteractionMessage(
            session_id=session_id,
            patient_id=patient_id,
            patient_snapshot=patient_snapshot,  # FIX F-SNAP: forwarded to Orchestrator
            alerts=all_alerts,
            medications_checked=med_names,
            allergies_checked=[
                a.get("code", {}).get("text", "") for a in allergies
            ],
            has_critical_alerts=has_critical,
            agent_status=AgentStatus.SUCCESS,
        )

        publish_message(
            config.topic_drug_interactions_ready,
            drug_message,
            attributes={"session_id": session_id, "patient_id": patient_id},
        )

        # 7. Emit audit event
        audit = AuditEventMessage(
            session_id=session_id,
            principal=SERVICE_ACCOUNT,
            agent_name=AGENT_NAME,
            action="DRUG_INTERACTION_CHECK",
            resource_type="MedicationRequest,AllergyIntolerance",
            resource_id=patient_id,
            fhir_query=f"patient={patient_id}&resources=MedicationRequest,AllergyIntolerance,Observation",
            outcome="SUCCESS",
        )
        publish_message(config.topic_audit_events, audit)

        result = {
            "session_id": session_id,
            "patient_id": patient_id,
            "status": "SUCCESS",
            "medications_checked": len(med_names),
            "allergies_checked": len(allergies),
            "total_alerts": len(all_alerts),
            "critical_alerts": sum(1 for a in all_alerts if a.severity == AlertSeverity.CRITICAL),
            "high_alerts": sum(1 for a in all_alerts if a.severity == AlertSeverity.HIGH),
            "has_critical_alerts": has_critical,
            "published_to": config.topic_drug_interactions_ready,
        }

        logger.info("Drug interaction check complete: %s", result)
        return result

    except Exception as e:
        logger.error("Drug interaction agent failed: %s", str(e))

        audit = AuditEventMessage(
            session_id=session_id,
            principal=SERVICE_ACCOUNT,
            agent_name=AGENT_NAME,
            action="DRUG_INTERACTION_CHECK",
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

drug_interaction_agent = Agent(
    name=AGENT_NAME,
    model=config.gemini_model,
    description=(
        "Checks drug-drug interactions via RxNorm, allergy conflicts including "
        "cross-reactivity patterns, and lab-based contraindications for current "
        "patient medications. Publishes clinical alerts to the "
        "drug-interactions-ready topic."
    ),
    instruction=(
        "You are the Drug Interaction Agent in a clinical decision support pipeline. "
        "Call run_drug_interaction_check to pull the diagnosis context, query FHIR "
        "for current medications and allergies, check all interactions and "
        "contraindications, and publish the alert results. "
        "Report the session_id, total alerts, and whether any critical alerts were found."
    ),
    tools=[run_drug_interaction_check],
)