#!/usr/bin/env bash
# scripts/setup_gcp.sh
# Full GCP project setup for the HC-CDSS rebuild.
# Run once after creating a new GCP project.
#
# Usage:
#   export GCP_PROJECT_ID=your-new-project-id
#   chmod +x scripts/setup_gcp.sh
#   ./scripts/setup_gcp.sh
#
# Prerequisites:
#   - gcloud CLI installed and authenticated
#   - Billing enabled on the project
#   - Owner or Editor role on the project

set -euo pipefail

PROJECT_ID="${GCP_PROJECT_ID:?GCP_PROJECT_ID must be set}"
REGION="${GCP_LOCATION:-us-central1}"
SA_NAME="cdss-sa"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
BQ_DATASET="cds_audit"
FHIR_DATASET="cds-dataset"
FHIR_STORE="cds-fhir-store"
GCS_BUCKET="${PROJECT_ID}-cdss-data"
FIRESTORE_DB="(default)"

echo "============================================================"
echo "HC-CDSS GCP Setup"
echo "Project: ${PROJECT_ID}"
echo "Region:  ${REGION}"
echo "============================================================"

# ── STEP 1: Enable APIs ───────────────────────────────────────────────────────
echo ""
echo "STEP 1: Enabling APIs..."

gcloud services enable \
  healthcare.googleapis.com \
  pubsub.googleapis.com \
  firestore.googleapis.com \
  bigquery.googleapis.com \
  storage.googleapis.com \
  aiplatform.googleapis.com \
  dlp.googleapis.com \
  secretmanager.googleapis.com \
  discoveryengine.googleapis.com \
  cloudkms.googleapis.com \
  logging.googleapis.com \
  cloudresourcemanager.googleapis.com \
  --project="${PROJECT_ID}"

echo "  ✓ APIs enabled"

# ── STEP 2: Service Account ───────────────────────────────────────────────────
echo ""
echo "STEP 2: Creating service account..."

gcloud iam service-accounts create "${SA_NAME}" \
  --display-name="HC-CDSS Service Account" \
  --project="${PROJECT_ID}" 2>/dev/null || echo "  · Service account already exists"

IAM_ROLES=(
  "roles/healthcare.fhirResourceEditor"
  "roles/pubsub.editor"
  "roles/datastore.user"
  "roles/bigquery.dataEditor"
  "roles/bigquery.jobUser"
  "roles/storage.objectAdmin"
  "roles/aiplatform.user"
  "roles/dlp.user"
  "roles/secretmanager.secretAccessor"
  "roles/logging.logWriter"
  "roles/cloudkms.cryptoKeyEncrypterDecrypter"
)

for ROLE in "${IAM_ROLES[@]}"; do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="${ROLE}" \
    --quiet
  echo "  ✓ Granted ${ROLE}"
done

# Download service account key to use with ADC locally
gcloud iam service-accounts keys create "./sa-key.json" \
  --iam-account="${SA_EMAIL}" \
  --project="${PROJECT_ID}" 2>/dev/null || echo "  · SA key already exists"
echo "  ✓ SA key created: sa-key.json"
echo "  ⚠  Add sa-key.json to .gitignore — never commit this file"

# ── STEP 3: Cloud Healthcare API + FHIR Store ─────────────────────────────────
echo ""
echo "STEP 3: Creating Cloud Healthcare FHIR store..."

gcloud healthcare datasets create "${FHIR_DATASET}" \
  --location="${REGION}" \
  --project="${PROJECT_ID}" 2>/dev/null || echo "  · Dataset already exists"
echo "  ✓ Healthcare dataset: ${FHIR_DATASET}"

gcloud healthcare fhir-stores create "${FHIR_STORE}" \
  --dataset="${FHIR_DATASET}" \
  --location="${REGION}" \
  --version=R4 \
  --project="${PROJECT_ID}" 2>/dev/null || echo "  · FHIR store already exists"
echo "  ✓ FHIR store: ${FHIR_STORE} (R4)"

# Grant service account Healthcare FHIR permissions
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/healthcare.fhirResourceEditor" \
  --quiet

# ── STEP 4: Pub/Sub (handled by setup_pubsub.py) ─────────────────────────────
echo ""
echo "STEP 4: Pub/Sub topics and subscriptions..."
echo "  Run: python scripts/setup_pubsub.py"
echo "  (Skipped here — run separately after setting GOOGLE_APPLICATION_CREDENTIALS)"

# ── STEP 5: BigQuery ─────────────────────────────────────────────────────────
echo ""
echo "STEP 5: Creating BigQuery dataset and tables..."

bq mk --dataset \
  --location="${REGION}" \
  --description="HC-CDSS audit and session data" \
  "${PROJECT_ID}:${BQ_DATASET}" 2>/dev/null || echo "  · BigQuery dataset already exists"
