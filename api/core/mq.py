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


async def publish_webhook_event(payload: dict) -> None:
    connection = await connect()
    channel = await connection.channel()
    try:
        await channel.declare_queue(settings.WEBHOOK_QUEUE_NAME, durable=True)
        await channel.default_exchange.publish(
            aio_pika.Message(
                body=json.dumps(payload).encode(),
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            ),
            routing_key=settings.WEBHOOK_QUEUE_NAME,
        )
    finally:
        await channel.close()
