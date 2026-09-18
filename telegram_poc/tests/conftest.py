from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from telegram_app.config import Settings
from telegram_app.database.models import Base


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as s:
        yield s

    await engine.dispose()


@pytest.fixture
def settings(tmp_path):
    return Settings(
        telegram_api_id=12345,
        telegram_api_hash="fakehash",
        telegram_session_path=str(tmp_path / "test.session"),
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        media_storage_path=str(tmp_path / "media"),
        raw_evidence_storage_path=str(tmp_path / "raw_messages"),
        avatar_cache_path=str(tmp_path / "avatars"),
        message_media_cache_path=str(tmp_path / "message_media"),
        media_download_enabled=False,
        collection_batch_limit=200,
        access_reconciliation_interval_hours=12,
    )
