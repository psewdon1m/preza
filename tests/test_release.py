import io
import ast
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from build_release import build_release
from install import load_env
from release_verify import safe_extract, validate_download_url, verify_manifest


@pytest.fixture(scope="module")
def assets(tmp_path_factory):
    root = tmp_path_factory.mktemp("signed-release")
    private = root / "test-only-private.pem"
    subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:3072",
                    "-out", str(private)], check=True, capture_output=True)
    output = root / "release"
    release = build_release("0.0.1", "ghcr.io/psewdon1m/preza@sha256:" + "a" * 64, private, output)
    return output, private, release


def test_signed_bundle_and_exact_version_bootstrap(assets, tmp_path):
    output, _, expected = assets
    assert verify_manifest(output / "release.json", output / "release.json.sig",
                           output / "public-key.pem", "0.0.1") == expected
    safe_extract(output / expected["bundle"]["file"], tmp_path)
    assert (tmp_path / "preza/scripts/install.py").is_file()
    subprocess.run(["sh", "-n", str(output / "bootstrap.sh")], check=True)
    bootstrap = (output / "bootstrap.sh").read_text()
    assert "@@" not in bootstrap and "BEGIN PUBLIC KEY" in bootstrap
    assert "preza-v0.0.1" in bootstrap
    embedded = bootstrap.split("python3 - <<'PREZA_BOOTSTRAP_PY'\n", 1)[1].rsplit("\nPREZA_BOOTSTRAP_PY", 1)[0]
    ast.parse(embedded)


def test_signature_tampering_and_wrong_version_are_rejected(assets, tmp_path):
    output, _, _ = assets
    modified = tmp_path / "modified.json"
    modified.write_bytes((output / "release.json").read_bytes().replace(b'"0.0.1"', b'"9.9.9"'))
    with pytest.raises(ValueError, match="signature"):
        verify_manifest(modified, output / "release.json.sig", output / "public-key.pem", "0.0.1")
    with pytest.raises(ValueError, match="identity"):
        verify_manifest(output / "release.json", output / "release.json.sig", output / "public-key.pem", "0.0.2")


def test_valid_signature_cannot_authorize_other_image_registry(assets, tmp_path):
    output, private, release = assets
    path, signature = tmp_path / "bad-image.json", tmp_path / "bad-image.sig"
    release = {**release, "image": "evil.example/preza@sha256:" + "a" * 64}
    path.write_text(json.dumps(release))
    subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(private), "-out", str(signature), str(path)], check=True)
    with pytest.raises(ValueError, match="allow-listed"):
        verify_manifest(path, signature, output / "public-key.pem", "0.0.1")


@pytest.mark.parametrize("name,kind", [("../outside", "file"), ("/tmp/outside", "file"),
    ("preza/../../outside", "file"), ("preza/link", "symlink"), ("preza/device", "device")])
def test_archive_traversal_links_and_devices_are_rejected(tmp_path, name, kind):
    path = tmp_path / "bad.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        member = tarfile.TarInfo(name)
        if kind == "symlink":
            member.type, member.linkname = tarfile.SYMTYPE, "/etc/passwd"
        elif kind == "device":
            member.type = tarfile.CHRTYPE
        else:
            member.size = 3
        archive.addfile(member, io.BytesIO(b"bad") if kind == "file" else None)
    with pytest.raises(ValueError, match="Unsafe"):
        safe_extract(path, tmp_path / "extracted")
    assert not (tmp_path.parent / "outside").exists()


@pytest.mark.parametrize("url", ["http://github.com/psewdon1m/preza", "https://evil.example/file",
                                "https://user:pass@github.com/file", "https://github.com:444/file"])
def test_download_origins_are_restricted(url):
    with pytest.raises(ValueError):
        validate_download_url(url)


def test_environment_parser_preserves_opaque_access_key_without_execution(tmp_path):
    path = tmp_path / ".env"
    key = " 1111 ё $HOME $(touch /tmp/no) ' \" \n "
    path.write_text("ACCESS_KEY=" + json.dumps(key, ensure_ascii=False) + "\n")
    assert load_env(path)["ACCESS_KEY"] == key
    path.write_text("ACCESS_KEY= 1111 \n")
    assert load_env(path)["ACCESS_KEY"] == " 1111 "
