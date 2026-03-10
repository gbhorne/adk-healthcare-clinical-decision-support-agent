"""
scripts/setup_pubsub.py
Create all Pub/Sub topics and subscriptions for the HC-CDSS rebuild.

Run once during GCP project setup:
  python scripts/setup_pubsub.py

TOPICS AND SUBSCRIPTIONS
=========================

  patient-context-ready
    ├── diagnosis-agent-sub             (Agent 2)
    ├── protocol-agent-sub              (Agent 3)
    └── drug-interaction-patient-context-sub  (Agent 4 — FIX M1)

  diagnosis-ready
    ├── drug-interaction-agent-sub      (Agent 4 session correlation)
    └── orchestrator-diagnosis-sub      (Agent 5)

  protocols-ready
    └── orchestrator-protocols-sub      (Agent 5 — FIX C3)

  drug-interactions-ready
    └── orchestrator-agent-sub          (Agent 5)

  audit-events
    └── audit-agent-sub                 (Agent 6)

CHANGES FROM ORIGINAL:
  C3b — Added orchestrator-protocols-sub on protocols-ready topic.
        This was the missing subscription that caused protocol_msg = None.
  M1  — Added drug-interaction-patient-context-sub on patient-context-ready.
        Agent 4 now reads patient data from the pipeline instead of
        re-querying FHIR 3 times.
  M2  — This script documents and creates the full topic→subscription mapping
        that was previously undocumented.
"""

import sys
from google.cloud import pubsub_v1
from google.api_core.exceptions import AlreadyExists

# ── Configuration ─────────────────────────────────────────────────────────────

try:
    from shared.config import config
    PROJECT_ID = config.project_id
except Exception:
    import os
    PROJECT_ID = os.getenv("GCP_PROJECT_ID", "")

if not PROJECT_ID:
    print("ERROR: GCP_PROJECT_ID is not set. Add it to your .env file.")
    sys.exit(1)

# ── Topic → Subscription mapping ─────────────────────────────────────────────

TOPICS_AND_SUBSCRIPTIONS = {
    "patient-context-ready": [
        "diagnosis-agent-sub",                    # Agent 2: diagnosis
        "protocol-agent-sub",                     # Agent 3: protocol lookup
        "drug-interaction-patient-context-sub",   # Agent 4: FIX M1 — avoid FHIR re-query
    ],
    "diagnosis-ready": [
        "drug-interaction-agent-sub",             # Agent 4: session correlation
        "orchestrator-diagnosis-sub",             # Agent 5: diagnosis pull
    ],
    "protocols-ready": [
        "orchestrator-protocols-sub",             # Agent 5: FIX C3 — was missing
    ],
    "drug-interactions-ready": [
        "orchestrator-agent-sub",                 # Agent 5: main entry point
    ],
    "audit-events": [
        "audit-agent-sub",                        # Agent 6: audit flush
    ],
}

ACK_DEADLINE_SECONDS = 60
MESSAGE_RETENTION_DURATION = "604800s"   # 7 days


def create_topic(publisher: pubsub_v1.PublisherClient, project_id: str, topic_name: str) -> None:
    topic_path = publisher.topic_path(project_id, topic_name)
    try:
        publisher.create_topic(request={"name": topic_path})
        print(f"  ✓ Created topic: {topic_name}")
    except AlreadyExists:
        print(f"  · Topic already exists: {topic_name}")


def create_subscription(
    subscriber: pubsub_v1.SubscriberClient,
    project_id: str,
    topic_name: str,
    subscription_name: str,
) -> None:
    topic_path = f"projects/{project_id}/topics/{topic_name}"
    subscription_path = subscriber.subscription_path(project_id, subscription_name)

    try:
        subscriber.create_subscription(
            request={
                "name": subscription_path,
                "topic": topic_path,
                "ack_deadline_seconds": ACK_DEADLINE_SECONDS,
                "message_retention_duration": {"seconds": 604800},
                "retry_policy": {
                    "minimum_backoff": {"seconds": 10},
                    "maximum_backoff": {"seconds": 600},
                },
            }
        )
        print(f"      ✓ Created subscription: {subscription_name}")
    except AlreadyExists:
        print(f"      · Subscription already exists: {subscription_name}")


def main():
    print(f"\nSetting up Pub/Sub for project: {PROJECT_ID}")
    print("=" * 60)

    publisher = pubsub_v1.PublisherClient()
    subscriber = pubsub_v1.SubscriberClient()

    total_topics = 0
    total_subs = 0

    for topic_name, subscriptions in TOPICS_AND_SUBSCRIPTIONS.items():
        print(f"\nTopic: {topic_name}")
        create_topic(publisher, PROJECT_ID, topic_name)
        total_topics += 1

        for sub_name in subscriptions:
            create_subscription(subscriber, PROJECT_ID, topic_name, sub_name)
            total_subs += 1

    print(f"\n{'=' * 60}")
    print(f"Done. Topics: {total_topics} | Subscriptions: {total_subs}")
    print("\nVerification command:")
    print(f"  gcloud pubsub subscriptions list --project={PROJECT_ID} --format='table(name,topic)'")

    subscriber.close()


if __name__ == "__main__":
    main()
