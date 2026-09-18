from datetime import UTC, datetime

from osint_app.routes.lookups import _merge_username_evidence
from osint_app.schemas import EvidenceOut


def _evidence(source_name: str, normalized_value: str, confidence: float, **metadata) -> EvidenceOut:
    return EvidenceOut(
        id=f"id-{source_name}-{normalized_value}",
        entity_id=None,
        job_id=f"job-{source_name}",
        source_name=source_name,
        source_type="username_presence",
        query="someuser",
        raw_value=normalized_value,
        normalized_value=normalized_value,
        entity_type="SocialProfile",
        source_url=normalized_value,
        observed_at=datetime.now(UTC),
        confidence=confidence,
        extraction_method=f"{source_name}_module",
        raw_metadata=metadata,
    )


def test_hit_confirmed_by_both_tools_collapses_to_one_row_with_higher_confidence():
    sherlock_hit = _evidence("sherlock", "https://github.com/someuser", 0.55, site="github.com")
    maigret_hit = _evidence("maigret", "https://github.com/someuser", 0.55, site="github.com")

    merged = _merge_username_evidence([sherlock_hit, maigret_hit])

    assert len(merged) == 1
    result = merged[0]
    assert set(result.source_name.split("+")) == {"sherlock", "maigret"}
    # corroboration must score higher than either tool alone -- never just
    # pick one arbitrary source's confidence and never average it down
    assert result.confidence > 0.55
    assert result.raw_metadata == {"sherlock": {"site": "github.com"}, "maigret": {"site": "github.com"}}


def test_hit_from_only_one_tool_keeps_its_own_confidence_and_metadata_keyed_by_source():
    sherlock_only = _evidence("sherlock", "https://example.com/someuser", 0.55, site="example.com")

    merged = _merge_username_evidence([sherlock_only])

    assert len(merged) == 1
    assert merged[0].source_name == "sherlock"
    assert merged[0].confidence == 0.55
    assert merged[0].raw_metadata == {"sherlock": {"site": "example.com"}}


def test_same_site_different_url_paths_still_merges():
    """Live-observed real case: sherlock and maigret confirm the same site
    with structurally different profile URLs for the same username."""
    sherlock_hit = _evidence("sherlock", "https://hackerrank.com/darlaashishratna", 0.55)
    maigret_hit = _evidence("maigret", "https://www.hackerrank.com/profile/darlaashishratna", 0.55)

    merged = _merge_username_evidence([sherlock_hit, maigret_hit])

    assert len(merged) == 1
    assert set(merged[0].source_name.split("+")) == {"sherlock", "maigret"}
    assert merged[0].confidence > 0.55


def test_different_urls_never_merged():
    hit_a = _evidence("sherlock", "https://github.com/someuser", 0.55, site="github.com")
    hit_b = _evidence("maigret", "https://gitlab.com/someuser", 0.55, site="gitlab.com")

    merged = _merge_username_evidence([hit_a, hit_b])

    assert len(merged) == 2
    assert {r.source_name for r in merged} == {"sherlock", "maigret"}
    by_source = {r.source_name: r for r in merged}
    assert by_source["sherlock"].raw_metadata == {"sherlock": {"site": "github.com"}}
    assert by_source["maigret"].raw_metadata == {"maigret": {"site": "gitlab.com"}}
