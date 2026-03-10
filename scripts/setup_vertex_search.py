"""
setup_vertex_search.py — Create Vertex AI Search engine and load clinical protocols.

Steps:
  1. Upload protocol JSON files to GCS
  2. Create Discovery Engine data store
  3. Import documents from GCS
  4. Create search engine pointing at the data store
  5. Grant service account discoveryengine.viewer role

Usage:
    python scripts/setup_vertex_search.py
    python scripts/setup_vertex_search.py --check   # just verify status
"""

import os
import sys
import json
import time
import argparse
from pathlib import Path
from dotenv import load_dotenv

# Load .env but override GOOGLE_APPLICATION_CREDENTIALS so this script
# always runs as the personal ADC account (needed for discoveryengine.admin).
# The sa-key is used by the pipeline agents at runtime, not for setup.
load_dotenv()
os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)

try:
    from google.cloud import storage
    from google.cloud import discoveryengine_v1beta as discoveryengine
    import google.auth
    import subprocess
except ImportError as e:
    print(f"ERROR: Missing dependency: {e}")
    print("Run: pip install google-cloud-storage google-cloud-discoveryengine")
    sys.exit(1)

PROJECT_ID      = os.getenv("GCP_PROJECT_ID", "")
LOCATION        = "global"
BUCKET          = os.getenv("GCS_BUCKET", "")
ENGINE_ID       = os.getenv("VERTEX_AI_SEARCH_ENGINE_ID", "cds-clinical-protocols")
DATA_STORE_ID   = "cds-protocols-datastore"
COLLECTION      = "default_collection"
SA_EMAIL        = f"cdss-sa@{PROJECT_ID}.iam.gserviceaccount.com"
PROTOCOLS_DIR   = Path(__file__).parent.parent / "data" / "protocols"
GCS_PREFIX      = "protocols"


def upload_protocols_to_gcs() -> list[str]:
    """Upload all protocol JSON files to GCS and return their URIs."""
    print("\n── Step 1: Upload protocol documents to GCS ─────────────────")
    client = storage.Client(project=PROJECT_ID)
    bucket = client.bucket(BUCKET)
    uris = []

    for json_file in sorted(PROTOCOLS_DIR.glob("*.json")):
        blob_name = f"{GCS_PREFIX}/{json_file.name}"
        blob = bucket.blob(blob_name)
        blob.upload_from_filename(str(json_file), content_type="application/json")
        uri = f"gs://{BUCKET}/{blob_name}"
        uris.append(uri)
        print(f"  Uploaded: {uri}")

    print(f"  {len(uris)} protocol documents uploaded ✅")
    return uris


def create_data_store(client: discoveryengine.DataStoreServiceClient) -> str:
    """Create the Discovery Engine data store. Returns data store name."""
    print("\n── Step 2: Create Discovery Engine data store ───────────────")

    parent = f"projects/{PROJECT_ID}/locations/{LOCATION}/collections/{COLLECTION}"
    data_store_name = f"{parent}/dataStores/{DATA_STORE_ID}"

    # Check if already exists
    try:
        existing = client.get_data_store(name=data_store_name)
        print(f"  Data store already exists: {existing.name}")
        return data_store_name
    except Exception:
        pass

    data_store = discoveryengine.DataStore(
        display_name="CDS Clinical Protocols",
        industry_vertical=discoveryengine.IndustryVertical.GENERIC,
        content_config=discoveryengine.DataStore.ContentConfig.CONTENT_REQUIRED,
        solution_types=[discoveryengine.SolutionType.SOLUTION_TYPE_SEARCH],
    )

    operation = client.create_data_store(
        parent=parent,
        data_store=data_store,
        data_store_id=DATA_STORE_ID,
    )

    print(f"  Creating data store (operation: {operation.operation.name})...")
    result = operation.result(timeout=120)
    print(f"  Data store created: {result.name} ✅")
    return data_store_name


def import_documents(data_store_name: str) -> None:
    """Import protocol JSON documents from GCS into the data store."""
    print("\n── Step 3: Import protocol documents from GCS ───────────────")

    client = discoveryengine.DocumentServiceClient()
    gcs_source = discoveryengine.GcsSource(
        input_uris=[f"gs://{BUCKET}/{GCS_PREFIX}/*.json"],
        data_schema="custom",
    )

    request = discoveryengine.ImportDocumentsRequest(
        parent=f"{data_store_name}/branches/default_branch",
        gcs_source=gcs_source,
        reconciliation_mode=discoveryengine.ImportDocumentsRequest.ReconciliationMode.FULL,
    )

    operation = client.import_documents(request=request)
    print(f"  Import operation started: {operation.operation.name}")
    print("  Waiting for import to complete (may take 2-5 minutes)...")

    result = operation.result(timeout=300)
    print(f"  Import complete ✅")
    if hasattr(result, 'error_samples') and result.error_samples:
        print(f"  Warnings: {len(result.error_samples)} errors during import")
        for err in result.error_samples[:3]:
            print(f"    {err}")


