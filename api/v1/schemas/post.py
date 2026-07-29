import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class PostMetrics(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    views: Optional[int] = None
    reach: Optional[int] = None
    likes: Optional[int] = None
    comments: Optional[int] = None
    shares: Optional[int] = None
    saves: Optional[int] = None
    engagement_rate: Optional[float] = None


class PostResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    platform_post_id: str
    # instagram | facebook. Never pool the two when comparing performance —
    # their reach mechanics differ enough that a combined average is meaningless.
    source: str
    post_type: Optional[str]
    caption: Optional[str]
    posted_at: Optional[datetime]
    permalink: Optional[str]
    media_url: Optional[str]
    # Latest snapshot only. The full time series stays server-side; the
    # dashboard never needs every capture for a list view.
    metrics: Optional[PostMetrics] = None


class PaginatedPosts(BaseModel):
    items: list[PostResponse]
    total: int
    page: int
    page_size: int
