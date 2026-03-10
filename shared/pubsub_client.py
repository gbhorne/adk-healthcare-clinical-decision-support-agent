"""
shared/pubsub_client.py
Pub/Sub helpers used by every agent to publish and pull messages.
All messages are JSON-serialized Pydantic models.
"""

import json
import logging
from typing import Optional, Type, TypeVar
from google.cloud import pubsub_v1
from google.api_core import retry
from pydantic import BaseModel

from shared.config import config

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


# ── Publisher ─────────────────────────────────────────────────────────────────

class CDSPublisher:
    """
    Thin wrapper around google-cloud-pubsub PublisherClient.
    Serializes Pydantic models to JSON and publishes to a topic.
    """

    def __init__(self):
        self._client = pubsub_v1.PublisherClient()

    def publish(
        self,
        topic_name: str,
        message: BaseModel,
        attributes: Optional[dict[str, str]] = None,
    ) -> str:
        """
        Publish a Pydantic model as a JSON message to a Pub/Sub topic.

        Args:
            topic_name: Short topic name (e.g. "patient-context-ready")
            message:    Any Pydantic BaseModel instance
            attributes: Optional key-value metadata attached to the message

        Returns:
            Published message ID
        """
        topic_path = self._client.topic_path(config.project_id, topic_name)
        payload = message.model_dump_json().encode("utf-8")
        attrs = attributes or {}

        future = self._client.publish(topic_path, data=payload, **attrs)
        message_id = future.result(timeout=30)

        logger.info(
            "Published to %s | message_id=%s | session_id=%s",
            topic_name,
            message_id,
            attrs.get("session_id", "unknown"),
        )
        return message_id

    def close(self):
        self._client.transport.close()


# ── Subscriber ────────────────────────────────────────────────────────────────

class CDSSubscriber:
    """
    Synchronous pull subscriber.
    Pulls exactly one message per call, deserializes it into a Pydantic model,
    and acks it only after successful processing.
    """

    def __init__(self):
        self._client = pubsub_v1.SubscriberClient()

    def pull_one(
        self,
        subscription_name: str,
        model_class: Type[T],
        timeout: float = 30.0,
    ) -> Optional[T]:
        """
        Pull one message from a subscription and deserialize into model_class.

        Args:
            subscription_name: Short subscription name (e.g. "diagnosis-agent-sub")
            model_class:       Pydantic model class to deserialize into
            timeout:           How long to wait for a message (seconds)

        Returns:
            Deserialized model instance, or None if no message available
        """
        subscription_path = self._client.subscription_path(
            config.project_id, subscription_name
        )

        response = self._client.pull(
            request={
                "subscription": subscription_path,
                "max_messages": 1,
            },
            retry=retry.Retry(deadline=timeout),
            timeout=timeout,
        )

        if not response.received_messages:
            logger.debug("No messages on %s", subscription_name)
            return None

        received = response.received_messages[0]
        ack_id = received.ack_id
        raw_data = received.message.data.decode("utf-8")

        try:
            payload = json.loads(raw_data)
            model_instance = model_class(**payload)

            # Ack only after successful deserialization
            self._client.acknowledge(
                request={
                    "subscription": subscription_path,
                    "ack_ids": [ack_id],
                }
            )

            logger.info(
                "Pulled and acked from %s | session_id=%s",
                subscription_name,
                getattr(model_instance, "session_id", "unknown"),
            )
            return model_instance

        except Exception as e:
            logger.error(
                "Failed to deserialize message from %s: %s",
                subscription_name,
                str(e),
            )
            # Do NOT ack — message will be redelivered after ack deadline
            return None

    def close(self):
        self._client.transport.close()


# ── Convenience functions ─────────────────────────────────────────────────────

_publisher: Optional[CDSPublisher] = None
_subscriber: Optional[CDSSubscriber] = None


def get_publisher() -> CDSPublisher:
    """Return a module-level singleton publisher."""
    global _publisher
    if _publisher is None:
        _publisher = CDSPublisher()
    return _publisher


def get_subscriber() -> CDSSubscriber:
    """Return a module-level singleton subscriber."""
    global _subscriber
    if _subscriber is None:
        _subscriber = CDSSubscriber()
    return _subscriber


def publish_message(
    topic_name: str,
    message: BaseModel,
    attributes: Optional[dict[str, str]] = None,
) -> str:
    """Module-level shortcut for publishing."""
    return get_publisher().publish(topic_name, message, attributes)


def pull_message(
    subscription_name: str,
    model_class: Type[T],
    timeout: float = 30.0,
) -> Optional[T]:
    """Module-level shortcut for pulling."""
    return get_subscriber().pull_one(subscription_name, model_class, timeout)