import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from conftest import KEY, ORIGIN, create, edit, make_pdf
from app.storage import Storage
from app.main import create_app


def viewer(application):
    # Lifespan is already running in the owner fixture; don't initialize twice.
    return TestClient(application, base_url=ORIGIN, headers={"Origin": ORIGIN})


def test_admin_key_is_exact_and_mutations_require_origin_and_csrf(client):
    assert client.get("/api/admin/presentations").status_code == 401
    assert client.post("/api/admin/login", json={"key": KEY.strip()}).status_code == 401
    assert client.post("/api/admin/login", json={"key": KEY}, headers={"Origin": "https://evil.test"}).status_code == 403
    response = client.post("/api/admin/login", json={"key": KEY})
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "Secure" in response.headers["set-cookie"]
    assert client.post("/api/admin/logout").status_code == 403
    assert client.post("/api/admin/logout", headers={"X-CSRF-Token": response.json()["csrf"]}).status_code == 200
    assert client.get("/api/admin/presentations").status_code == 401


def test_multiple_presentations_are_isolated_and_no_public_catalog(owner, application):
    first, second = create(owner, "first"), create(owner, "second", default_mode="slides", allow_switch="false")
    assert first["id"] != second["id"]
    assert len(owner.get("/api/admin/presentations").json()["presentations"]) == 2
    public = viewer(application)
    state = public.get("/api/public/presentations/second").json()
    assert state["default_mode"] == "slides" and not state["allow_switch"]
    for route in ("/api/slides", "/api/upload", "/api/public/presentations", "/slides/slide-1.png",
                  "/.env", "/source/current.pdf"):
        assert public.get(route).status_code == 404
    assert public.get(first["slide_base"] + "0").headers["content-type"] == "image/png"


def test_simple_password_protects_images_events_and_only_its_own_deck(owner, application):
    first, second = create(owner, "private", "1111"), create(owner, "other", "1111")
    public = viewer(application)
    assert public.get("/api/public/presentations/private").json()["locked"]
    assert public.get(first["slide_base"] + "0").status_code == 401
    assert public.get("/api/p/" + first["id"] + "/events").status_code == 401
    assert public.post("/api/public/presentations/private/unlock", json={"password": "bad"}).status_code == 401
    assert public.post("/api/public/presentations/private/unlock", json={"password": "1111"}).status_code == 200
    assert not public.get("/api/public/presentations/private").json()["locked"]
    assert public.get(first["slide_base"] + "0").status_code == 200
    assert public.get(second["slide_base"] + "0").status_code == 401
    assert "password_hash" not in owner.get("/api/admin/presentations").text


def test_password_rotation_revokes_existing_access_and_removal_opens_it(owner, application):
    record = create(owner, password="1111")
    public = viewer(application)
    public.post("/api/public/presentations/deck/unlock", json={"password": "1111"})
    updated = edit(owner, record, password_action="set", password="2").json()
    assert public.get(record["slide_base"] + "0").status_code == 401
    assert public.post("/api/public/presentations/deck/unlock", json={"password": "1111"}).status_code == 401
    assert public.post("/api/public/presentations/deck/unlock", json={"password": "2"}).status_code == 200
    assert edit(owner, updated, password_action="remove").status_code == 200
    assert viewer(application).get(record["slide_base"] + "0").status_code == 200


def test_settings_slug_and_optimistic_edit_conflict(owner, application):
    record = create(owner)
    response = edit(owner, record, title="Новый заголовок", slug="renamed", default_mode="slides", allow_switch="false")
    assert response.status_code == 200
    current = response.json()
    assert edit(owner, record, title="stale edit").status_code == 409
    assert owner.get("/api/public/presentations/deck").status_code == 404
    assert owner.get("/api/public/presentations/renamed").json()["title"] == "Новый заголовок"
    assert current["content_version"] == record["content_version"]
    assert not current["allow_switch"]
    assert edit(owner, current, slug="../outside").status_code == 400
    create(owner, "taken")
    assert edit(owner, current, slug="taken").status_code == 409
    assert create(owner, "auto")["slide_count"] == 3


def test_failed_replacement_keeps_old_pdf_and_success_changes_version(owner, application):
    record = create(owner)
    url = "/api/admin/presentations/" + record["id"]
    data = {"title": record["title"], "slug": record["slug"], "expected_revision": record["revision"]}
    broken = owner.put(url, data=data, files={"file": ("bad.pdf", b"%PDF-1.4\nbroken", "application/pdf")})
    assert broken.status_code == 400
    assert owner.get(record["slide_base"] + "0").status_code == 200
    assert owner.get(url).json()["content_version"] == record["content_version"]
    replacement = owner.put(url, data=data, files={"file": ("new.pdf", make_pdf(2), "application/pdf")})
    assert replacement.status_code == 200
    current = replacement.json()
    assert current["slide_count"] == 2 and current["content_version"] != record["content_version"]
    assert owner.get(record["slide_base"] + "0").status_code == 404
    assert owner.get(current["slide_base"] + "1").status_code == 200
    assert not list(application.state.storage.staging.iterdir())


