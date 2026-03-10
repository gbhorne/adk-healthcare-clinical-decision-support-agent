"""
scripts/validate.py
9-gate validation script for the HC-CDSS rebuild.

Run after setup to confirm all fixes are in place and GCP is correctly configured:
  python scripts/validate.py

Gates:
  1. Config project_id is set (FIX C1 check)
  2. Pub/Sub subscriptions exist — count >= 8 (FIX C3b, M1, M2)
  3. Orchestrator uses vertexai not genai (FIX C2 check)
  4. Orchestrator pulls protocol_msg not hardcoded None (FIX C3 check)
  5. Diagnosis agent redacts PHI before Gemini prompt (FIX H2 check)
  6. audit_logger.write is not replaced with pass (FIX H4 check)
  7. FHIR patients are accessible — 3 of 3 (data readiness)
  8. BigQuery audit_log table exists (infrastructure check)
  9. Cloud DLP responds (live API check)
"""

import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

passed = 0
failed = 0
warnings = 0


def check(label: str, result: bool, detail: str = "", warn: bool = False) -> None:
    global passed, failed, warnings
    if result:
        print(f"  ✓  {label}")
        if detail:
            print(f"     {detail}")
        passed += 1
    elif warn:
        print(f"  ⚠  {label} (warning)")
        if detail:
            print(f"     {detail}")
        warnings += 1
    else:
        print(f"  ✗  {label}")
        if detail:
            print(f"     {detail}")
        failed += 1


# ── Gate 1: Config project_id ─────────────────────────────────────────────────
print("\nGate 1: Config — project_id is set")
try:
    from shared.config import config
    has_project = bool(config.project_id and config.project_id != "healthcare-2026")
    check(
        "GCP_PROJECT_ID is set and not the old project",
        has_project,
        detail=f"project_id = '{config.project_id}'" if has_project else "Set GCP_PROJECT_ID in .env",
    )
    has_protocols_sub = bool(config.sub_orchestrator_protocols)
    check(
        "sub_orchestrator_protocols is defined (FIX C3a)",
        has_protocols_sub,
        detail=f"sub_orchestrator_protocols = '{config.sub_orchestrator_protocols}'",
    )
    has_m1_sub = bool(config.sub_drug_interaction_patient_context)
    check(
        "sub_drug_interaction_patient_context is defined (FIX M1)",
        has_m1_sub,
        detail=f"sub = '{config.sub_drug_interaction_patient_context}'",
    )
    old_project_in_config = "healthcare-2026" in str(config.__dict__)
    check(
        "No 'healthcare-2026' values remain in config (FIX C1)",
        not old_project_in_config,
        detail="Old project ID still present in config" if old_project_in_config else "",
    )
except Exception as e:
    check("Config loads without error", False, detail=str(e))

# ── Gate 2: Pub/Sub subscriptions ─────────────────────────────────────────────
print("\nGate 2: Pub/Sub subscriptions exist")
try:
    from google.cloud import pubsub_v1
    subscriber = pubsub_v1.SubscriberClient()
    project_path = f"projects/{config.project_id}"
    subs = list(subscriber.list_subscriptions(request={"project": project_path}))
    sub_names = {s.name.split("/")[-1] for s in subs}
    subscriber.close()

    required_subs = {
        "diagnosis-agent-sub",
        "protocol-agent-sub",
        "drug-interaction-patient-context-sub",
        "drug-interaction-agent-sub",
        "orchestrator-diagnosis-sub",
        "orchestrator-protocols-sub",
        "orchestrator-agent-sub",
        "audit-agent-sub",
    }
    missing = required_subs - sub_names
    check(
        f"All 8 required subscriptions exist ({len(required_subs) - len(missing)}/{len(required_subs)})",
        len(missing) == 0,
        detail=f"Missing: {missing}" if missing else f"Found: {len(sub_names)} total subs",
    )
    check(
        "orchestrator-protocols-sub exists (FIX C3b)",
        "orchestrator-protocols-sub" in sub_names,
    )
    check(
        "drug-interaction-patient-context-sub exists (FIX M1)",
        "drug-interaction-patient-context-sub" in sub_names,
    )
except Exception as e:
    check("Pub/Sub subscription check", False, detail=str(e), warn=True)

# ── Gate 3: Orchestrator uses vertexai not genai ──────────────────────────────
print("\nGate 3: Orchestrator SDK (FIX C2)")
orchestrator_file = REPO_ROOT / "agents" / "orchestrator" / "agent.py"
content = orchestrator_file.read_text(encoding="utf-8")
check(
    "Orchestrator imports vertexai.generative_models (FIX C2)",
    "from vertexai.generative_models import" in content,
)
check(
    "Orchestrator does NOT import google.generativeai (FIX C2)",
    "import google.generativeai" not in content,
)
code_lines = [l for l in content.splitlines() if not l.strip().startswith("#")]
code_only = "\n".join(code_lines)
check(
    "No genai.configure() call in orchestrator (FIX C2)",
    "genai.configure" not in code_only,
)

