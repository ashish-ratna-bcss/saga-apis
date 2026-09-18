from bluweb_app.services.extraction.event_extractor import extract_event
from bluweb_app.services.extraction.structured_data import extract_structured_data

_EVENT_HTML = '''<html><head><script type="application/ld+json">
{"@type":"Event","name":"Town Hall Meeting","startDate":"2026-10-01T18:00:00Z",
"endDate":"2026-10-01T20:00:00Z","eventStatus":"https://schema.org/EventScheduled",
"description":"Join the city council for our monthly town hall discussion on local budget priorities.",
"location":{"@type":"Place","name":"City Hall","address":{"addressLocality":"Springfield"}},
"organizer":{"@type":"Organization","name":"City Council"},
"offers":{"@type":"Offer","price":"0"}}
</script></head><body><h1>Town Hall</h1></body></html>'''


def test_extracts_event_from_structured_data():
    structured = extract_structured_data(_EVENT_HTML, "https://example.gov/events/town-hall")
    doc = extract_event("https://example.gov/events/town-hall", _EVENT_HTML, structured=structured)

    assert doc is not None
    assert doc.extractor == "event"
    assert doc.headline == "Town Hall Meeting"
    assert doc.publisher == "City Council"
    assert doc.section == "City Hall"
    assert doc.published_at is not None and doc.published_at.year == 2026
    assert doc.raw_metadata["status"] == "EventScheduled"


def test_no_event_entity_returns_none():
    html = "<html><body><p>No event here.</p></body></html>"
    structured = extract_structured_data(html, "https://example.com/x")
    assert extract_event("https://example.com/x", html, structured=structured) is None
