"""Shared bootstrap/update verification. Uses Python stdlib and OpenSSL only."""
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath

REPOSITORY = "psewdon1m/preza"
IMAGE_PREFIX = "ghcr.io/psewdon1m/preza@sha256:"
DOWNLOAD_HOSTS = {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}
MAX_BUNDLE = 20 * 1024 * 1024


def validate_version(version):
    if not re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", version):
        raise ValueError("Expected an exact numeric version, for example 0.0.1")
    return version


def supported_host():
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "aarch64"):
        raise RuntimeError("Only Linux amd64/arm64 is supported")
    values = {}
    for line in Path("/etc/os-release").read_text().splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value.strip('"')
    if values.get("ID") not in ("debian", "ubuntu"):
        raise RuntimeError("Supported hosts: Debian 12+ or Ubuntu 22.04+")
    minimum = 12 if values["ID"] == "debian" else 22
    if int(values.get("VERSION_ID", "0").split(".")[0]) < minimum:
        raise RuntimeError("Unsupported OS version")


def release_url(version):
    validate_version(version)
    return f"https://github.com/{REPOSITORY}/releases/download/preza-v{version}/"


def validate_download_url(url):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in DOWNLOAD_HOSTS or parsed.username
            or parsed.password or parsed.port not in (None, 443)):
        raise ValueError("Artifact URL is outside the allowed HTTPS hosts")


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_download_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url, destination, limit):
    validate_download_url(url)
    destination = Path(destination)
    opener = urllib.request.build_opener(SafeRedirect())
    for attempt in range(3):
        try:
            start = time.monotonic()
            request = urllib.request.Request(url, headers={"User-Agent": "preza-installer/1"})
            with opener.open(request, timeout=10) as response, destination.open("wb") as out:
                validate_download_url(response.url)
                if int(response.headers.get("Content-Length", "0")) > limit:
                    raise ValueError("Artifact exceeds the download limit")
                total = 0
                while chunk := response.read(65536):
                    total += len(chunk)
                    if total > limit or time.monotonic() - start > 120:
                        raise ValueError("Artifact exceeds size/time limits")
                    out.write(chunk)
            return
        except (OSError, urllib.error.URLError):
            destination.unlink(missing_ok=True)
            if attempt == 2:
                raise
            time.sleep(attempt + 1)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_public_key(key):
    result = subprocess.run(["openssl", "pkey", "-pubin", "-in", str(key), "-text", "-noout"],
                            capture_output=True, text=True, timeout=10)
    match = re.search(r"Public-Key:\s*\((\d+) bit\)", result.stdout)
    if result.returncode or not match or int(match.group(1)) < 3072 or "Modulus:" not in result.stdout:
        raise ValueError("A valid RSA public key of at least 3072 bits is required")


def verify_manifest(manifest_path, signature, key, version):
    validate_version(version)
    check_public_key(key)
    if Path(manifest_path).stat().st_size > 65536 or Path(signature).stat().st_size > 8192:
        raise ValueError("Manifest/signature exceeds limits")
    result = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(key),
                             "-signature", str(signature), str(manifest_path)],
                            capture_output=True, timeout=10)
    if result.returncode:
        raise ValueError("Release signature verification failed")
    manifest = json.loads(Path(manifest_path).read_bytes())
    if (manifest.get("schema_version") != 1 or manifest.get("service") != "preza"
            or manifest.get("role") != "application" or manifest.get("version") != version
            or manifest.get("tag") != "preza-v" + version):
        raise ValueError("Signed manifest has an incorrect schema or release identity")
    image = manifest.get("image", "")
    if not re.fullmatch(re.escape(IMAGE_PREFIX) + r"[0-9a-f]{64}", image):
        raise ValueError("Image must be the allow-listed GHCR image with a complete SHA-256 digest")
    if manifest.get("platforms") != ["linux/amd64", "linux/arm64"]:
        raise ValueError("Incorrect release platforms")
    bundle = manifest.get("bundle", {})
    if (bundle.get("file") != f"preza-{version}-install.tar.gz"
            or not re.fullmatch(r"[0-9a-f]{64}", str(bundle.get("sha256", "")))
            or type(bundle.get("size")) is not int or not 0 < bundle["size"] <= MAX_BUNDLE):
        raise ValueError("Invalid signed bundle coordinates")
    return manifest


def safe_extract(bundle, destination):
    destination = Path(destination)
    with tarfile.open(bundle, "r:gz") as archive:
        members = archive.getmembers()
        if len(members) > 128 or sum(member.size for member in members) > 32 * 1024 * 1024:
            raise ValueError("Archive exceeds member/expansion limits")
        seen = set()
        for member in members:
            path = PurePosixPath(member.name)
            if (path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != "preza"
                    or "\\" in member.name or member.name in seen or not (member.isfile() or member.isdir())):
                raise ValueError("Unsafe release archive entry")
            seen.add(member.name)
        required = {"preza/compose.production.yml", "preza/scripts/install.py",
                    "preza/scripts/release_verify.py", "preza/nginx/preza.conf"}
        if not required.issubset(seen):
            raise ValueError("Incomplete release bundle")
        # Copy bytes explicitly: archive owners, permissions and links are never applied.
        for member in members:
            target = destination.joinpath(*PurePosixPath(member.name).parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True, mode=0o755)
            else:
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                with archive.extractfile(member) as source, target.open("xb") as out:
                    shutil.copyfileobj(source, out)
                target.chmod(0o644)


def fetch_release(version, key, destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="preza-release-") as scratch:
        scratch = Path(scratch)
        manifest_path, signature = scratch / "release.json", scratch / "release.json.sig"
        download(release_url(version) + "release.json", manifest_path, 65536)
        download(release_url(version) + "release.json.sig", signature, 8192)
        manifest = verify_manifest(manifest_path, signature, key, version)
        bundle = scratch / manifest["bundle"]["file"]
        download(release_url(version) + bundle.name, bundle, MAX_BUNDLE)
        if bundle.stat().st_size != manifest["bundle"]["size"] or sha256(bundle) != manifest["bundle"]["sha256"]:
            raise ValueError("Release bundle checksum/size mismatch")
        safe_extract(bundle, destination)
        extracted = destination / "preza"
        shutil.copyfile(manifest_path, extracted / "release.json")
        shutil.copyfile(signature, extracted / "release.json.sig")
        (extracted / ".release-digest").write_text(sha256(manifest_path) + "\n")
        return extracted, manifest
