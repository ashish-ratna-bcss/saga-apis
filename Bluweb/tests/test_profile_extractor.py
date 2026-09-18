from bluweb_app.services.extraction.profile_extractor import extract_profile
from bluweb_app.services.extraction.structured_data import extract_structured_data

_PERSON_HTML = '''<html><head><script type="application/ld+json">
{"@type":"Person","name":"Dr. Jane Smith","jobTitle":"Professor of Physics",
"worksFor":{"@type":"Organization","name":"State University"},
"description":"Dr. Smith researches quantum foundations and has published extensively in the field."}
</script></head><body><h1>Jane Smith</h1></body></html>'''

_ORG_HTML = '''<html><head><script type="application/ld+json">
{"@type":"Organization","name":"Acme Nonprofit","description":"A community organization serving the greater metro area since 1998.",
"address":{"@type":"PostalAddress","streetAddress":"123 Main St","addressLocality":"Springfield"},
"telephone":"555-1234"}
</script></head><body><h1>About Us</h1></body></html>'''


def test_extracts_person_profile():
    structured = extract_structured_data(_PERSON_HTML, "https://university.edu/faculty/jane-smith")
    doc = extract_profile("https://university.edu/faculty/jane-smith", _PERSON_HTML, structured=structured)

    assert doc is not None
    assert doc.extractor == "profile"
    assert doc.headline == "Dr. Jane Smith"
    assert doc.publisher == "State University"
    assert doc.section == "Professor of Physics"
    assert doc.raw_metadata["subject_type"] == "Person"


def test_falls_back_to_organization_when_no_person():
    structured = extract_structured_data(_ORG_HTML, "https://acmenonprofit.org/about")
    doc = extract_profile("https://acmenonprofit.org/about", _ORG_HTML, structured=structured)

    assert doc is not None
    assert doc.headline == "Acme Nonprofit"
    assert doc.raw_metadata["subject_type"] == "Organization"
    assert doc.raw_metadata["telephone"] == "555-1234"


def test_no_person_or_organization_returns_none():
    html = "<html><body><p>Nothing structured here.</p></body></html>"
    structured = extract_structured_data(html, "https://example.com/x")
    assert extract_profile("https://example.com/x", html, structured=structured) is None
