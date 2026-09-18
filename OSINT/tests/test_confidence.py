from datetime import UTC, datetime, timedelta

from osint_app.confidence import compute_confidence


def test_single_source_gives_base_score_plus_reliability():
    # holehe's reliability prior (0.80) clears the 0.70 threshold, so the
    # "Source reliability" factor also applies: 0.30 + 0.05.
    result = compute_confidence(source_names=["holehe"])
    assert result.score == 0.35
    assert len(result.explanation()) == 2


def test_two_independent_sources_stack():
    result = compute_confidence(source_names=["holehe", "maigret"])
    assert result.score == 0.55


def test_three_independent_sources_stack():
    result = compute_confidence(source_names=["holehe", "maigret", "sherlock"])
    assert round(result.score, 2) == 0.65


def test_duplicate_source_names_not_double_counted():
    result = compute_confidence(source_names=["holehe", "holehe", "holehe"])
    assert result.score == 0.35


def test_exact_match_and_recency_add_up():
    result = compute_confidence(
        source_names=["holehe", "maigret"],
        exact_match=True,
        most_recent_observation=datetime.now(UTC) - timedelta(days=1),
    )
    assert round(result.score, 2) == 0.75


def test_old_observation_does_not_get_recency_bonus():
    result = compute_confidence(
        source_names=["holehe"],
        most_recent_observation=datetime.now(UTC) - timedelta(days=400),
    )
    assert result.score == 0.35


def test_score_caps_at_one():
    result = compute_confidence(
        source_names=["phonenumbers", "hibp_pwned_passwords", "holehe", "maigret"],
        exact_match=True,
        name_company_agreement=True,
        most_recent_observation=datetime.now(UTC),
    )
    assert result.score == 1.0


def test_every_factor_is_explainable():
    result = compute_confidence(source_names=["holehe", "maigret"], exact_match=True)
    for line in result.explanation():
        assert line.startswith("✓")
