"""RabbitMQ consumer for provider webhook events.

Run with: python -m workers.webhook_consumer
"""

import asyncio
import json

import aio_pika

from api.utils.logger import logger
from api.utils.settings import settings
from api.v1.services.webhook_service import process_webhook_event


async def main() -> None:
    connection = await aio_pika.connect_robust(settings.RABBITMQ_URL)
    async with connection:
        channel = await connection.channel()
        await channel.set_qos(prefetch_count=10)
        queue = await channel.declare_queue(settings.WEBHOOK_QUEUE_NAME, durable=True)
        logger.info("Webhook consumer listening on %s", settings.WEBHOOK_QUEUE_NAME)

        async with queue.iterator() as messages:
            async for message in messages:
                async with message.process(requeue=False):
                    try:
                        await process_webhook_event(json.loads(message.body))
                    except Exception:
                        logger.exception("Failed to process webhook event")


if __name__ == "__main__":
    asyncio.run(main())
