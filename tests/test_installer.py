import json
import shutil
import subprocess
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
    for key in ("ACCESS_KEY", "SESSION_SECRET", "ADMIN_KEY_HASH", "DATA_VOLUME"):
        assert updated[key] == values[key]
    journal = json.loads((root / "previous-update.json").read_text())
    assert not (root / "update-pending.json").exists()
    installer.rollback(updated, journal)
    assert running == dict(version="0.0.1", data="before")
    assert installer.load_env(root / ".env")["VERSION"] == "0.0.1"
