"""SQLAlchemy ORM models.

Kept deliberately plain (no Telethon types, no business logic) so the schema
stays portable from SQLite to another SQLAlchemy-supported database without
touching the service/repository layers.
"""
from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #


class SourceType(str, enum.Enum):
    CHANNEL = "channel"
    GROUP = "group"
    SUPERGROUP = "supergroup"
    USER = "user"
    BOT = "bot"
    UNKNOWN = "unknown"


class SourceAccessStatus(str, enum.Enum):
    """The explicit source-access state machine (see access_manager for transitions)."""

    DISCOVERED = "DISCOVERED"
    PUBLIC_ACCESSIBLE = "PUBLIC_ACCESSIBLE"
    ACCESSIBLE = "ACCESSIBLE"
    JOIN_REQUEST_REQUIRED = "JOIN_REQUEST_REQUIRED"
    JOIN_REQUEST_PENDING = "JOIN_REQUEST_PENDING"
    JOIN_REQUEST_APPROVED = "JOIN_REQUEST_APPROVED"
    JOIN_REQUEST_REJECTED = "JOIN_REQUEST_REJECTED"
    JOINED = "JOINED"
    MONITORING = "MONITORING"
    ACCESS_DENIED = "ACCESS_DENIED"
    NOT_FOUND = "NOT_FOUND"
    ERROR = "ERROR"
    DISABLED = "DISABLED"


