import json
import secrets
import threading
import uuid

import pytest
from fastapi.testclient import TestClient

import updater
import install as installer
from app.main import create_app
from app.updates import UpdateClient, UpdateServiceError
from conftest import KEY, ORIGIN, create
from test_installer import prepared


class DeferredThread:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass


def test_admin_only_update_api_requires_origin_csrf_and_valid_version(client, owner):
    guest = TestClient(owner.app, base_url=ORIGIN)
    assert guest.get("/api/admin/updates").status_code == 401
    assert guest.post("/api/admin/updates/check").status_code == 401
    assert owner.get("/api/admin/updates").json()["enabled"] is False
    assert owner.post("/api/admin/updates/check", headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert owner.post("/api/admin/updates/check", headers={"Origin": "https://evil.test"}).status_code == 403
    assert owner.post("/api/admin/updates/check").status_code == 503
    assert owner.post("/api/admin/updates", json={"version": "latest;reboot", "request_id": str(uuid.uuid4())}).status_code == 422


def test_host_control_accepts_only_checked_upgrade_and_is_idempotent(prepared, monkeypatch):
    root, values = prepared
    controller = updater.Controller(root)
    monkeypatch.setattr(updater, "latest_release", lambda: "0.0.2")
    monkeypatch.setattr(updater.threading, "Thread", DeferredThread)
    request = {"version": "0.0.2", "request_id": str(uuid.uuid4())}
    with pytest.raises(updater.ControlError, match="Сначала"):
        controller.request_update(request)
    assert controller.check()["update_available"]
    for version in ("0.0.1", "0.0.3", "../0.0.2", "0.0.2;reboot"):
        with pytest.raises(updater.ControlError):
            controller.request_update({**request, "version": version})
    first = controller.request_update(request)
    assert first["updating"] and first["job"]["id"] == request["request_id"]
    assert controller.request_update(request)["job"]["id"] == request["request_id"]
    with pytest.raises(updater.ControlError, match="не завершено"):
        controller.request_update({**request, "request_id": str(uuid.uuid4())})
    assert updater.Controller(root).job["id"] == request["request_id"]


def test_real_unix_connection_authenticates_and_exposes_only_update_operations(prepared, owner, tmp_path, monkeypatch):
    root, values = prepared
    controller = updater.Controller(root)
    monkeypatch.setattr(updater, "latest_release", lambda: "0.0.2")
    socket = tmp_path / "c.sock"
    with updater.Server(str(socket), updater.Handler) as server:
        server.controller = controller
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            client = UpdateClient(str(socket), controller.token)
            with pytest.raises(UpdateServiceError) as error:
                UpdateClient(str(socket), "incorrect-token").request("GET", "/status")
            assert error.value.status == 403
            with pytest.raises(UpdateServiceError) as error:
                client.request("POST", "/shell", {"command": "reboot"})
            assert error.value.status == 404
            owner.app.state.updates = client
            assert owner.get("/api/admin/updates").json()["current_version"] == "0.0.1"
            assert owner.post("/api/admin/updates/check").json()["latest_version"] == "0.0.2"
            assert controller.token not in owner.get("/api/admin/updates").text
            monkeypatch.setattr(updater.Controller, "worker", lambda self: None)
            request = {"version": "0.0.2", "request_id": str(uuid.uuid4())}
            accepted = owner.post("/api/admin/updates", json=request)
            assert accepted.status_code == 202
            assert accepted.json()["job"]["id"] == request["request_id"]
            assert owner.post("/api/admin/updates", json=request).status_code == 202
            assert owner.post("/api/admin/updates", json={**request, "request_id": str(uuid.uuid4())}).status_code == 409
            values["UPDATE_CONTROL_TOKEN"] = secrets.token_urlsafe(48)
            installer.write_env(values)
            with pytest.raises(UpdateServiceError) as error:
                client.request("GET", "/status")
            assert error.value.status == 403
            rotated = UpdateClient(str(socket), values["UPDATE_CONTROL_TOKEN"])
            assert rotated.request("GET", "/status")["job"]["id"] == request["request_id"]
        finally:
            server.shutdown()
            worker.join(timeout=2)


def test_update_guard_pauses_edits_but_keeps_viewers_readable(owner, application, tmp_path, monkeypatch):
    record = create(owner, "during-update")
    guard = tmp_path / "busy"
    guard.write_text("updating")
    monkeypatch.setenv("PREZA_UPDATE_GUARD", str(guard))
    with TestClient(create_app(application.state.storage.root), base_url=ORIGIN, headers={"Origin": ORIGIN}) as client:
        login = client.post("/api/admin/login", json={"key": KEY}).json()
        client.headers["X-CSRF-Token"] = login["csrf"]
        assert client.delete("/api/admin/presentations/" + record["id"]).status_code == 409
        assert client.post("/api/admin/presentations/" + record["id"] + "/live", json={"active": True, "slide": 1}).status_code == 409
        assert client.get("/api/admin/presentations").status_code == 200
        assert client.get("/p/during-update").status_code == 200
        assert client.get(record["slide_base"] + "0").status_code == 200
        guard.unlink()
        assert client.post("/api/admin/presentations/" + record["id"] + "/live", json={"active": True, "slide": 1}).status_code == 200


def test_interrupted_update_uses_persisted_rollback_journal(prepared, monkeypatch):
    root, values = prepared
    controller = updater.Controller(root)
    controller.job = {"id": str(uuid.uuid4()), "target_version": "0.0.2", "previous_version": "0.0.1", "state": "running"}
    controller.save_job()
    (root / "update-pending.json").write_text(json.dumps({"request_id": controller.job["id"],
                                                         "target_version": "0.0.2", "previous_version": "0.0.1"}))
    calls = []

    class ImmediateThread:
        def __init__(self, target, kwargs=None, **options):
            self.target, self.kwargs = target, kwargs or {}
        def start(self):
            self.target(**self.kwargs)

    def restore(*arguments):
        calls.append(arguments)
        (root / "update-pending.json").unlink()
        return 0

    monkeypatch.setattr(updater.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(controller, "run_installer", restore)
    monkeypatch.setattr(installer, "healthy", lambda *args, **kwargs: True)
    controller.recover()
    assert calls == [("rollback",)]
    assert controller.status()["job"]["state"] == "failed"
    assert not controller.status()["updating"]


def test_update_worker_reports_verified_completion_and_persists_result(prepared, monkeypatch):
    root, values = prepared
    controller = updater.Controller(root)
    monkeypatch.setattr(updater, "latest_release", lambda: "0.0.2")
    monkeypatch.setattr(updater.threading, "Thread", DeferredThread)
    controller.check()
    request = {"version": "0.0.2", "request_id": str(uuid.uuid4())}
    controller.request_update(request)
    calls = []

    def complete(*arguments):
        calls.append(arguments)
        installer.record_install({**values, "VERSION": "0.0.2"})
        return 0

    monkeypatch.setattr(controller, "run_installer", complete)
    monkeypatch.setattr(installer, "healthy", lambda *args, **kwargs: True)
    controller.worker()
    assert calls == [("update", "0.0.2")]
    assert controller.status()["job"]["state"] == "completed"
    assert updater.Controller(root).status()["job"]["state"] == "completed"


def test_control_never_rolls_back_an_unrelated_manual_update(prepared, monkeypatch):
    root, values = prepared
    controller = updater.Controller(root)
    controller.job = {"id": str(uuid.uuid4()), "target_version": "0.0.2", "previous_version": "0.0.1", "state": "queued"}
    manual_journal = {"request_id": None, "target_version": "0.0.3", "previous_version": "0.0.1"}
    (root / "update-pending.json").write_text(json.dumps(manual_journal))
    calls = []
    monkeypatch.setattr(controller, "run_installer", lambda *args: calls.append(args) or 1)
    monkeypatch.setattr(installer, "healthy", lambda *args, **kwargs: True)
    controller.worker()
    assert calls == [("update", "0.0.2")]
    assert json.loads((root / "update-pending.json").read_text()) == manual_journal
    assert controller.status()["job"]["state"] == "recovery_required"