def test_live_enforces_current_slide_for_viewers_and_allows_admin_preview(owner, application):
    record = create(owner)
    public = viewer(application)
    control = "/api/admin/presentations/" + record["id"] + "/live"
    assert public.post(control, json={"active": True, "slide": 1}).status_code == 401
    assert owner.post(control, json={"active": True, "slide": 20}).status_code == 400
    assert owner.post(control, json={"active": True, "slide": 1}).status_code == 200
    state = public.get("/api/public/presentations/deck").json()
    assert state["live_active"] and state["current_slide"] == 1
    assert public.get(record["slide_base"] + "0").status_code == 409
    assert public.get(record["slide_base"] + "1").status_code == 200
    assert owner.get(record["preview_base"] + "0").status_code == 200
    owner.post(control, json={"active": False, "slide": 1})
    assert public.get(record["slide_base"] + "0").status_code == 200


def test_delete_does_not_delete_other_presentations(owner, application):
    first, second = create(owner, "first"), create(owner, "second")
    assert owner.delete("/api/admin/presentations/" + first["id"]).status_code == 200
    assert owner.get(first["slide_base"] + "0").status_code == 404
    assert owner.get(second["slide_base"] + "0").status_code == 200
    assert not (application.state.storage.presentations / first["id"]).exists()


def test_non_pdf_and_duplicate_addresses_are_rejected(owner):
    first = create(owner)
    duplicate = owner.post("/api/admin/presentations", data={"title": "Duplicate", "slug": "deck"},
        files={"file": ("slides.pdf", make_pdf(), "application/pdf")})
    assert duplicate.status_code == 409
    bad = owner.post("/api/admin/presentations", data={"title": "Bad", "slug": "bad"},
        files={"file": ("slides.pdf", b"not a pdf", "application/pdf")})
    assert bad.status_code == 400
    assert owner.get(first["slide_base"] + "0").status_code == 200


def test_legacy_migration_is_idempotent_preserves_files_and_does_not_resurrect(tmp_path):
    old = tmp_path / "slides"
    old.mkdir()
    (old / "slide-10.png").write_bytes(b"ten")
    (old / "slide-2.png").write_bytes(b"two")
    storage = Storage(tmp_path)
    storage.initialize()
    migrated = storage.all()[0]
    assert migrated["legacy"] and migrated["slide_count"] == 2
    assert (storage.version_path(migrated) / "slide-1.png").read_bytes() == b"two"
    assert (old / "slide-10.png").exists()
    storage.initialize()
    assert len(storage.all()) == 1
    with storage.connect() as db:
        db.execute("DELETE FROM presentations")
    storage.initialize()
    assert storage.all() == []


def test_login_throttles_failed_attempts(client):
    for _ in range(10):
        assert client.post("/api/admin/login", json={"key": "wrong"}).status_code == 401
    assert client.post("/api/admin/login", json={"key": "wrong"}).status_code == 429


def test_upload_limits_with_content_length_and_chunked_transfer(tmp_path, monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret-" * 3)
    monkeypatch.setenv("ADMIN_ACCESS_KEY", KEY)
    monkeypatch.setenv("PUBLIC_URL", ORIGIN)
    monkeypatch.setenv("MAX_UPLOAD_MB", "1")
    application = create_app(tmp_path)
    with TestClient(application, base_url=ORIGIN, headers={"Origin": ORIGIN}) as client:
        client.headers["X-CSRF-Token"] = client.post("/api/admin/login", json={"key": KEY}).json()["csrf"]
        response = client.post("/api/admin/presentations", data={"title": "Large", "slug": "large"},
            files={"file": ("large.pdf", b"%PDF-" + b"x" * (3 * 1024 * 1024), "application/pdf")})
        assert response.status_code == 413
        def chunks():
            yield b'--test\r\nContent-Disposition: form-data; name="file"; filename="large.pdf"\r\nContent-Type: application/pdf\r\n\r\n'
            for _ in range(4):
                yield b"x" * (1024 * 1024)
            yield b"\r\n--test--\r\n"
        response = client.post("/api/admin/presentations", content=chunks(),
                               headers={"Content-Type": "multipart/form-data; boundary=test"})
        assert response.status_code == 413
        assert client.get("/api/health").status_code == 200
