"""Efficient re-discovery for monitoring crawls (spec section 77): prefer
sitemap/RSS entries over re-crawling the whole homepage every interval.

Reuses the pre-flight engine's own sitemap/robots/feed checks -- they're
already bounded (sampled, short-timeout) exactly because they were built to
run inline in a request; that same shape is what a scheduler tick wants,
so there's no separate "discovery" implementation to keep in sync with the
pre-flight one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import feedparser

from bluweb_app.core.config import Settings
from bluweb_app.services.discovery.sitemap_discovery import discover_sitemap_urls
from bluweb_app.services.events import Event, default_bus
from bluweb_app.services.normalization.url_normalizer import extract_domain
from bluweb_app.services.preflight.feeds import check_feeds
from bluweb_app.services.preflight.models import DiscoveryStatus
from bluweb_app.services.preflight.robots import check_robots
from bluweb_app.services.preflight.sitemap import check_sitemap
from bluweb_app.services.security.url_security import URLSecurityService

logger = logging.getLogger("webintel.discovery")

_MAX_FEED_ENTRIES = 20


@dataclass
class DiscoveryOutcome:
    urls: list[str] = field(default_factory=list)
    sitemap_status: DiscoveryStatus = DiscoveryStatus.UNKNOWN
    feed_status: DiscoveryStatus = DiscoveryStatus.UNKNOWN
    sitemap_url_count: int = 0
    feed_url_count: int = 0


async def discover_extra_seeds(base_url: str, settings: Settings, security: URLSecurityService) -> DiscoveryOutcome:
    """Best-effort: any failure here just means the crawl falls back to
    homepage-only discovery, never blocks the scheduled crawl itself.

    `urls` are actual article/page URLs -- a feed's own XML URL is a
    discovery mechanism, not a page to crawl and run the extractor against,
    so any feed found is fetched and parsed for its entry links here rather
    than being added to the frontier directly. Same for a sitemap *index*:
    its `sample_urls` are nested sitemap FILE urls (e.g. sitemap-news.xml),
    not content pages -- `discover_sitemap_urls` recurses into those and
    returns only real page URLs (spec Phase 9 section 5, bug fixed here).

    `sitemap_status`/`feed_status` (spec Phase 9 section 7) distinguish "not
    present" from "blocked"/"failed"/"invalid" -- see DiscoveryStatus --
    persisted by the caller via `persist_discovery_outcome` so domain
    capability learning knows the difference.
    """
    outcome = DiscoveryOutcome()
    try:
        robots = await check_robots(base_url, settings, security)
        sitemap = await check_sitemap(base_url, robots, settings, security)
        outcome.sitemap_status = sitemap.status
        if sitemap.available and sitemap.sitemap_url:
            sitemap_result = await discover_sitemap_urls(sitemap.sitemap_url, settings, security)
            outcome.urls.extend(u.url for u in sitemap_result.urls)
            outcome.sitemap_url_count = len(sitemap_result.urls)
            await default_bus.publish(Event("SITEMAP_DISCOVERED", {"url": base_url, "count": outcome.sitemap_url_count}))

        feed = await check_feeds(base_url, None, settings, security)
        outcome.feed_status = feed.status
        if feed.available and feed.feed_url:
            feed_urls = await _feed_entry_links(feed.feed_url, settings, security)
            outcome.urls.extend(feed_urls)
            outcome.feed_url_count = len(feed_urls)
            await default_bus.publish(Event("FEED_DISCOVERED", {"url": base_url, "count": outcome.feed_url_count}))
    except Exception:  # noqa: BLE001
        logger.warning("seed discovery failed for %s, falling back to homepage-only", base_url, exc_info=True)

    outcome.urls = list(dict.fromkeys(outcome.urls))
    return outcome


async def persist_discovery_outcome(base_url: str, outcome: DiscoveryOutcome) -> None:
    """No-op: Bluweb no longer owns a database for capability learning.

    Kept so legacy callers (scheduler) do not crash; discovery still runs
    in-process via ``discover_extra_seeds``.
    """
    return None


async def _feed_entry_links(feed_url: str, settings: Settings, security: URLSecurityService) -> list[str]:
    async with security.build_client(timeout=settings.preflight_http_timeout_seconds) as client:
        response = await client.get(feed_url)
    parsed = feedparser.parse(response.content)
    return [entry.link for entry in parsed.entries[:_MAX_FEED_ENTRIES] if getattr(entry, "link", None)]
