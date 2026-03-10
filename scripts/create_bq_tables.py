"""
create_bq_tables.py — Drop and recreate CDSS BigQuery tables with correct schema.

Usage:
    python scripts/create_bq_tables.py
    python scripts/create_bq_tables.py --dry-run

Reads project/dataset config from environment (.env or exports).
Safe to re-run: uses --drop flag logic, prompts before destructive action.
"""

import os
import sys
import argparse
from dotenv import load_dotenv

load_dotenv()

try:
    from google.cloud import bigquery
except ImportError:
    print("ERROR: google-cloud-bigquery not installed. Run: pip install google-cloud-bigquery")
    sys.exit(1)

PROJECT_ID  = os.getenv("GCP_PROJECT_ID", "")
BQ_DATASET  = os.getenv("BQ_DATASET", "cdss_audit")
AUDIT_TABLE = os.getenv("BQ_AUDIT_TABLE", "audit_events")
SESSIONS_TABLE = os.getenv("BQ_SESSIONS_TABLE", "clinical_summaries")

# ── Schema: audit_events ──────────────────────────────────────────────────────
# Matches agents/audit/agent.py :: _build_row()
AUDIT_EVENTS_SCHEMA = [
    bigquery.SchemaField("event_id",            "STRING",    mode="REQUIRED"),
    bigquery.SchemaField("session_id",          "STRING",    mode="REQUIRED"),
    bigquery.SchemaField("timestamp",           "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("principal",           "STRING",    mode="NULLABLE"),
    bigquery.SchemaField("agent_name",          "STRING",    mode="NULLABLE"),
    bigquery.SchemaField("action",              "STRING",    mode="NULLABLE"),
    bigquery.SchemaField("resource_type",       "STRING",    mode="NULLABLE"),
    bigquery.SchemaField("resource_id",         "STRING",    mode="NULLABLE"),
    bigquery.SchemaField("fhir_query",          "STRING",    mode="NULLABLE"),
    bigquery.SchemaField("gemini_prompt_hash",  "STRING",    mode="NULLABLE"),
    bigquery.SchemaField("gemini_model",        "STRING",    mode="NULLABLE"),
    bigquery.SchemaField("gemini_output_hash",  "STRING",    mode="NULLABLE"),
    bigquery.SchemaField("dlp_findings_count",  "INTEGER",   mode="NULLABLE"),
    bigquery.SchemaField("dlp_transformations", "STRING",    mode="NULLABLE"),
    bigquery.SchemaField("outcome",             "STRING",    mode="NULLABLE"),
    bigquery.SchemaField("error_message",       "STRING",    mode="NULLABLE"),
    bigquery.SchemaField("log_version",         "INTEGER",   mode="NULLABLE"),
]

# ── Schema: clinical_summaries ────────────────────────────────────────────────
# Matches agents/orchestrator/agent.py :: _write_session_to_bigquery()
CLINICAL_SUMMARIES_SCHEMA = [
    bigquery.SchemaField("session_id",            "STRING",    mode="REQUIRED"),
    bigquery.SchemaField("patient_id",            "STRING",    mode="REQUIRED"),
    bigquery.SchemaField("generated_at",          "TIMESTAMP", mode="NULLABLE"),
    bigquery.SchemaField("diagnosis_count",       "INTEGER",   mode="NULLABLE"),
    bigquery.SchemaField("alert_count",           "INTEGER",   mode="NULLABLE"),
    bigquery.SchemaField("has_critical_alerts",   "BOOLEAN",   mode="NULLABLE"),
    bigquery.SchemaField("drug_interaction_count","INTEGER",   mode="NULLABLE"),
    bigquery.SchemaField("allergy_conflict_count","INTEGER",   mode="NULLABLE"),
    bigquery.SchemaField("protocol_count",        "INTEGER",   mode="NULLABLE"),
    bigquery.SchemaField("dlp_inspected",         "BOOLEAN",   mode="NULLABLE"),
    bigquery.SchemaField("gemini_model",          "STRING",    mode="NULLABLE"),
    bigquery.SchemaField("firestore_path",        "STRING",    mode="NULLABLE"),
]


def recreate_table(
    client: bigquery.Client,
    dataset_id: str,
    table_name: str,
    schema: list,
    dry_run: bool,
) -> None:
    table_id = f"{PROJECT_ID}.{dataset_id}.{table_name}"
    print(f"\n{'[DRY RUN] ' if dry_run else ''}Processing: {table_id}")

    if not dry_run:
        # Delete existing table if it exists
        client.delete_table(table_id, not_found_ok=True)
        print(f"  Dropped (if existed): {table_id}")

        # Recreate with correct schema
        table = bigquery.Table(table_id, schema=schema)
        table.time_partitioning = bigquery.TimePartitioning(
            type_=bigquery.TimePartitioningType.DAY,
            field="timestamp" if table_name == AUDIT_TABLE else "generated_at",
        )
        client.create_table(table)
        print(f"  Created with {len(schema)} fields + day partitioning ✅")
    else:
        print(f"  Would recreate with {len(schema)} fields:")
        for f in schema:
            print(f"    {f.name:30s} {f.field_type:10s} {f.mode}")


def main():
    parser = argparse.ArgumentParser(description="Recreate CDSS BigQuery tables")
    parser.add_argument("--dry-run", action="store_true", help="Print schema without making changes")
    args = parser.parse_args()

    if not PROJECT_ID:
        print("ERROR: GCP_PROJECT_ID not set. Check your .env file.")
        sys.exit(1)

    print(f"Project : {PROJECT_ID}")
    print(f"Dataset : {BQ_DATASET}")
    print(f"Tables  : {AUDIT_TABLE}, {SESSIONS_TABLE}")

    if not args.dry_run:
        confirm = input("\nThis will DROP and recreate both tables (all data lost). Continue? [y/N] ")
        if confirm.strip().lower() != "y":
            print("Aborted.")
            sys.exit(0)

    client = bigquery.Client(project=PROJECT_ID)

    recreate_table(client, BQ_DATASET, AUDIT_TABLE,    AUDIT_EVENTS_SCHEMA,       args.dry_run)
    recreate_table(client, BQ_DATASET, SESSIONS_TABLE, CLINICAL_SUMMARIES_SCHEMA, args.dry_run)

    if not args.dry_run:
        print("\nDone. Both tables recreated with correct schema.")
        print("Run the CDSS pipeline again — BQ writes should succeed with no errors.")
    else:
        print("\n[DRY RUN complete — no changes made]")


if __name__ == "__main__":
    main()
