"""RabbitMQ consumer that syncs one connected account per message.

Run with: python -m workers.ingestion_consumer
"""

import asyncio
import json
import uuid

import aio_pika
from sqlalchemy import select

from api.db.database import AsyncSessionLocal, engine
from api.utils.logger import logger, silence_noisy_loggers
from api.utils.settings import settings
from api.v1.models.connected_account import AccountStatus, ConnectedAccount
from api.v1.services.ingestion_service import ingest_account
from api.v1.services.meta_client import MetaAPIError

# Meta rate-limits per user, so a wide prefetch would only queue up 429s.
# Syncs are minutes apart, not milliseconds — depth here buys nothing.
PREFETCH = 2


async def handle_message(body: bytes) -> None:
    payload = json.loads(body)
    account_id = uuid.UUID(payload["account_id"])

    async with AsyncSessionLocal() as db:
        account = (
            await db.execute(
                select(ConnectedAccount).where(ConnectedAccount.id == account_id)
            )
        ).scalar_one_or_none()

        # The account may have been disconnected while this job sat in the
        # queue. Dropping the message is correct — not an error.
        if account is None:
            logger.info("Ingestion job for unknown account %s — dropping", account_id)
            return
        if account.status == AccountStatus.DISCONNECTED:
            logger.info("Account %s is disconnected — skipping sync", account_id)
            return

        await ingest_account(db, account)


async def main() -> None:
    # The worker makes the bulk of the Graph API calls, so it is the biggest
    # source of credential leakage into logs if httpx is left at INFO.
    silence_noisy_loggers()
    connection = await aio_pika.connect_robust(settings.RABBITMQ_URL)
    async with connection:
        channel = await connection.channel()
        await channel.set_qos(prefetch_count=PREFETCH)
        queue = await channel.declare_queue(
            settings.INGESTION_QUEUE_NAME, durable=True
        )
        logger.info("Ingestion consumer listening on %s", settings.INGESTION_QUEUE_NAME)

        async with queue.iterator() as messages:
            async for message in messages:
                # requeue=False: a poison message would otherwise loop forever.
                # Failed accounts are retried by the next scheduled run, and a
                # token problem is recorded on the account row itself.
                async with message.process(requeue=False):
                    try:
                        await handle_message(message.body)
                    except MetaAPIError as exc:
                        logger.warning("Meta rejected a sync: %s", exc)
                    except Exception:
                        logger.exception("Ingestion job failed")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