def create_search_engine(
    engine_client: discoveryengine.EngineServiceClient,
    data_store_name: str,
) -> None:
    """Create the search engine pointing at the data store."""
    print("\n── Step 4: Create search engine ─────────────────────────────")

    parent = f"projects/{PROJECT_ID}/locations/{LOCATION}/collections/{COLLECTION}"
    engine_name = f"{parent}/engines/{ENGINE_ID}"

    # Check if already exists
    try:
        existing = engine_client.get_engine(name=engine_name)
        print(f"  Search engine already exists: {existing.name}")
        return
    except Exception:
        pass

    engine = discoveryengine.Engine(
        display_name="CDS Clinical Protocols Search",
        solution_type=discoveryengine.SolutionType.SOLUTION_TYPE_SEARCH,
        data_store_ids=[DATA_STORE_ID],
        search_engine_config=discoveryengine.Engine.SearchEngineConfig(
            search_tier=discoveryengine.SearchTier.SEARCH_TIER_STANDARD,
        ),
    )

    operation = engine_client.create_engine(
        parent=parent,
        engine=engine,
        engine_id=ENGINE_ID,
    )

    print(f"  Creating search engine (operation: {operation.operation.name})...")
    result = operation.result(timeout=120)
    print(f"  Search engine created: {result.name} ✅")


def grant_iam_role() -> None:
    """Grant discoveryengine.viewer to the CDSS service account."""
    print("\n── Step 5: Grant IAM role to service account ────────────────")

    cmd = [
        "gcloud", "projects", "add-iam-policy-binding", PROJECT_ID,
        f"--member=serviceAccount:{SA_EMAIL}",
        "--role=roles/discoveryengine.viewer",
        "--quiet",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        print(f"  Granted roles/discoveryengine.viewer to {SA_EMAIL} ✅")
    else:
        print(f"  WARNING: IAM grant failed: {result.stderr}")
        print(f"  Run manually: {' '.join(cmd)}")


def check_status() -> None:
    """Check current status of data store and engine."""
    print("\n── Status Check ──────────────────────────────────────────────")

    engine_client = discoveryengine.EngineServiceClient()
    parent = f"projects/{PROJECT_ID}/locations/{LOCATION}/collections/{COLLECTION}"

    try:
        engine = engine_client.get_engine(
            name=f"{parent}/engines/{ENGINE_ID}"
        )
        print(f"  Engine:     {engine.name} ✅")
        print(f"  Created:    {engine.create_time}")
    except Exception as e:
        print(f"  Engine:     NOT FOUND — {e}")

    ds_client = discoveryengine.DataStoreServiceClient()
    try:
        ds = ds_client.get_data_store(
            name=f"{parent}/dataStores/{DATA_STORE_ID}"
        )
        print(f"  Data store: {ds.name} ✅")
    except Exception as e:
        print(f"  Data store: NOT FOUND — {e}")


def main():
    parser = argparse.ArgumentParser(description="Set up Vertex AI Search for CDSS protocols")
    parser.add_argument("--check", action="store_true", help="Check status only")
    args = parser.parse_args()

    if not PROJECT_ID:
        print("ERROR: GCP_PROJECT_ID not set in .env")
        sys.exit(1)
    if not BUCKET:
        print("ERROR: GCS_BUCKET not set in .env")
        sys.exit(1)

    print(f"Project:    {PROJECT_ID}")
    print(f"Bucket:     {BUCKET}")
    print(f"Engine ID:  {ENGINE_ID}")
    print(f"Data store: {DATA_STORE_ID}")

    if args.check:
        check_status()
        return

    # Run setup
    upload_protocols_to_gcs()

    ds_client = discoveryengine.DataStoreServiceClient()
    data_store_name = create_data_store(ds_client)

    import_documents(data_store_name)

    engine_client = discoveryengine.EngineServiceClient()
    create_search_engine(engine_client, data_store_name)

    grant_iam_role()

    print("\n── Setup Complete ────────────────────────────────────────────")
    print("NOTE: Allow 10-15 minutes for documents to be indexed before searching.")
    print("Then re-run the CDSS pipeline — protocols_found should be > 0.")
    print("\nAdd to your .env:")
    print(f"  VERTEX_AI_SEARCH_ENGINE_ID={ENGINE_ID}")


if __name__ == "__main__":
    main()
