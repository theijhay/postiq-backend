import json

import aio_pika

from api.utils.settings import settings

_connection: aio_pika.abc.AbstractRobustConnection | None = None


async def connect() -> aio_pika.abc.AbstractRobustConnection:
    """Establish (or reuse) the robust broker connection.

    Raises on first failure; once established, aio-pika reconnects
    automatically if the broker drops.
    """
    global _connection
    if _connection is None or _connection.is_closed:
        _connection = await aio_pika.connect_robust(settings.RABBITMQ_URL)
    return _connection


async def close() -> None:
    global _connection
    if _connection is not None and not _connection.is_closed:
        await _connection.close()
    _connection = None


async def _publish(queue_name: str, payload: dict) -> None:
    """Publish one persistent message onto a durable queue."""
    connection = await connect()
    channel = await connection.channel()
    try:
        await channel.declare_queue(queue_name, durable=True)
        await channel.default_exchange.publish(
            aio_pika.Message(
                body=json.dumps(payload).encode(),
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            ),
            routing_key=queue_name,
        )
    finally:
        await channel.close()


async def publish_webhook_event(payload: dict) -> None:
    await _publish(settings.WEBHOOK_QUEUE_NAME, payload)


async def publish_ingestion_job(account_id: str, reason: str = "scheduled") -> None:
    """Queue a sync for one connected account.

    Carries only the account id — the worker re-reads current state from the
    database, so a job that sits in the queue during a disconnect does the right
    thing when it is finally picked up.
    """
    await _publish(
        settings.INGESTION_QUEUE_NAME,
        {"account_id": str(account_id), "reason": reason},
    )
