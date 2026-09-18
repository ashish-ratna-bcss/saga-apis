from bluweb_app.services.extraction.faq_extractor import extract_faq
from bluweb_app.services.extraction.structured_data import extract_structured_data

_FAQ_HTML = '''<html><head><script type="application/ld+json">
{"@type":"FAQPage","mainEntity":[
{"@type":"Question","name":"What are your hours?","acceptedAnswer":{"@type":"Answer","text":"We are open 9-5 Monday through Friday."}},
{"@type":"Question","name":"Do you ship internationally?","acceptedAnswer":{"@type":"Answer","text":"Yes, worldwide."}}
]}</script></head><body><h1>FAQ</h1></body></html>'''

_URL = "https://example.com/faq"


def test_extracts_faq_items_from_structured_data():
    structured = extract_structured_data(_FAQ_HTML, _URL)
    doc = extract_faq(_URL, _FAQ_HTML, structured=structured)

    assert doc is not None
    assert doc.extractor == "faq"
    assert doc.raw_metadata["faq_item_count"] == 2
    assert "What are your hours?" in doc.body
    assert "We are open 9-5" in doc.body


def test_no_faq_page_entity_returns_none():
    html = "<html><body><p>No FAQ here.</p></body></html>"
    structured = extract_structured_data(html, "https://example.com/x")
    assert extract_faq("https://example.com/x", html, structured=structured) is None
