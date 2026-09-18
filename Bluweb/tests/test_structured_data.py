from bluweb_app.services.extraction.structured_data import extract_structured_data

_ARTICLE_HTML = '''
<html><head><title>Test</title>
<meta property="og:title" content="A Great Article"/>
<meta property="og:type" content="article"/>
<meta property="og:url" content="https://news.example.com/article-1"/>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"NewsArticle","headline":"Breaking News Today",
"datePublished":"2026-01-15T10:00:00Z","dateModified":"2026-01-16T08:00:00Z",
"author":{"@type":"Person","name":"Jane Doe"},"publisher":{"@type":"Organization","name":"Example News"},
"articleSection":"Politics","keywords":["election","politics"],"image":["https://news.example.com/img.jpg"]}
</script>
</head><body><article><h1>Breaking News Today</h1><p>Body text here.</p></article></body></html>
'''


def test_extracts_all_fields_from_full_json_ld_and_og():
    result = extract_structured_data(_ARTICLE_HTML, "https://news.example.com/article-1")
    assert result.schema_type == "NewsArticle"
    assert result.headline == "Breaking News Today"
    assert result.author == "Jane Doe"
    assert result.published_at is not None and result.published_at.year == 2026
    assert result.updated_at is not None
    assert result.publisher == "Example News"
    assert result.section == "Politics"
    assert result.tags == ["election", "politics"]
    assert result.images == ["https://news.example.com/img.jpg"]
    assert result.og_type == "article"
    assert result.canonical_url == "https://news.example.com/article-1"


def test_no_structured_data_returns_empty_result_without_crashing():
    result = extract_structured_data("<html><body><p>hi</p></body></html>", "https://x.com/")
    assert result.schema_type is None
    assert result.headline is None
    assert result.tags == []


def test_malformed_json_ld_does_not_crash():
    html = '<script type="application/ld+json">{not valid json</script>'
    result = extract_structured_data(html, "https://x.com/")
    assert result.schema_type is None


# -- Universal Adaptive Web Intelligence: additional entity types --


def test_event_entity_extracted():
    html = '''<script type="application/ld+json">
    {"@type":"Event","name":"Town Hall","startDate":"2026-10-01T18:00:00Z",
    "eventStatus":"https://schema.org/EventScheduled",
    "location":{"@type":"Place","name":"City Hall"},
    "organizer":{"@type":"Organization","name":"City Council"}}
    </script>'''
    result = extract_structured_data(html, "https://example.gov/events/town-hall")
    event = result.entities["Event"]
    assert event["name"] == "Town Hall"
    assert event["status"] == "EventScheduled"
    assert event["location"] == "City Hall"
    assert event["organizer"] == "City Council"


def test_job_posting_entity_extracted():
    html = '''<script type="application/ld+json">
    {"@type":"JobPosting","title":"Backend Engineer","datePosted":"2026-08-01",
    "hiringOrganization":{"@type":"Organization","name":"Acme Corp"},
    "baseSalary":{"@type":"MonetaryAmount","currency":"USD",
    "value":{"@type":"QuantitativeValue","value":150000}}}
    </script>'''
    result = extract_structured_data(html, "https://acme.com/careers/backend-engineer")
    job = result.entities["JobPosting"]
    assert job["title"] == "Backend Engineer"
    assert job["salary"] == "150000"
    assert job["salary_currency"] == "USD"
    assert job["hiring_organization"] == "Acme Corp"


def test_faq_page_entity_extracted():
    html = '''<script type="application/ld+json">
    {"@type":"FAQPage","mainEntity":[
    {"@type":"Question","name":"What are your hours?","acceptedAnswer":{"@type":"Answer","text":"9-5"}}]}
    </script>'''
    result = extract_structured_data(html, "https://example.com/faq")
    assert result.entities["FAQPage"]["items"] == [{"question": "What are your hours?", "answer": "9-5"}]


def test_person_entity_extracted():
    html = '''<script type="application/ld+json">
    {"@type":"Person","name":"Dr. Jane Smith","jobTitle":"Professor of Physics",
    "worksFor":{"@type":"Organization","name":"State University"}}
    </script>'''
    result = extract_structured_data(html, "https://university.edu/faculty/jane-smith")
    person = result.entities["Person"]
    assert person["name"] == "Dr. Jane Smith"
    assert person["works_for"] == "State University"


def test_breadcrumb_list_extracted():
    html = '''<script type="application/ld+json">
    {"@type":"BreadcrumbList","itemListElement":[
    {"@type":"ListItem","position":1,"name":"Home","item":"https://example.com/"},
    {"@type":"ListItem","position":2,"name":"Events","item":"https://example.com/events/"}]}
    </script>'''
    result = extract_structured_data(html, "https://example.com/events/")
    assert result.breadcrumbs == [
        {"name": "Home", "url": "https://example.com/", "position": 1},
        {"name": "Events", "url": "https://example.com/events/", "position": 2},
    ]


def test_entities_and_article_do_not_interfere():
    # A page with both a top-level NewsArticle block AND a separate
    # top-level BreadcrumbList block: the article-shaped fields classify
    # normally, and the breadcrumb is picked up as a distinct signal
    # without overriding `headline`. (A Person block *nested inside*
    # `author` is not picked up as a separate entity -- only sibling
    # top-level @type blocks are scanned; see module docstring.)
    html = '''<script type="application/ld+json">
    {"@type":"NewsArticle","headline":"Big Story","author":{"@type":"Person","name":"Reporter Bob"}}
    </script>
    <script type="application/ld+json">
    {"@type":"BreadcrumbList","itemListElement":[{"@type":"ListItem","position":1,"name":"News","item":"https://news.example.com/"}]}
    </script>'''
    result = extract_structured_data(html, "https://news.example.com/big-story")
    assert result.headline == "Big Story"
    assert result.breadcrumbs == [{"name": "News", "url": "https://news.example.com/", "position": 1}]
