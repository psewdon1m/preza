from html.parser import HTMLParser

from fastapi.testclient import TestClient

from app.main import create_app
from conftest import ORIGIN, create, edit


class PageMetadata(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.meta = {}
        self.title = ""
        self.in_title = False
        self.scripts = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta":
            self.meta[attrs.get("property") or attrs.get("name")] = attrs.get("content")
        elif tag == "title":
            self.in_title = True
        elif tag == "script":
            self.scripts.append(attrs.get("src"))

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title += data


def test_protected_presentation_has_share_metadata_without_exposing_slides(owner, application):
    record = create(owner, "private-share", password="1111")
    title = 'Отчёт <Q4> & "планы" </title><script>alert(1)</script>'
    response = edit(owner, record, title=title)
    assert response.status_code == 200
    with TestClient(application, base_url=ORIGIN) as public:
        page = public.get("/p/private-share")
        assert page.status_code == 200
        assert page.headers["content-type"].startswith("text/html")
        metadata = PageMetadata(page.text)
        assert metadata.title == title + " — Preza"
        assert metadata.meta["og:title"] == metadata.title
        assert metadata.meta["twitter:title"] == metadata.title
        assert metadata.meta["og:url"] == ORIGIN + "/p/private-share"
        assert metadata.meta["og:image"] == ORIGIN + "/assets/brand/social-preview.jpg"
        assert metadata.meta["twitter:image"] == metadata.meta["og:image"]
        assert metadata.meta["twitter:card"] == "summary_large_image"
        assert len(metadata.scripts) == 1
        assert metadata.scripts[0].split("?", 1)[0] == "/assets/view.js"
        assert "?v=" in metadata.scripts[0]
        assert record["content_version"] not in page.text
        assert public.get(record["slide_base"] + "0").status_code == 401
        preview = public.get(metadata.meta["og:image"])
        assert preview.status_code == 200
        assert preview.headers["content-type"] == "image/jpeg"


def test_share_urls_use_public_url_instead_of_request_host(application, tmp_path, monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "https://share.example.test/")
    with TestClient(create_app(tmp_path), base_url="http://internal.test") as client:
        page = client.get("/")
        assert page.status_code == 200
        metadata = PageMetadata(page.text)
        assert metadata.title == "Preza — управление презентациями"
        assert metadata.meta["og:url"] == "https://share.example.test/admin"
        assert metadata.meta["og:image"] == "https://share.example.test/assets/brand/social-preview.jpg"
