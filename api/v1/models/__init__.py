"""Model registry.

alembic/env.py does ``from api.v1.models import *`` to populate
``Base.metadata`` before autogenerate runs. Any new model MUST be imported and
listed in ``__all__`` here, or Alembic will silently generate an empty
migration — or worse, a DROP.
"""

from api.v1.models.base_model import BaseTableModel
from api.v1.models.connected_account import AccountStatus, ConnectedAccount
from api.v1.models.insight import Insight, InsightType
from api.v1.models.post import Post, PostType
from api.v1.models.post_metrics_snapshot import PostMetricsSnapshot
from api.v1.models.user import SubscriptionStatus, User

__all__ = [
    "AccountStatus",
    "BaseTableModel",
    "ConnectedAccount",
    "Insight",
    "InsightType",
    "Post",
    "PostMetricsSnapshot",
    "PostType",
    "SubscriptionStatus",
    "User",
]
