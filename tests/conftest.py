import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("WEB_DIR", str(Path(__file__).resolve().parents[1] / "web"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from app.main import create_app

ORIGIN = "https://preza.shmoza.net"
KEY = " 1111 ё "


def make_pdf(pages=3):
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               ("<< /Type /Pages /Kids [" + " ".join(f"{4+i*2} 0 R" for i in range(pages)) +
                f"] /Count {pages} >>").encode(),
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    for i in range(pages):
        objects.append(("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 320 180] "
                        f"/Resources << /Font << /F1 3 0 R >> >> /Contents {5+i*2} 0 R >>").encode())
        stream = f"0.9 0.95 0.9 rg 0 0 320 180 re f 0.1 0.2 0.1 rg BT /F1 24 Tf 40 85 Td (Slide {i+1}) Tj ET".encode()
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
    result, offsets = bytearray(b"%PDF-1.4\n"), [0]
    for n, obj in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(f"{n} 0 obj\n".encode() + obj + b"\nendobj\n")
    start = len(result)
    result.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        result.extend(f"{offset:010d} 00000 n \n".encode())
    result.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF".encode())
    return bytes(result)


@pytest.fixture
def application(tmp_path, monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret-" * 3)
    monkeypatch.setenv("ADMIN_ACCESS_KEY", KEY)
    monkeypatch.delenv("ADMIN_KEY_HASH", raising=False)
    monkeypatch.setenv("PUBLIC_URL", ORIGIN)
    monkeypatch.setenv("PDF_RENDER_DPI", "72")
    return create_app(tmp_path)


@pytest.fixture
def client(application):
    with TestClient(application, base_url=ORIGIN, headers={"Origin": ORIGIN}) as client:
        yield client


@pytest.fixture
def owner(client):
    response = client.post("/api/admin/login", json={"key": KEY})
    assert response.status_code == 200
    client.headers["X-CSRF-Token"] = response.json()["csrf"]
    return client


def create(client, slug="deck", password="", **settings):
    response = client.post("/api/admin/presentations",
        data={"title": "Тестовая презентация", "slug": slug, "password": password, **settings},
        files={"file": ("slides.pdf", make_pdf(), "application/pdf")})
    assert response.status_code == 201, response.text
    return response.json()


def edit(client, record, **values):
    return client.put("/api/admin/presentations/" + record["id"], data={
        "title": record["title"], "slug": record["slug"], "default_mode": record["default_mode"],
        "allow_switch": str(record["allow_switch"]), "expected_revision": record["revision"], **values,
    })