class AccessRequestStatus(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    ERROR = "ERROR"


class MediaType(str, enum.Enum):
    PHOTO = "photo"
    VIDEO = "video"
    DOCUMENT = "document"
    AUDIO = "audio"
    VOICE = "voice"
    STICKER = "sticker"
    OTHER = "other"


class ProcessingStatus(str, enum.Enum):
    RAW = "raw"
    NORMALIZED = "normalized"
    ENRICHED = "enriched"


class CollectionMethod(str, enum.Enum):
    """How a message ended up persisted - distinguishes the live monitoring
    pipeline from search-driven and backfill-driven ingestion."""

    MONITORING = "monitoring"
    GLOBAL_SEARCH = "global_search"
    CHANNEL_SEARCH = "channel_search"
    HISTORICAL_BACKFILL = "historical_backfill"
    MANUAL_COLLECTION = "manual_collection"


class BackfillJobStatus(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


# --------------------------------------------------------------------------- #
# Tables
# --------------------------------------------------------------------------- #


class TelegramAccount(Base):
    __tablename__ = "telegram_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_user_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone_masked: Mapped[str | None] = mapped_column(String(32), nullable=True)
    session_path: Mapped[str] = mapped_column(String(512), nullable=False)
    is_authenticated: Mapped[bool] = mapped_column(Boolean, default=False)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TelegramSource(Base):
    __tablename__ = "telegram_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    identifier: Mapped[str] = mapped_column(String(512), nullable=False)
    telegram_entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_type: Mapped[str] = mapped_column(String(32), default=SourceType.UNKNOWN.value)
    access_status: Mapped[str] = mapped_column(String(32), default=SourceAccessStatus.DISCOVERED.value, index=True)
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    monitoring_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    monitoring_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_status_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Separate from `access_status` (the operational field, e.g. MONITORING)
    # on purpose: the outcome of the most recent live Telegram probe, recorded
    # unconditionally every time check_access runs - even when the state
    # machine blocks that probe result from overwriting an operational status
    # like MONITORING. Reuses SourceAccessStatus values; not a new enum.
    last_probe_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_probe_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_probe_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Discovery provenance - who/what surfaced this source, if not manually
    # registered by an operator. Never implies monitoring or access on its
    # own; see BotService._discover_from_bot_response.
    discovered_from_source_id: Mapped[int | None] = mapped_column(
        ForeignKey("telegram_sources.id", ondelete="SET NULL"), nullable=True
    )
    discovery_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    access_requests: Mapped[list["TelegramAccessRequest"]] = relationship(
        back_populates="source", cascade="all, delete-orphan"
    )
    messages: Mapped[list["TelegramMessage"]] = relationship(back_populates="source", cascade="all, delete-orphan")
    checkpoint: Mapped["CollectionCheckpoint | None"] = relationship(
        back_populates="source", uselist=False, cascade="all, delete-orphan"
    )


class TelegramAccessRequest(Base):
    __tablename__ = "telegram_access_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("telegram_sources.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(32), default=AccessRequestStatus.PENDING.value, index=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    source: Mapped[TelegramSource] = relationship(back_populates="access_requests")


class TelegramMessage(Base):
    __tablename__ = "telegram_messages"
    __table_args__ = (UniqueConstraint("source_id", "telegram_message_id", name="uq_source_message"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("telegram_sources.id", ondelete="CASCADE"), index=True)
    telegram_message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    telegram_channel_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    telegram_group_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sender_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sender_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sender_display_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    message_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    edit_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    views: Mapped[int | None] = mapped_column(Integer, nullable=True)
    forwards: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reply_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    grouped_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    media_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    raw_data_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    raw_data_ref: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    processing_status: Mapped[str] = mapped_column(String(32), default=ProcessingStatus.RAW.value)
    collection_method: Mapped[str] = mapped_column(String(32), default=CollectionMethod.MONITORING.value, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    source: Mapped[TelegramSource] = relationship(back_populates="messages")
    media_items: Mapped[list["TelegramMedia"]] = relationship(back_populates="message", cascade="all, delete-orphan")


class TelegramMedia(Base):
    __tablename__ = "telegram_media"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("telegram_messages.id", ondelete="CASCADE"), index=True)
    media_type: Mapped[str] = mapped_column(String(32), nullable=False)
    telegram_media_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    filename: Mapped[str | None] = mapped_column(String(512), nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    local_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    downloaded: Mapped[bool] = mapped_column(Boolean, default=False)
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    message: Mapped[TelegramMessage] = relationship(back_populates="media_items")


class TelegramEntity(Base):
    """Cache of resolved Telegram entities (channels/groups/users) - not access proof."""

    __tablename__ = "telegram_entities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    raw_data_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CollectionCheckpoint(Base):
    __tablename__ = "collection_checkpoints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_sources.id", ondelete="CASCADE"), unique=True, index=True
    )
    last_message_id: Mapped[int] = mapped_column(Integer, default=0)
    last_collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    source: Mapped[TelegramSource] = relationship(back_populates="checkpoint")


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("telegram_sources.id", ondelete="SET NULL"), nullable=True)
    telegram_entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    previous_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    new_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    processed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("telegram_sources.id", ondelete="SET NULL"), nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    actor: Mapped[str] = mapped_column(String(128), default="system")
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    success: Mapped[bool] = mapped_column(Boolean, default=True)


class SchedulerJobRun(Base):
    __tablename__ = "scheduler_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="RUNNING")
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class BackfillJob(Base):
    """Tracks one historical-collection request for a source.

    Progress is its own checkpoint (`checkpoint_message_id`, the oldest
    message id successfully persisted so far) - deliberately separate from
    `collection_checkpoints`, which anchors the live forward-incremental
    monitoring pipeline. Backfill walks *backward* into history and must
    never advance or corrupt that forward checkpoint.
    """

    __tablename__ = "backfill_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("telegram_sources.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(32), default=BackfillJobStatus.PENDING.value, index=True)
    from_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    to_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    requested_limit: Mapped[int] = mapped_column(Integer, default=1000)
    messages_found: Mapped[int] = mapped_column(Integer, default=0)
    messages_inserted: Mapped[int] = mapped_column(Integer, default=0)
    messages_skipped: Mapped[int] = mapped_column(Integer, default=0)
    checkpoint_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    source: Mapped[TelegramSource] = relationship()