echo "  ✓ BigQuery dataset: ${BQ_DATASET}"

# audit_log table
bq mk --table \
  "${PROJECT_ID}:${BQ_DATASET}.audit_log" \
  "event_id:STRING,session_id:STRING,timestamp:STRING,principal:STRING,agent_name:STRING,action:STRING,resource_type:STRING,resource_id:STRING,fhir_query:STRING,gemini_prompt_hash:STRING,gemini_model:STRING,gemini_output_hash:STRING,dlp_findings_count:INTEGER,dlp_transformations:STRING,outcome:STRING,error_message:STRING,log_version:STRING" \
  2>/dev/null || echo "  · audit_log table already exists"
echo "  ✓ BigQuery table: audit_log"

# sessions table
bq mk --table \
  "${PROJECT_ID}:${BQ_DATASET}.sessions" \
  "session_id:STRING,patient_id:STRING,generated_at:STRING,diagnosis_count:INTEGER,alert_count:INTEGER,has_critical_alerts:BOOLEAN,drug_interaction_count:INTEGER,allergy_conflict_count:INTEGER,protocol_count:INTEGER,dlp_inspected:BOOLEAN,gemini_model:STRING,firestore_path:STRING" \
  2>/dev/null || echo "  · sessions table already exists"
echo "  ✓ BigQuery table: sessions"

# ── STEP 6: Firestore ─────────────────────────────────────────────────────────
echo ""
echo "STEP 6: Enabling Firestore (Native mode)..."

gcloud firestore databases create \
  --location="${REGION}" \
  --type=firestore-native \
  --project="${PROJECT_ID}" 2>/dev/null || echo "  · Firestore already enabled"
echo "  ✓ Firestore Native mode enabled"

# ── STEP 7: Cloud Storage ─────────────────────────────────────────────────────
echo ""
echo "STEP 7: Creating Cloud Storage bucket..."

gcloud storage buckets create "gs://${GCS_BUCKET}" \
  --location="${REGION}" \
  --project="${PROJECT_ID}" \
  --uniform-bucket-level-access 2>/dev/null || echo "  · GCS bucket already exists"
echo "  ✓ GCS bucket: ${GCS_BUCKET}"

# Create protocols prefix placeholder
echo "clinical-protocols/" | gcloud storage cp - "gs://${GCS_BUCKET}/.keep" \
  --project="${PROJECT_ID}" 2>/dev/null || true

# ── STEP 8: Cloud DLP Templates ──────────────────────────────────────────────
echo ""
echo "STEP 8: DLP templates..."
echo "  Creating DLP inspect template..."

curl -s -X POST \
  "https://dlp.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/inspectTemplates" \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "Content-Type: application/json" \
  -d "{
    \"inspectTemplate\": {
      \"displayName\": \"HC-CDSS PHI Inspect\",
      \"inspectConfig\": {
        \"infoTypes\": [
          {\"name\": \"PERSON_NAME\"}, {\"name\": \"DATE_OF_BIRTH\"},
          {\"name\": \"US_SOCIAL_SECURITY_NUMBER\"}, {\"name\": \"PHONE_NUMBER\"},
          {\"name\": \"EMAIL_ADDRESS\"}, {\"name\": \"STREET_ADDRESS\"},
          {\"name\": \"MEDICAL_RECORD_NUMBER\"}, {\"name\": \"US_HEALTHCARE_NPI\"},
          {\"name\": \"AGE\"}, {\"name\": \"DATE\"}
        ],
        \"minLikelihood\": \"LIKELY\",
        \"includeQuote\": false
      }
    },
    \"templateId\": \"cds-phi-inspect\"
  }" > /tmp/dlp_inspect_response.json 2>&1

echo "  ✓ DLP inspect template created (check /tmp/dlp_inspect_response.json)"

# ── Final: Update .env file reminder ─────────────────────────────────────────
echo ""
echo "============================================================"
echo "SETUP COMPLETE"
echo "============================================================"
echo ""
echo "Next steps:"
echo ""
echo "1. Update your .env file:"
cat << ENV
   GCP_PROJECT_ID=${PROJECT_ID}
   GCP_LOCATION=${REGION}
   GCS_BUCKET=${GCS_BUCKET}
   FHIR_DATASET_ID=${FHIR_DATASET}
   FHIR_STORE_ID=${FHIR_STORE}
ENV
echo ""
echo "2. Set Application Default Credentials:"
echo "   export GOOGLE_APPLICATION_CREDENTIALS=./sa-key.json"
echo ""
echo "3. Run Pub/Sub setup:"
echo "   python scripts/setup_pubsub.py"
echo ""
echo "4. Load synthetic patients into FHIR:"
echo "   python scripts/load_fhir_patients.py"
echo ""
echo "5. Start the ADK web UI:"
echo "   adk run cdss_agent"
echo ""
