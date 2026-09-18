"""Structured metadata extraction (spec Phase B): JSON-LD, microdata,
OpenGraph via `extruct` (BSD-licensed, actively maintained by Zyte/
Scrapinghub, does exactly this and only this -- adopted rather than
hand-rolling a second JSON-LD/OG parser next to the presence-only checks
`preflight/html.py` already does for the capability-scoring use case).

This module only *extracts and normalizes* structured data. Deciding what
kind of page it describes is the page classifier's job
(`app/services/classification/page_classifier.py`), which consumes this.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

import extruct

logger = logging.getLogger("webintel.structured_data")

# schema.org @type values that mean "this is an article-shaped thing" --
# used by both the classifier and the News/Blog extractors.
ARTICLE_TYPES = frozenset({
    "Article", "NewsArticle", "BlogPosting", "Report", "AnalysisNewsArticle",
    "OpinionNewsArticle", "ReviewNewsArticle", "SocialMediaPosting", "TechArticle",
})

# Universal Adaptive Web Intelligence: additional schema.org entity types
# normalized into `StructuredData.entities`, keyed by this dict's own key
# (not the raw @type, so a subtype like "MusicEvent" still lands under
# "Event"). One flat dict rather than a new dataclass field per type --
# bounded schema growth (spec section 3: "do not create unnecessary
# complexity") while still keeping each entity's fields inspectable.
_EVENT_TYPES = frozenset({"Event", "BusinessEvent", "EducationEvent", "SocialEvent", "MusicEvent", "SportsEvent", "Festival"})
_JOB_TYPES = frozenset({"JobPosting"})
_FAQ_TYPES = frozenset({"FAQPage"})
_PERSON_TYPES = frozenset({"Person"})
_ORG_TYPES = frozenset({
    "Organization", "LocalBusiness", "Corporation", "NGO",
    "GovernmentOrganization", "EducationalOrganization",
})
_BREADCRUMB_TYPES = frozenset({"BreadcrumbList"})
_ENTITY_TYPE_MAP = (
    ("Event", _EVENT_TYPES), ("JobPosting", _JOB_TYPES), ("FAQPage", _FAQ_TYPES),
    ("Person", _PERSON_TYPES), ("Organization", _ORG_TYPES),
)


@dataclass
class StructuredData:
    schema_type: str | None = None
    headline: str | None = None
    author: str | None = None
    published_at: datetime | None = None
    updated_at: datetime | None = None
    publisher: str | None = None
    section: str | None = None
    tags: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    og_type: str | None = None
    og_title: str | None = None
    canonical_url: str | None = None
    raw_json_ld: list[dict] = field(default_factory=list)
    entities: dict[str, dict] = field(default_factory=dict)
    breadcrumbs: list[dict] = field(default_factory=list)


def extract_structured_data(html: str, url: str) -> StructuredData:
    try:
        data = extruct.extract(html, base_url=url, syntaxes=["json-ld", "opengraph", "microdata"], uniform=True)
    except Exception:  # noqa: BLE001 - malformed markup must never break the crawl
        logger.warning("structured data extraction failed for %s", url, exc_info=True)
        return StructuredData()

    json_ld_blocks = [b for b in data.get("json-ld", []) if isinstance(b, dict)]
    og_blocks = data.get("opengraph", [])
    microdata_blocks = [b for b in data.get("microdata", []) if isinstance(b, dict)]

    article_block = _pick_article_block(json_ld_blocks) or _pick_article_block(microdata_blocks)
    og = og_blocks[0] if og_blocks else {}

    result = StructuredData(raw_json_ld=json_ld_blocks)

    if article_block:
        props = article_block.get("properties", article_block)  # microdata nests under "properties"
        result.schema_type = _short_type(article_block.get("@type") or article_block.get("type"))
        result.headline = _as_text(props.get("headline") or props.get("name"))
        result.author = _as_text(_author_name(props.get("author")))
        result.published_at = _parse_datetime(props.get("datePublished"))
        result.updated_at = _parse_datetime(props.get("dateModified"))
        result.publisher = _as_text(_org_name(props.get("publisher")))
        result.section = _as_text(props.get("articleSection"))
        result.tags = _as_list(props.get("keywords"))
        result.images = _as_list(props.get("image"))

    result.og_type = og.get("@type") or og.get("og:type")
    result.og_title = og.get("og:title")
    result.canonical_url = og.get("og:url")

    # Second pass, independent of the article-block match above: a page can
    # be a NewsArticle AND separately carry a BreadcrumbList, or a directory
    # profile page can carry Person without ever matching ARTICLE_TYPES.
    # First-match-wins per entity type (a page rarely has two Events, and if
    # it does, the first one found is the page's own subject).
    for block in json_ld_blocks + microdata_blocks:
        type_value = _short_type(block.get("@type") or block.get("type"))
        if type_value is None:
            continue
        props = block.get("properties", block) if "properties" in block else block

        if type_value in _BREADCRUMB_TYPES and not result.breadcrumbs:
            result.breadcrumbs = _extract_breadcrumbs(props)
            continue

        for entity_key, type_set in _ENTITY_TYPE_MAP:
            if type_value in type_set and entity_key not in result.entities:
                result.entities[entity_key] = _ENTITY_EXTRACTORS[entity_key](props)
                break

    return result


def _pick_article_block(blocks: list[dict]) -> dict | None:
    for block in blocks:
        type_value = _short_type(block.get("@type") or block.get("type"))
        if type_value in ARTICLE_TYPES:
            return block
    return None


def _short_type(type_value) -> str | None:
    if isinstance(type_value, list):
        type_value = type_value[0] if type_value else None
    if not isinstance(type_value, str):
        return None
    return type_value.rsplit("/", 1)[-1]  # microdata gives full schema.org URLs


def _author_name(author_value):
    if isinstance(author_value, list):
        author_value = author_value[0] if author_value else None
    if isinstance(author_value, dict):
        return author_value.get("name") or author_value.get("properties", {}).get("name")
    return author_value


def _org_name(publisher_value):
    if isinstance(publisher_value, dict):
        return publisher_value.get("name") or publisher_value.get("properties", {}).get("name")
    return publisher_value


def _as_text(value) -> str | None:
    if isinstance(value, list):
        value = value[0] if value else None
    return str(value) if value is not None else None


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)]


def _extract_event_fields(props: dict) -> dict:
    location = props.get("location")
    if isinstance(location, list):
        location = location[0] if location else {}
    location_name = None
    if isinstance(location, dict):
        address = location.get("address")
        location_name = _as_text(location.get("name")) or _as_text(
            address.get("addressLocality") if isinstance(address, dict) else address
        )
    offers = props.get("offers")
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    return {
        "name": _as_text(props.get("name")),
        "description": _as_text(props.get("description")),
        "start_date": _iso(_parse_datetime(props.get("startDate"))),
        "end_date": _iso(_parse_datetime(props.get("endDate"))),
        "status": _short_type(props.get("eventStatus")),
        "location": location_name,
        "organizer": _org_name(props.get("organizer")),
        "price": _as_text(offers.get("price")) if isinstance(offers, dict) else None,
        "image": _as_list(props.get("image")),
    }


def _extract_job_fields(props: dict) -> dict:
    job_location = props.get("jobLocation")
    if isinstance(job_location, list):
        job_location = job_location[0] if job_location else {}
    location_name = None
    if isinstance(job_location, dict):
        address = job_location.get("address")
        if isinstance(address, dict):
            location_name = _as_text(address.get("addressLocality"))
        elif isinstance(address, str):
            location_name = address

    salary = props.get("baseSalary")
    salary_value, currency = None, None
    if isinstance(salary, dict):
        value = salary.get("value")
        salary_value = value.get("value") or value.get("minValue") if isinstance(value, dict) else value
        currency = salary.get("currency")

    return {
        "title": _as_text(props.get("title") or props.get("name")),
        "description": _as_text(props.get("description")),
        "date_posted": _iso(_parse_datetime(props.get("datePosted"))),
        "valid_through": _iso(_parse_datetime(props.get("validThrough"))),
        "hiring_organization": _org_name(props.get("hiringOrganization")),
        "location": location_name,
        "salary": _as_text(salary_value),
        "salary_currency": _as_text(currency),
        "employment_type": _as_text(props.get("employmentType")),
    }


def _extract_faq_fields(props: dict) -> dict:
    main_entity = props.get("mainEntity") or []
    if isinstance(main_entity, dict):
        main_entity = [main_entity]
    items = []
    for question in main_entity:
        if not isinstance(question, dict):
            continue
        answer = question.get("acceptedAnswer")
        if isinstance(answer, list):
            answer = answer[0] if answer else {}
        answer_text = _as_text(answer.get("text")) if isinstance(answer, dict) else None
        question_text = _as_text(question.get("name"))
        if question_text:
            items.append({"question": question_text, "answer": answer_text})
    return {"items": items}


def _extract_person_fields(props: dict) -> dict:
    return {
        "name": _as_text(props.get("name")),
        "job_title": _as_text(props.get("jobTitle")),
        "works_for": _org_name(props.get("worksFor")),
        "description": _as_text(props.get("description")),
        "image": _as_list(props.get("image")),
        "url": _as_text(props.get("url")),
    }


def _extract_org_fields(props: dict) -> dict:
    address = props.get("address")
    address_text = None
    if isinstance(address, dict):
        street = _as_text(address.get("streetAddress"))
        locality = _as_text(address.get("addressLocality"))
        address_text = ", ".join(p for p in (street, locality) if p) or None
    elif isinstance(address, str):
        address_text = address
    return {
        "name": _as_text(props.get("name")),
        "description": _as_text(props.get("description")),
        "address": address_text,
        "telephone": _as_text(props.get("telephone")),
        "url": _as_text(props.get("url")),
    }


def _extract_breadcrumbs(props: dict) -> list[dict]:
    items = props.get("itemListElement") or []
    if not isinstance(items, list):
        return []
    result = []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        item_ref = entry.get("item")
        name = _as_text(entry.get("name"))
        url = None
        if isinstance(item_ref, dict):
            name = name or _as_text(item_ref.get("name"))
            url = _as_text(item_ref.get("@id") or item_ref.get("url"))
        elif isinstance(item_ref, str):
            url = item_ref
        if name:
            result.append({"name": name, "url": url, "position": entry.get("position")})
    return result


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


_ENTITY_EXTRACTORS = {
    "Event": _extract_event_fields,
    "JobPosting": _extract_job_fields,
    "FAQPage": _extract_faq_fields,
    "Person": _extract_person_fields,
    "Organization": _extract_org_fields,
}


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    if isinstance(value, list):
        value = value[0] if value else None
    if not isinstance(value, str):
        return None
    normalized = value.replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.fromisoformat(normalized) if fmt is None else datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None
