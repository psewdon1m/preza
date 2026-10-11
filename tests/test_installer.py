import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import install as installer
from build_release import build_release
from release_verify import safe_extract, sha256


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    root, trust = tmp_path / "preza", tmp_path / "trust.pem"
    root.mkdir()
    (root / "releases").mkdir()
    private = tmp_path / "test-only-private.pem"
    subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:3072",
                    "-out", str(private)], check=True, capture_output=True)
    for version in ("0.0.1", "0.0.2"):
        output = tmp_path / version
        release = build_release(version, "ghcr.io/psewdon1m/preza@sha256:" + ("a" if version == "0.0.1" else "b") * 64,
                                private, output)
        unpacked = tmp_path / ("unpack-" + version)
        safe_extract(output / release["bundle"]["file"], unpacked)
        directory = root / "releases" / version
        shutil.move(unpacked / "preza", directory)
        shutil.copyfile(output / "release.json", directory / "release.json")
        shutil.copyfile(output / "release.json.sig", directory / "release.json.sig")
        (directory / ".release-digest").write_text(sha256(directory / "release.json") + "\n")
        shutil.copyfile(output / "public-key.pem", trust)
    monkeypatch.setattr(installer, "ROOT", root)
    monkeypatch.setattr(installer, "TRUST", trust)
    monkeypatch.setattr(installer, "prerequisites", lambda: None)
    (root / "current").symlink_to(root / "releases/0.0.1", target_is_directory=True)
    installer.prepare()
    values = installer.load_env(root / ".env")
    values["ACCESS_KEY"] = " 1111 ё "
    installer.write_env(values)
    installer.runtime_files(values)
    installer.record_install(values)
    return root, values


def test_prepare_preserves_secrets_and_runtime_does_not_receive_raw_key(prepared):
    root, values = prepared
    original = (root / ".env").read_bytes()
    with pytest.raises(RuntimeError, match="already exists"):
        installer.prepare()
    assert (root / ".env").read_bytes() == original
    assert (root / ".env").stat().st_mode & 0o777 == 0o600
    runtime = (root / "runtime.env").read_text()
    assert values["ACCESS_KEY"] not in runtime
    assert "ADMIN_KEY_HASH=pbkdf2_sha256$" in runtime
    installer.validate_config(values, installer.manifest(root / "current"), root / "current")


def test_existing_installation_gets_update_control_without_rotating_other_secrets(prepared):
    root, values = prepared
    values.pop("UPDATE_CONTROL_TOKEN")
    installer.atomic_write(root / ".env", "\n".join(line for line in (root / ".env").read_text().splitlines()
                                                   if not line.startswith("UPDATE_CONTROL_TOKEN=")) + "\n")
    original = {key: values[key] for key in ("SESSION_SECRET", "ADMIN_KEY_HASH", "ACCESS_KEY", "DATA_VOLUME")}
    installer.runtime_files(values)
    token = values["UPDATE_CONTROL_TOKEN"]
    installer.runtime_files(values)
    assert values["UPDATE_CONTROL_TOKEN"] == token
    assert {key: values[key] for key in original} == original
    assert (root / "update-control").stat().st_mode & 0o777 == 0o750
    assert (root / "update-control").stat().st_gid == 10001


@pytest.mark.parametrize("key,value", [("ACCESS_KEY", ""), ("PUBLIC_URL", "http://preza.shmoza.net"),
                                      ("LISTEN_PORT", "18381"), ("PREZA_IMAGE", "ghcr.io/psewdon1m/preza:latest")])
def test_install_rejects_missing_input_port_conflicts_and_changed_locks(prepared, key, value):
    root, values = prepared
    with pytest.raises(ValueError):
        installer.validate_config({**values, key: value}, installer.manifest(root / "current"), root / "current")


def simulate_engine(monkeypatch, root, running):
    calls = []
    monkeypatch.setattr(installer, "run", lambda command, **kwargs: calls.append(command))
    monkeypatch.setattr(installer, "compose", lambda directory, *args, **kwargs: calls.append(list(args)))
    monkeypatch.setattr(installer, "healthy", lambda values, **kwargs: running["version"] == values["VERSION"])
    monkeypatch.setattr(installer, "setup_updater", lambda directory: None)
    monkeypatch.setattr(installer, "data_snapshot", lambda values: {"presentations": [], "settings": {}, "files": {running["data"]: "hash"}})
    backup = root / "backups/test"
    backup.mkdir(parents=True)
    (backup / "data.tar.gz").write_bytes(b"fixture backup")
    monkeypatch.setattr(installer, "backup_data", lambda values: backup)
    monkeypatch.setattr(installer, "restore_data", lambda values, source: running.update(data="before"))
    return calls


def test_failed_update_restores_old_version_configuration_and_data(prepared, monkeypatch):
    root, values = prepared
    running = dict(version="0.0.1", data="before")
    calls = simulate_engine(monkeypatch, root, running)
    def start(directory, configuration):
        if configuration["VERSION"] == "0.0.2":
            running.update(version="0.0.2", data="new schema")
            raise RuntimeError("Unhealthy new release")
        running["version"] = configuration["VERSION"]
        installer.record_install(configuration)
    monkeypatch.setattr(installer, "start", start)
    with pytest.raises(RuntimeError, match="Unhealthy"):
        installer.update(values, root / "current", "0.0.2")
    assert running == dict(version="0.0.1", data="before")
    assert (root / "current").resolve() == root / "releases/0.0.1"
    assert installer.load_env(root / ".env")["VERSION"] == "0.0.1"
    assert not (root / "update-pending.json").exists()
    assert not any("nginx" in str(call) for call in calls)


