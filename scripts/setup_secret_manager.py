"""
setup_secret_manager.py
Migrate HC-CDSS credentials from .env / sa-key.json to Google Secret Manager.

WHY SECRET MANAGER:
  - sa-key.json loaded directly from disk is a security risk — the key can be
    accidentally committed to git or exposed in container images.
  - Secret Manager provides audit logging, version rotation, and IAM-scoped
    access control for every secret access.
  - In Cloud Run, attach the service account directly and remove
    GOOGLE_APPLICATION_CREDENTIALS entirely; use Secret Manager only for
    application-level secrets (API keys, webhook tokens, etc.).

SECRETS CREATED:
  - cdss-kms-key-name        : Full KMS key resource name
  - cdss-gemini-model        : Gemini model string (so it can be rotated without redeploy)
  - cdss-vertex-search-id    : Vertex AI Search engine ID

USAGE:
  python scripts/setup_secret_manager.py

After running, these values can be loaded via config._get_secret() instead of
reading from .env. The _get_secret() helper already falls back to .env when
Secret Manager is unavailable, so no code changes are required.

NOTE: This script does NOT migrate sa-key.json itself into Secret Manager.
For Cloud Run deployment, remove GOOGLE_APPLICATION_CREDENTIALS and attach
the service account at the Cloud Run service level (--service-account flag).
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv()

from shared.config import config
config.validate()

PROJECT_ID = config.project_id

SECRETS = {
    "cdss-kms-key-name": os.getenv("KMS_KEY_NAME", ""),
    "cdss-gemini-model": os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
    "cdss-vertex-search-id": os.getenv("VERTEX_AI_SEARCH_ENGINE_ID", "cds-clinical-protocols"),
}


def get_client():
    from google.cloud import secretmanager
    return secretmanager.SecretManagerServiceClient()


def upsert_secret(client, secret_id: str, value: str) -> str:
    """Create or update a secret. Returns the secret resource name."""
    if not value:
        print(f"  SKIP {secret_id} — value is empty, set in .env first")
        return ""

    parent = f"projects/{PROJECT_ID}"
    secret_name = f"{parent}/secrets/{secret_id}"

    # Create secret if it does not exist
    try:
        client.get_secret(request={"name": secret_name})
        print(f"  EXISTS {secret_id}")
    except Exception:
        client.create_secret(
            request={
                "parent": parent,
                "secret_id": secret_id,
                "secret": {"replication": {"automatic": {}}},
            }
        )
        print(f"  CREATED {secret_id}")

    # Add new version with current value
    response = client.add_secret_version(
        request={
            "parent": secret_name,
            "payload": {"data": value.encode("UTF-8")},
        }
    )
    print(f"  VERSION  {response.name}")
    return secret_name


def main():
    print(f"\nMigrating secrets to Secret Manager — project: {PROJECT_ID}\n")
    client = get_client()

    created = []
    for secret_id, value in SECRETS.items():
        name = upsert_secret(client, secret_id, value)
        if name:
            created.append((secret_id, name))

    print("\n" + "=" * 70)
    if created:
        print("Secrets created/updated. Access in code via config._get_secret():")
        for secret_id, name in created:
            print(f"  {secret_id:35s} {name}")
        print()
        print("For Cloud Run deployment:")
        print("  1. Attach service account: --service-account cdss-sa@PROJECT.iam.gserviceaccount.com")
        print("  2. Remove GOOGLE_APPLICATION_CREDENTIALS from environment")
        print("  3. Grant Secret Manager accessor role to the service account:")
        print(f"     gcloud secrets add-iam-policy-binding cdss-kms-key-name \\")
        print(f"       --member=serviceAccount:cdss-sa@{PROJECT_ID}.iam.gserviceaccount.com \\")
        print(f"       --role=roles/secretmanager.secretAccessor")
    else:
        print("No secrets were created — check .env values above.")
    print("=" * 70)


if __name__ == "__main__":
    main()
