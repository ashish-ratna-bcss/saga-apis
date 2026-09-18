"""Request/response schemas for the Telegram provider API (app/api/routes_provider.py).

Field names here match the external contract exactly (e.g. `members_count`,
`photo_url`) rather than this codebase's own internal naming (`member_count`,
`photo_thumb_data_uri` in app/schemas/search.py) - this module is a stable
contract for an external consumer, not a reflection of our internal shapes.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class ChannelIdentifierRequest(BaseModel):
    """Any one of the three identifies the channel/group/user/bot."""

    username: str | None = None
    url: str | None = None
    channel_id: str | None = None


class ProviderChannelOut(BaseModel):
    id: str
    username: str | None = None
    title: str | None = None
    type: str
    description: str | None = None
    members_count: int | None = None
    photo_url: str | None = None
    is_public: bool
    url: str | None = None


class ProviderAuthorOut(BaseModel):
    id: str | None = None
    username: str | None = None
    name: str | None = None


class ProviderMediaOut(BaseModel):
    type: str
    filename: str | None = None
    mime_type: str | None = None
    file_size: int | None = None
    url: str | None = None


class ProviderMessageOut(BaseModel):
    id: str
    channel_id: str
    text: str | None = None
    date: datetime | None = None
    url: str | None = None
    views: int | None = None
    forwards: int | None = None
    replies_count: int | None = None
    media: list[ProviderMediaOut] = []
    author: ProviderAuthorOut | None = None


class ChannelMessagesRequest(BaseModel):
    username: str | None = None
    channel_id: str | None = None
    limit: int | None = None
    cursor: str | None = None


class ProviderMessagesResponse(BaseModel):
    items: list[ProviderMessageOut]
    cursor: str | None = None


class MessageRequest(BaseModel):
    url: str | None = None
    channel_id: str | None = None
    message_id: str | None = None


class ProviderReplyOut(BaseModel):
    id: str
    text: str | None = None
    date: datetime | None = None
    url: str | None = None
    author: ProviderAuthorOut | None = None


class MessageRepliesRequest(BaseModel):
    channel_id: str
    message_id: str
    limit: int | None = None
    cursor: str | None = None


class ProviderRepliesResponse(BaseModel):
    items: list[ProviderReplyOut]
    cursor: str | None = None


class ProviderChannelSummaryOut(BaseModel):
    id: str
    username: str | None = None
    title: str | None = None
    type: str
    description: str | None = None
    members_count: int | None = None
    photo_url: str | None = None
    url: str | None = None


class ProviderSearchChannelsResponse(BaseModel):
    items: list[ProviderChannelSummaryOut]


class ProviderSearchMessagesResponse(BaseModel):
    items: list[ProviderMessageOut]
    cursor: str | None = None


class ResolveLinkRequest(BaseModel):
    url: str


class ProviderResolveResponse(BaseModel):
    kind: str  # "message" | "channel" | "invite"
    channel: ProviderChannelOut | None = None
    message: ProviderMessageOut | None = None
    invite: str | None = None


class ProviderAccessResponse(BaseModel):
    accessible: bool
    state: str  # public | member | pending | denied | invite_required | unknown
    detail: str | None = None


class JoinInviteRequest(BaseModel):
    invite: str


class ProviderJoinResponse(BaseModel):
    joined: bool
    pending: bool
    channel: ProviderChannelOut | None = None