def test_successful_update_preserves_inputs_and_can_roll_back(prepared, monkeypatch):
    root, values = prepared
    running = dict(version="0.0.1", data="before")
    simulate_engine(monkeypatch, root, running)
    def start(directory, configuration):
        running["version"] = configuration["VERSION"]
        installer.record_install(configuration)
    monkeypatch.setattr(installer, "start", start)
    installer.update(values, root / "current", "0.0.2")
    updated = installer.load_env(root / ".env")
    assert updated["VERSION"] == "0.0.2"
    for key in ("ACCESS_KEY", "SESSION_SECRET", "ADMIN_KEY_HASH", "DATA_VOLUME", "UPDATE_CONTROL_TOKEN"):
        assert updated[key] == values[key]
    journal = json.loads((root / "previous-update.json").read_text())
    assert not (root / "update-pending.json").exists()
    installer.rollback(updated, journal)
    assert running == dict(version="0.0.1", data="before")
    assert installer.load_env(root / ".env")["VERSION"] == "0.0.1"


@pytest.mark.parametrize("damage", [None, "settings", "file"])
def test_updates_keep_real_presentations_passwords_urls_files_and_sessions(prepared, monkeypatch, damage):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from conftest import KEY, ORIGIN, create

    root, values = prepared
    data = root / "data"
    monkeypatch.setenv("ADMIN_ACCESS_KEY", KEY)
    monkeypatch.setenv("SESSION_SECRET", values["SESSION_SECRET"])
    monkeypatch.setenv("PUBLIC_URL", ORIGIN)
    monkeypatch.setenv("PDF_RENDER_DPI", "72")
    monkeypatch.setenv("PREZA_VERSION", "0.0.1")
    monkeypatch.setenv("ADMIN_KEY_HASH", values["ADMIN_KEY_HASH"])
    with TestClient(create_app(data), base_url=ORIGIN, headers={"Origin": ORIGIN}) as owner:
        login = owner.post("/api/admin/login", json={"key": KEY}).json()
        owner.headers["X-CSRF-Token"] = login["csrf"]
        public_deck = create(owner, "stable-public", default_mode="slides", allow_switch="false")
        protected = create(owner, "stable-private", password="1111", default_mode="scroll")
        owner.post("/api/admin/presentations/" + protected["id"] + "/live", json={"active": True, "slide": 1})
        with TestClient(owner.app, base_url=ORIGIN, headers={"Origin": ORIGIN}) as guest:
            guest.post("/api/public/presentations/stable-private/unlock", json={"password": "1111"})
            guest_cookies = dict(guest.cookies)
        owner_cookies = dict(owner.cookies)
        original = {item["id"]: item for item in owner.get("/api/admin/presentations").json()["presentations"]}

    monkeypatch.setattr(installer, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(installer, "compose", lambda *args, **kwargs: None)
    monkeypatch.setattr(installer, "setup_updater", lambda directory: None)
    monkeypatch.setattr(installer, "healthy", lambda *args, **kwargs: True)

    def local_volume(values, code, *, backup=None, **options):
        # Execute the actual backup, restore and audit programs against a real data directory.
        code = code.replace("'/data'", repr(str(data))).replace("'file:/data/", "'file:" + str(data) + "/")
        code = code.replace("'/data/", "'" + str(data) + "/")
        if backup:
            code = code.replace("'/backup/", "'" + str(backup) + "/")
        return subprocess.run([sys.executable, "-c", code], check=True, **{key: value for key, value in options.items() if key in ("capture_output", "text")})

    monkeypatch.setattr(installer, "volume_command", local_volume)

    def start(directory, configuration):
        monkeypatch.setenv("PREZA_VERSION", configuration["VERSION"])
        with TestClient(create_app(data), base_url=ORIGIN) as candidate:
            assert candidate.get("/api/health").json()["version"] == configuration["VERSION"]
            if configuration["VERSION"] == "0.0.2" and damage == "settings":
                with candidate.app.state.storage.connect() as db:
                    db.execute("UPDATE presentations SET password_hash='',allow_switch=1")
            if configuration["VERSION"] == "0.0.2" and damage == "file":
                (candidate.app.state.storage.version_path(public_deck) / "source.pdf").write_bytes(b"corrupted")
        installer.record_install(configuration)

    monkeypatch.setattr(installer, "start", start)
    if damage:
        with pytest.raises(RuntimeError, match="changed during update"):
            installer.update(values, root / "current", "0.0.2")
    else:
        installer.update(values, root / "current", "0.0.2")

    assert not (root / "update-control/busy").exists()
    assert not (root / "update-pending.json").exists()
    expected_version = "0.0.1" if damage else "0.0.2"
    assert installer.load_env(root / ".env")["VERSION"] == expected_version
    with TestClient(create_app(data), base_url=ORIGIN, headers={"Origin": ORIGIN}) as owner:
        owner.cookies.update(owner_cookies)
        assert owner.get("/api/admin/session").json()["csrf"] == login["csrf"]
        restored = {item["id"]: item for item in owner.get("/api/admin/presentations").json()["presentations"]}
        assert restored == original
        assert owner.get("/p/stable-public").status_code == 200
        assert owner.get(public_deck["slide_base"] + "0").status_code == 200
        with TestClient(owner.app, base_url=ORIGIN) as guest:
            assert guest.get(protected["slide_base"] + "1").status_code == 401
            guest.cookies.update(guest_cookies)
            assert guest.get(protected["slide_base"] + "1").status_code == 200
            assert guest.get("/api/public/presentations/stable-private").json()["current_slide"] == 1
    backup = json.loads((root / "previous-update.json").read_text())["backup"] if not damage else next((root / "backups").iterdir())
    installer.verify_data_snapshot(json.loads((Path(backup) / "presentations.json").read_text()), installer.data_snapshot(values))
