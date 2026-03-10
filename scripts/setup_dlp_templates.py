"""
setup_dlp_templates.py
Create named Cloud DLP inspect and deidentify templates for the HC-CDSS pipeline.

WHY NAMED TEMPLATES:
  - Inline DLP config is duplicated across 3 agents (diagnosis, protocol_lookup,
    orchestrator). A change to PHI types requires editing all 3 files.
  - Named templates are versioned in the DLP API, auditable, and reusable.
  - Template resource names are stored in .env and loaded via GCPConfig.

TEMPLATES CREATED:
  - cdss-phi-inspect    : InspectTemplate — PHI info_types for CDSS pipeline
  - cdss-phi-deidentify : DeidentifyTemplate — CryptoReplaceFfxFpeConfig with KMS

USAGE:
  python scripts/setup_dlp_templates.py

After running, add to .env:
  DLP_INSPECT_TEMPLATE=projects/YOUR-PROJECT/locations/global/inspectTemplates/cdss-phi-inspect
  DLP_DEIDENTIFY_TEMPLATE=projects/YOUR-PROJECT/locations/global/deidentifyTemplates/cdss-phi-deidentify
"""

import os
import sys
from pathlib import Path

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from shared.config import config

config.validate()

PROJECT_ID = config.project_id
KMS_KEY = config.kms_key_name
LOCATION = "global"   # DLP templates use global location for cross-region access

# ── PHI info_types — shared across all 3 pipeline intercept points ─────────
PHI_INFO_TYPES = [
    {"name": "PERSON_NAME"},
    {"name": "DATE_OF_BIRTH"},
    {"name": "DATE"},
    {"name": "AGE"},
    {"name": "US_SOCIAL_SECURITY_NUMBER"},
    {"name": "MEDICAL_RECORD_NUMBER"},
    {"name": "PHONE_NUMBER"},
    {"name": "EMAIL_ADDRESS"},
    {"name": "STREET_ADDRESS"},
    {"name": "LOCATION"},
    {"name": "US_HEALTHCARE_NPI"},
    {"name": "US_DEA_NUMBER"},
    {"name": "PASSPORT"},
    {"name": "US_DRIVERS_LICENSE_NUMBER"},
    {"name": "US_BANK_ROUTING_MICR"},
    {"name": "CREDIT_CARD_NUMBER"},
    {"name": "IP_ADDRESS"},
    {"name": "MAC_ADDRESS"},
]

TEMPLATE_PARENT = f"projects/{PROJECT_ID}/locations/{LOCATION}"
INSPECT_TEMPLATE_ID = "cdss-phi-inspect"
DEIDENTIFY_TEMPLATE_ID = "cdss-phi-deidentify"


def get_dlp_client():
    from google.cloud import dlp_v2
    return dlp_v2.DlpServiceClient()


def create_inspect_template(client) -> str:
    """Create or overwrite the DLP inspect template. Returns resource name."""
    inspect_template = {
        "display_name": "CDSS PHI Inspect Template",
        "description": (
            "Detects PHI categories used by the HC-CDSS pipeline at all three "
            "DLP intercept points: post-FHIR fetch, post-Gemini diagnosis output, "
            "and pre-Firestore/BigQuery write."
        ),
        "inspect_config": {
            "info_types": PHI_INFO_TYPES,
            "min_likelihood": "LIKELIHOOD_UNSPECIFIED",
            "limits": {
                "max_findings_per_item": 100,
            },
            "include_quote": True,
        },
    }

    # Delete existing template if present (idempotent re-run)
    resource_name = f"{TEMPLATE_PARENT}/inspectTemplates/{INSPECT_TEMPLATE_ID}"
    try:
        client.delete_inspect_template(request={"name": resource_name})
        print(f"  Deleted existing inspect template: {INSPECT_TEMPLATE_ID}")
    except Exception:
        pass  # Did not exist, that is fine

    response = client.create_inspect_template(
        request={
            "parent": TEMPLATE_PARENT,
            "inspect_template": inspect_template,
            "template_id": INSPECT_TEMPLATE_ID,
        }
    )
    print(f"  Created inspect template: {response.name}")
    return response.name


def create_deidentify_template(client) -> str:
    """Create or overwrite the DLP deidentify template. Returns resource name."""
    if not KMS_KEY:
        print(
            "  WARNING: KMS_KEY_NAME is not set in .env. "
            "Creating deidentify template with REPLACE transformation instead of "
            "CryptoReplaceFfxFpe. Set KMS_KEY_NAME and re-run to enable encryption."
        )
        primitive_transformation = {
            "replace_with_info_type_config": {}
        }
    else:
        # CryptoReplaceFfxFpe: format-preserving encryption using KMS key
        # Produces deterministic surrogates — same PHI value always maps to same token
        primitive_transformation = {
            "crypto_replace_ffx_fpe_config": {
                "crypto_key": {
                    "kms_wrapped": {
                        "wrapped_key": b"",    # Placeholder — DLP resolves via KMS key name
                        "crypto_key_name": KMS_KEY,
                    }
                },
                "common_alphabet": "ALPHA_NUMERIC",
                "surrogate_info_type": {"name": "PHI_TOKEN"},
            }
        }

    deidentify_template = {
        "display_name": "CDSS PHI Deidentify Template",
        "description": (
            "Pseudonymizes PHI in HC-CDSS pipeline outputs using format-preserving "
            "encryption (CryptoReplaceFfxFpe) keyed by the CDSS KMS key. "
            "Applied at Moments 1, 2, and 3 in the pipeline."
        ),
        "deidentify_config": {
            "info_type_transformations": {
                "transformations": [
                    {
                        "info_types": PHI_INFO_TYPES,
                        "primitive_transformation": primitive_transformation,
                    }
                ]
            }
        },
    }

    resource_name = f"{TEMPLATE_PARENT}/deidentifyTemplates/{DEIDENTIFY_TEMPLATE_ID}"
    try:
        client.delete_deidentify_template(request={"name": resource_name})
        print(f"  Deleted existing deidentify template: {DEIDENTIFY_TEMPLATE_ID}")
    except Exception:
        pass

    response = client.create_deidentify_template(
        request={
            "parent": TEMPLATE_PARENT,
            "deidentify_template": deidentify_template,
            "template_id": DEIDENTIFY_TEMPLATE_ID,
        }
    )
    print(f"  Created deidentify template: {response.name}")
    return response.name


def main():
    print(f"\nCreating DLP templates in project: {PROJECT_ID}")
    print(f"Location: {LOCATION}")
    print(f"KMS key: {KMS_KEY or '(not set — using REPLACE fallback)'}")
    print()

    client = get_dlp_client()

    print("Creating inspect template...")
    inspect_name = create_inspect_template(client)

    print("\nCreating deidentify template...")
    deidentify_name = create_deidentify_template(client)

    print("\n" + "=" * 70)
    print("Templates created. Add these to your .env file:")
    print()
    print(f"DLP_INSPECT_TEMPLATE={inspect_name}")
    print(f"DLP_DEIDENTIFY_TEMPLATE={deidentify_name}")
    print()
    print("Then the pipeline agents will use named templates instead of")
    print("inline config. No agent code changes required when PHI types change.")
    print("=" * 70)


if __name__ == "__main__":
    main()
