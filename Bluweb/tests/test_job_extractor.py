from bluweb_app.services.extraction.job_extractor import extract_job
from bluweb_app.services.extraction.structured_data import extract_structured_data

_JOB_HTML = '''<html><head><script type="application/ld+json">
{"@type":"JobPosting","title":"Backend Engineer","datePosted":"2026-08-01",
"hiringOrganization":{"@type":"Organization","name":"Acme Corp"},
"description":"We are looking for a backend engineer to join our growing team and build scalable systems.",
"jobLocation":{"@type":"Place","address":{"addressLocality":"Remote"}},
"baseSalary":{"@type":"MonetaryAmount","currency":"USD","value":{"@type":"QuantitativeValue","value":150000}},
"employmentType":"FULL_TIME"}
</script></head><body><h1>Backend Engineer</h1></body></html>'''

_URL = "https://acme.com/careers/backend-engineer"


def test_extracts_job_from_structured_data():
    structured = extract_structured_data(_JOB_HTML, _URL)
    doc = extract_job(_URL, _JOB_HTML, structured=structured)

    assert doc is not None
    assert doc.extractor == "job"
    assert doc.headline == "Backend Engineer"
    assert doc.publisher == "Acme Corp"
    assert doc.section == "Remote"
    assert doc.raw_metadata["salary"] == "150000"
    assert doc.raw_metadata["salary_currency"] == "USD"
    assert doc.raw_metadata["employment_type"] == "FULL_TIME"


def test_status_open_when_no_valid_through():
    structured = extract_structured_data(_JOB_HTML, _URL)
    doc = extract_job(_URL, _JOB_HTML, structured=structured)
    assert doc.raw_metadata["status"] == "open"


def test_status_closed_when_valid_through_in_past():
    html = _JOB_HTML.replace(
        '"employmentType":"FULL_TIME"}', '"employmentType":"FULL_TIME","validThrough":"2020-01-01"}'
    )
    structured = extract_structured_data(html, _URL)
    doc = extract_job(_URL, html, structured=structured)
    assert doc.raw_metadata["status"] == "closed"


def test_no_job_posting_entity_returns_none():
    html = "<html><body><p>Not a job page.</p></body></html>"
    structured = extract_structured_data(html, "https://example.com/x")
    assert extract_job("https://example.com/x", html, structured=structured) is None
