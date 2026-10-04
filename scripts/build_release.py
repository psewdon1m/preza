#!/usr/bin/env python3
"""Build signed release assets around an already-published immutable image."""
import argparse
import io
import json
import re
import subprocess
import tarfile
from pathlib import Path

from release_verify import IMAGE_PREFIX, check_public_key, sha256, validate_version, verify_manifest

SOURCE = Path(__file__).resolve().parents[1]
BUNDLE_FILES = ("compose.production.yml", "nginx/preza.conf", "scripts/install.py", "scripts/release_verify.py")


def build_release(version, image, signing_key, output):
    validate_version(version)
    if not re.fullmatch(re.escape(IMAGE_PREFIX) + r"[a-f0-9]{64}", image):
        raise ValueError("Use the published GHCR image by immutable SHA-256 digest")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    public = output / "public-key.pem"
    subprocess.run(["openssl", "pkey", "-in", str(signing_key), "-pubout", "-out", str(public)], check=True)
    check_public_key(public)
    bundle = output / f"preza-{version}-install.tar.gz"
    with tarfile.open(bundle, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        for filename in BUNDLE_FILES:
            data = (SOURCE / filename).read_bytes()
            entry = tarfile.TarInfo("preza/" + filename)
            entry.size, entry.mode, entry.mtime, entry.uid, entry.gid = len(data), 0o644, 0, 0, 0
            archive.addfile(entry, io.BytesIO(data))
    manifest = dict(schema_version=1, service="preza", role="application", version=version,
                    tag="preza-v" + version, image=image, platforms=["linux/amd64", "linux/arm64"],
                    bundle=dict(file=bundle.name, size=bundle.stat().st_size, sha256=sha256(bundle)))
    path = output / "release.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    signature = output / "release.json.sig"
    subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(signing_key), "-out", str(signature), str(path)], check=True)
    verify_manifest(path, signature, public, version)
    template = (SOURCE / "scripts/bootstrap.sh.in").read_text()
    replacements = dict(VERSION=version, PUBLIC_KEY=public.read_text(),
                        VERIFIER=(SOURCE / "scripts/release_verify.py").read_text(),
                        BOOTSTRAP=(SOURCE / "scripts/bootstrap.py").read_text())
    for key, value in replacements.items():
        template = template.replace("@@" + key + "@@", value)
    if "@@" in template:
        raise ValueError("Unresolved bootstrap placeholders")
    bootstrap = output / "bootstrap.sh"
    bootstrap.write_text(template, newline="\n")
    bootstrap.chmod(0o755)
    print("Signed release assets prepared:", version)
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--signing-key", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=SOURCE / "dist")
    arguments = parser.parse_args()
    build_release(arguments.version, arguments.image, arguments.signing_key, arguments.output)
