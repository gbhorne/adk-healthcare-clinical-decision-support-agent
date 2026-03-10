"""
scripts/load_fhir_patients.py
Load synthetic patient FHIR bundles into the Cloud Healthcare FHIR store.
Uses POST with _id parameter to create resources with specific IDs.
"""

import json
import os
import sys
from pathlib import Path

import google.auth
import google.auth.transport.requests
import requests

try:
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from shared.config import config
    PROJECT_ID = config.project_id
    REGION = config.location
    DATASET_ID = config.dataset_id
    FHIR_STORE_ID = config.fhir_store_id
except Exception:
    PROJECT_ID = os.getenv("GCP_PROJECT_ID", "")
    REGION = os.getenv("GCP_LOCATION", "us-central1")
    DATASET_ID = os.getenv("FHIR_DATASET_ID", "cdss-dataset")
    FHIR_STORE_ID = os.getenv("FHIR_STORE_ID", "cdss-fhir-store")

if not PROJECT_ID:
    print("ERROR: GCP_PROJECT_ID not set.")
    sys.exit(1)

FHIR_BASE = (
    f"https://healthcare.googleapis.com/v1/projects/{PROJECT_ID}"
    f"/locations/{REGION}/datasets/{DATASET_ID}"
    f"/fhirStores/{FHIR_STORE_ID}/fhir"
)

DATA_DIR = Path(__file__).parent.parent / "data" / "synthetic"

PATIENT_FILES = [
    "patient-marcus-webb.json",
    "patient-diane-okafor.json",
    "patient-james-tran.json",
    "patient-sofia-reyes.json",
    "patient-david-kim.json",
    "patient-amara-osei.json",
    "patient-robert-chen.json",
    "patient-priya-patel.json",
    "patient-thomas-okafor.json",
    "patient-linda-marsh.json",
]

RESOURCE_ORDER = [
    "Patient", "Encounter", "Condition", "AllergyIntolerance",
    "MedicationRequest", "Observation"
]


def get_auth_token() -> str:
    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    credentials.refresh(google.auth.transport.requests.Request())
    return credentials.token


def upsert_resource(resource: dict, token: str) -> tuple[bool, str]:
    """
    Upsert a FHIR resource using conditional create:
    POST /fhir/ResourceType?_id=xxx
    - Creates if not exists (201)
    - Returns existing if found (200 with search bundle, we ignore and move on)
    Then updates with PUT /fhir/ResourceType/xxx
    """
    rt = resource.get("resourceType")
    rid = resource.get("id")
    if not rt or not rid:
        return False, "Missing resourceType or id"

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/fhir+json",
        "Accept": "application/fhir+json",
    }

    # Step 1: Try conditional create via POST with If-None-Exist
    post_url = f"{FHIR_BASE}/{rt}"
    post_headers = {**headers, "If-None-Exist": f"_id={rid}"}
    resp = requests.post(post_url, json=resource, headers=post_headers, timeout=30)

    if resp.status_code in (200, 201):
        return True, f"created HTTP {resp.status_code}"

    # Step 2: If 412 (already exists) or any error, try direct PUT update
    put_url = f"{FHIR_BASE}/{rt}/{rid}"
    resp = requests.put(put_url, json=resource, headers=headers, timeout=30)

    if resp.status_code in (200, 201):
        return True, f"updated HTTP {resp.status_code}"

    try:
        err = resp.json()
        diag = err.get("issue", [{}])[0].get("diagnostics", "")
    except Exception:
        diag = resp.text[:200]
    return False, f"HTTP {resp.status_code}: {diag}"


def load_patient_bundle(bundle_file: Path) -> tuple[bool, int, int]:
    with open(bundle_file) as f:
        bundle = json.load(f)

    entries = bundle.get("entry", [])

    def sort_key(entry):
        rt = entry.get("resource", {}).get("resourceType", "ZZZ")
        try:
            return RESOURCE_ORDER.index(rt)
        except ValueError:
            return 99

    entries_sorted = sorted(entries, key=sort_key)
    token = get_auth_token()
    successes = 0
    failures = 0

    for i, entry in enumerate(entries_sorted):
        resource = entry.get("resource", {})
        rt = resource.get("resourceType", "Unknown")
        rid = resource.get("id", "?")

        if i % 5 == 0:
            token = get_auth_token()

        ok, msg = upsert_resource(resource, token)
        if ok:
            print(f"    ✓ {rt}/{rid} — {msg}")
            successes += 1
        else:
            print(f"    ✗ {rt}/{rid} — {msg}")
            failures += 1

    return failures == 0, successes, failures


def main():
    print(f"\nLoading synthetic patients into FHIR store")
    print(f"Project:    {PROJECT_ID}")
    print(f"FHIR store: {DATASET_ID}/{FHIR_STORE_ID}")
    print("=" * 60)

    token = get_auth_token()
    test_resp = requests.get(
        f"{FHIR_BASE}/Patient?_count=1",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/fhir+json"},
        timeout=15
    )
    if test_resp.status_code != 200:
        print(f"ERROR: Cannot reach FHIR store. HTTP {test_resp.status_code}")
        print(test_resp.text[:300])
        sys.exit(1)
    print(f"FHIR store reachable ✓\n")

    results = []
    for filename in PATIENT_FILES:
        bundle_path = DATA_DIR / filename
        patient_id = filename.replace(".json", "")

        print(f"Loading: {filename}")
        if not bundle_path.exists():
            print(f"  ERROR: File not found: {bundle_path}")
            results.append((patient_id, False))
            continue

        ok, successes, failures = load_patient_bundle(bundle_path)
        results.append((patient_id, ok))
        print(f"  → {successes} succeeded, {failures} failed\n")

    print("=" * 60)
    print("Summary:")
    for patient_id, ok in results:
        print(f"  {'✓' if ok else '✗'} {patient_id}")

    if all(ok for _, ok in results):
        print("\nAll patients loaded successfully.")
        print("Next: python scripts/validate.py")
    else:
        print("\nSome patients failed. Check errors above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
