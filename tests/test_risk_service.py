"""Requires a running Redis (docker compose up -d redis)."""

import uuid

from api.utils.settings import settings
from api.v1.models import User
from api.v1.services import risk_service


def _user(limit_kobo: int) -> User:
    return User(id=uuid.uuid4(), daily_limit_kobo=limit_kobo)


async def test_within_limits_passes():
    user = _user(1_000_000)
    assert await risk_service.check_limits(user, 500_000) is None


async def test_daily_limit_exceeded():
    user = _user(1_000_000)
    await risk_service.record_spend(user.id, 900_000)
    assert await risk_service.check_limits(user, 200_000) == "limit_exceeded"
    # exactly at the limit is allowed
    assert await risk_service.check_limits(user, 100_000) is None


async def test_velocity_cap():
    user = _user(10_000_000_000)
    for _ in range(settings.MAX_TX_PER_HOUR):
        await risk_service.record_spend(user.id, 1)
    assert await risk_service.check_limits(user, 1) == "velocity_exceeded"
