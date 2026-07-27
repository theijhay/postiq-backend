import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class ConnectedAccountResponse(BaseModel):
    """Public view of a connected account.

    Deliberately omits every ``*_token_encrypted`` column — tokens must never
    leave the server, encrypted or not.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    platform: str
    fb_page_id: Optional[str]
    ig_business_id: Optional[str]
    account_name: Optional[str]
    status: str
    connected_at: datetime
    token_expires_at: Optional[datetime]
    last_ingested_at: Optional[datetime]


class MetaConnectResponse(BaseModel):
    authorization_url: str
