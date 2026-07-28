"""Periodically queue ingestion jobs for every active account.

Run with: python -m workers.scheduler

Kept separate from the consumer so the two scale independently: one scheduler
must exist, but any number of consumers may.
"""

import asyncio

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from api.core import mq
from api.db.database import AsyncSessionLocal, engine
from api.utils.logger import logger
from api.utils.settings import settings
from api.v1.models.connected_account import AccountStatus, ConnectedAccount


async def enqueue_due_accounts() -> int:
    """Queue a sync for every account in a syncable state.

    Includes token_expired accounts on purpose: a user who reconnects gets
    picked up on the next tick without any special-casing, and an account whose
    token is still bad simply fails again and stays marked.
    """
    async with AsyncSessionLocal() as db:
        accounts = (
            (
                await db.execute(
                    select(ConnectedAccount).where(
                        ConnectedAccount.status.in_(
                            [AccountStatus.ACTIVE, AccountStatus.TOKEN_EXPIRED]
                        )
                    )
                )
            )
            .scalars()
            .all()
        )

    for account in accounts:
        await mq.publish_ingestion_job(str(account.id))

    logger.info("Queued %s ingestion job(s)", len(accounts))
    return len(accounts)


async def main() -> None:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        enqueue_due_accounts,
        # Engagement settles over the first 24-48h and Meta's per-user rate
        # limit is roughly 200 calls/hour, so polling harder than this buys
        # nothing and risks throttling (PROJECT_SPEC.md §9.5).
        IntervalTrigger(hours=settings.INGESTION_INTERVAL_HOURS),
        id="ingest-all-accounts",
        # If the process was down over a scheduled run, do one catch-up rather
        # than firing every missed interval at once.
        coalesce=True,
        max_instances=1,
        next_run_time=None,
    )
    scheduler.start()
    logger.info(
        "Scheduler started — every %sh", settings.INGESTION_INTERVAL_HOURS
    )

    # Sync once at boot so a freshly connected account doesn't wait a full
    # interval for its first data.
    await enqueue_due_accounts()

    try:
        await asyncio.Event().wait()
    finally:
        scheduler.shutdown()
        await mq.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
