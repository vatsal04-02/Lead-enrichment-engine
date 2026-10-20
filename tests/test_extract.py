from leadengine.extract import extract_page, pick_best, good_email
from leadengine.validate import format_checks, format_valid, verify
from leadengine.scoring import field_status, score_company
from datetime import datetime, timedelta, timezone

HTML = '''<html><head><title>Home | Acme Plumbing</title>
<meta name="description" content="Acme Plumbing offers 24/7 emergency plumbing across Austin.">
<script type="application/ld+json">{"@context":"https://schema.org","@type":"LocalBusiness","name":"Acme Plumbing LLC",
"telephone":"+1 512-555-0101","address":{"@type":"PostalAddress","streetAddress":"12 Main St","addressLocality":"Austin","addressRegion":"TX"},
"sameAs":["https://www.linkedin.com/company/acme-plumbing","https://twitter.com/acmeplumb"]}</script></head>
<body><nav><a href="/about-us">About</a><a href="/contact">Contact us</a><a href="https://other.com/about">x</a></nav>
<a href="mailto:info@acme.com?subject=hi">mail</a><a href="https://facebook.com/sharer.php?u=x">share</a>
Founded in 1998. Call (512) 555-0102</body></html>'''


def test_extracts_structured_data_and_links():
    r = extract_page(HTML, "https://acme.com/", "home", "acme.com")
    best = {k: v[0] for k, v in pick_best(r.candidates, "acme.com").items()}
    assert best["company_name"].value == "Acme Plumbing LLC" and best["company_name"].method == "json_ld"
    assert best["email"].value == "info@acme.com" and best["email"].method == "mailto"
    assert best["phone"].value == "+1 512-555-0101"
    assert best["address"].value == "12 Main St, Austin, TX"
    assert best["linkedin_url"].value.endswith("/company/acme-plumbing")
    assert best["founded_year"].value == "1998"
    assert "facebook_url" not in best            # share links are not company pages
    assert r.links["about"] == ["https://acme.com/about-us"]
    assert r.links["contact"] == ["https://acme.com/contact"]
    assert all("other.com" not in u for us in r.links.values() for u in us)


def test_missing_stays_missing():
    r = extract_page("<html><body><p>Hello world</p></body></html>", "https://x.com/", "home", "x.com")
    assert pick_best(r.candidates, "x.com") == {}


def test_junk_emails_rejected():
    for e in ["a@example.com", "logo@2x.png", "noreply@acme.com", "name@domain.com"]:
        assert not good_email(e)
    assert good_email("sales@acme.com")


def test_malformed_html_and_binary_do_not_crash():
    r = extract_page('<div><p>unclosed <a href="mailto:a@b.co">\x00\x01<table><script>if(a<b){', "https://b.co/", "home", "b.co")
    assert any(c.value == "a@b.co" for c in r.candidates)


def test_js_shell_detected():
    html = '<html><head><script src="/a.js"></script><script src="/b.js"></script></head><body><div id="root"></div></body></html>'
    assert extract_page(html, "https://x.com/", "home", "x.com").js_hint


def test_cross_source_verification_and_email_never_verified():
    r = extract_page(HTML, "https://acme.com/", "home", "acme.com")
    best = pick_best(r.candidates, "acme.com")
    name_c = best["company_name"][0]
    assert verify("company_name", name_c, r.candidates) == ("verified", "cross_source_agreement")  # json_ld + title agree
    email_c = best["email"][0]
    assert verify("email", email_c, r.candidates) == ("unverified", None)


def test_format_checks_separate_from_verification():
    c = format_checks("email", "email", "bad@@x", "x.com")
    assert not format_valid(c)
    c = format_checks("email", "email", "info@x.com", "x.com")
    assert format_valid(c) and c["info_same_domain"]


def test_quality_scoring_distinguishes_present_valid_verified_stale():
    now = datetime.now(timezone.utc)
    row = dict(status="found", value="info@x.com", source_url="https://x.com/", method="mailto",
               verification_status="unverified", checks={"nonempty": True, "syntax": True},
               refresh_after=now + timedelta(days=1))
    st = field_status(row, now)
    assert st == {**st, "present": True, "source_attributed": True, "format_valid": True, "verified": False,
                  "stale": False, "quality": 0.8}
    stale = field_status({**row, "refresh_after": now - timedelta(days=1)}, now)
    assert stale["stale"] and stale["quality"] == 0.4
    row2 = {**row, "verification_status": "verified"}
    assert field_status(row2, now)["quality"] == 1.0
    sc = score_company({"email": row, "phone": {**row, "status": "not_found", "value": None}}, ["email", "phone"], now)
    assert sc["completeness"] == 0.5 and sc["verified_share"] == 0.0
