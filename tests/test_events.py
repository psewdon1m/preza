import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from app.main import create_app
from conftest import KEY, make_pdf


@pytest.fixture
def server(tmp_path, monkeypatch):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret-" * 3)
    monkeypatch.setenv("ADMIN_ACCESS_KEY", KEY)
    monkeypatch.setenv("PUBLIC_URL", url)
    monkeypatch.setenv("PDF_RENDER_DPI", "72")
    application = create_app(tmp_path)
    service = uvicorn.Server(uvicorn.Config(application, log_level="error", lifespan="on"))
    worker = threading.Thread(target=lambda: service.run(sockets=[listener]), daemon=True)
    worker.start()
    deadline = time.monotonic() + 5
    while not service.started and time.monotonic() < deadline:
        time.sleep(.01)
    assert service.started
    try:
        yield url
    finally:
        service.should_exit = True
        worker.join(timeout=5)
        if worker.is_alive():
            service.force_exit = True
        listener.close()


def next_event(lines):
    kind, data = "", ""
    for line in lines:
        if line.startswith("event: "):
            kind = line[7:]
        if line.startswith("data: "):
            data = line[6:]
        if not line and data:
            return kind, json.loads(data)
    raise AssertionError("SSE stream ended before the expected event")


def test_two_viewers_follow_live_changes_reconnect_password_rotation_and_deletion(server):
    with httpx.Client(base_url=server, headers={"Origin": server}, timeout=5) as owner:
        owner.headers["X-CSRF-Token"] = owner.post("/api/admin/login", json={"key": KEY}).json()["csrf"]
        response = owner.post("/api/admin/presentations", data={"title": "Live", "slug": "live"},
                              files={"file": ("live.pdf", make_pdf(), "application/pdf")})
        assert response.status_code == 201
        record = response.json()
        stream_url = "/api/p/" + record["id"] + "/events"
        control = "/api/admin/presentations/" + record["id"] + "/live"
        with httpx.Client(base_url=server, timeout=5) as first, httpx.Client(base_url=server, timeout=5) as second:
            with first.stream("GET", stream_url) as a, second.stream("GET", stream_url) as b:
                lines_a, lines_b = a.iter_lines(), b.iter_lines()
                assert next_event(lines_a)[0] == next_event(lines_b)[0] == "state"
                owner.post(control, json={"active": True, "slide": 2}).raise_for_status()
                for lines in (lines_a, lines_b):
                    kind, state = next_event(lines)
                    assert kind == "state" and state["live_active"] and state["current_slide"] == 2
            with first.stream("GET", stream_url) as reconnect:
                assert next_event(reconnect.iter_lines())[1]["current_slide"] == 2
            current = owner.get("/api/admin/presentations/" + record["id"]).json()
            with first.stream("GET", stream_url) as a:
                lines = a.iter_lines()
                next_event(lines)
                owner.put("/api/admin/presentations/" + record["id"], data={
                    "title": "Live", "slug": "live", "expected_revision": current["revision"],
                    "password_action": "set", "password": "1111",
                }).raise_for_status()
                assert next_event(lines)[0] == "locked"
            first.headers["Origin"] = server
            first.post("/api/public/presentations/live/unlock", json={"password": "1111"}).raise_for_status()
            with first.stream("GET", stream_url) as a:
                lines = a.iter_lines()
                next_event(lines)
                owner.delete("/api/admin/presentations/" + record["id"]).raise_for_status()
                assert next_event(lines)[0] == "deleted"