# ── Gate 4: Orchestrator pulls protocol_msg (FIX C3) ─────────────────────────
print("\nGate 4: Protocol message is pulled not hardcoded None (FIX C3)")
check(
    "orchestrator does not contain 'protocol_msg = None'",
    "protocol_msg = None" not in code_only,
)
check(
    "orchestrator pulls from sub_orchestrator_protocols",
    "sub_orchestrator_protocols" in content,
)

# ── Gate 5: Diagnosis agent redacts PHI before prompt (FIX H2) ───────────────
print("\nGate 5: Diagnosis agent PHI redaction before prompt (FIX H2)")
diagnosis_file = REPO_ROOT / "agents" / "diagnosis" / "agent.py"
diag_content = diagnosis_file.read_text(encoding="utf-8")
check(
    "_redact_phi_for_prompt() function exists (FIX H2)",
    "_redact_phi_for_prompt" in diag_content,
)
check(
    "redacted_dict passed to _build_diagnosis_prompt not raw snapshot (FIX H2)",
    "redacted_dict" in diag_content and "_build_diagnosis_prompt(redacted_dict" in diag_content,
)

# ── Gate 6: Audit agent Cloud Logging re-enabled (FIX H4) ────────────────────
print("\nGate 6: Audit agent Cloud Logging re-enabled (FIX H4)")
audit_file = REPO_ROOT / "agents" / "audit" / "agent.py"
audit_content = audit_file.read_text(encoding="utf-8")
check(
    "audit_logger.write(event) is present (FIX H4)",
    "audit_logger.write(event)" in audit_content,
)
# Check the write() call is NOT followed immediately by `pass` (original bug)
disabled_pattern = bool(re.search(r"audit_logger\.write\(event\)\s*\n\s*pass", audit_content))
check(
    "Cloud Logging write is not replaced by 'pass' (FIX H4)",
    not disabled_pattern,
    detail="Found 'pass' immediately after audit_logger.write — re-enable it" if disabled_pattern else "",
)

# ── Gate 7: FHIR patients are accessible ─────────────────────────────────────
print("\nGate 7: FHIR synthetic patients accessible")
try:
    import google.auth
    import google.auth.transport.requests
    import requests as http_requests

    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    credentials.refresh(google.auth.transport.requests.Request())
    token = credentials.token

    for patient_id in ["patient-marcus-webb", "patient-diane-okafor", "patient-james-tran"]:
        url = f"{config.fhir_base_url}/Patient/{patient_id}"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/fhir+json"}
        r = http_requests.get(url, headers=headers, timeout=15)
        check(
            f"Patient {patient_id} accessible via FHIR",
            r.status_code == 200,
            detail=f"HTTP {r.status_code}" if r.status_code != 200 else "",
        )
except Exception as e:
    check("FHIR patient access", False, detail=str(e), warn=True)

# ── Gate 8: BigQuery tables exist ────────────────────────────────────────────
print("\nGate 8: BigQuery tables exist")
try:
    from google.cloud import bigquery
    bq = bigquery.Client(project=config.project_id)
    for table_id in [config.bq_audit_table_id, config.bq_sessions_table_id]:
        try:
            bq.get_table(table_id)
            check(f"BigQuery table exists: {table_id.split('.')[-1]}", True)
        except Exception:
            check(f"BigQuery table exists: {table_id.split('.')[-1]}", False,
                  detail=f"Run setup_gcp.sh to create {table_id}")
except Exception as e:
    check("BigQuery access", False, detail=str(e), warn=True)

# ── Gate 9: Cloud DLP responds ───────────────────────────────────────────────
print("\nGate 9: Cloud DLP API responds")
try:
    from google.cloud import dlp_v2
    dlp = dlp_v2.DlpServiceClient()
    parent = f"projects/{config.project_id}/locations/us-central1"
    response = dlp.deidentify_content(
        request={
            "parent": parent,
            "deidentify_config": {
                "info_type_transformations": {
                    "transformations": [{"primitive_transformation": {"replace_with_info_type_config": {}}}]
                }
            },
            "inspect_config": {
                "info_types": [{"name": "PERSON_NAME"}],
                "min_likelihood": dlp_v2.Likelihood.LIKELY,
            },
            "item": {"value": "Patient: John Smith, DOB: 1975-04-12"},
        }
    )
    result_text = response.item.value
    check(
        "Cloud DLP deidentify_content responds",
        True,
        detail=f"Test result: '{result_text[:60]}'",
    )
    phi_found = "[PERSON_NAME]" in result_text
    check(
        "DLP correctly replaces PERSON_NAME token",
        phi_found,
        detail=f"Result: '{result_text}'" if not phi_found else "",
    )
except Exception as e:
    check("Cloud DLP API", False, detail=str(e), warn=True)

# ── Summary ───────────────────────────────────────────────────────────────────
total = passed + failed + warnings
print(f"\n{'=' * 60}")
print(f"Results: {passed}/{total} passed | {failed} failed | {warnings} warnings")
print("=" * 60)

if failed > 0:
    print("\nAddress failed gates before running the pipeline.")
    sys.exit(1)
elif warnings > 0:
    print("\nWarnings are non-blocking. Pipeline should run but verify connectivity.")
else:
    print("\nAll gates passed. Run: adk run cdss_agent")
