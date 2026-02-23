import asyncio
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse


DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
SOURCE_DIR = DATA_DIR / "source"
SLIDES_DIR = DATA_DIR / "slides"
LOCK = asyncio.Lock()


def ensure_dirs() -> None:
    SOURCE_DIR.mkdir(parents=True, exist_ok=True)
    SLIDES_DIR.mkdir(parents=True, exist_ok=True)


def natural_page_key(path: Path) -> tuple[int, str]:
    match = re.search(r"(\d+)(?=\.png$)", path.name)
    if not match:
        return (10**9, path.name)
    return (int(match.group(1)), path.name)


def slide_files() -> list[Path]:
    files = [p for p in SLIDES_DIR.glob("*.png") if p.is_file()]
    return sorted(files, key=natural_page_key)


def has_presentation() -> bool:
    return (SOURCE_DIR / "current.pdf").exists() or bool(slide_files())


def clear_storage() -> None:
    pdf_path = SOURCE_DIR / "current.pdf"
    if pdf_path.exists():
        pdf_path.unlink()
    for slide in slide_files():
        slide.unlink()


def convert_pdf_to_png(pdf_path: Path, output_dir: Path) -> None:
    dpi = int(os.getenv("PDF_RENDER_DPI", "300"))
    prefix = output_dir / "slide"
    cmd = [
        "pdftoppm",
        "-png",
        "-r",
        str(dpi),
        str(pdf_path),
        str(prefix),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "pdftoppm failed")


def replace_slides_from_temp(temp_slides_dir: Path, temp_pdf_path: Path) -> None:
    generated = sorted(temp_slides_dir.glob("*.png"), key=natural_page_key)
    if not generated:
        raise RuntimeError("PDF was processed but no slides were generated")

    clear_storage()
    shutil.move(str(temp_pdf_path), str(SOURCE_DIR / "current.pdf"))
    for slide in generated:
        shutil.move(str(slide), str(SLIDES_DIR / slide.name))


app = FastAPI(title="Simple Presentation API")


@app.on_event("startup")
async def startup() -> None:
    ensure_dirs()


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/slides")
async def get_slides() -> dict[str, object]:
    files = slide_files()
    return {
        "hasPresentation": has_presentation(),
        "slides": [f"/slides/{p.name}" for p in files],
        "count": len(files),
    }


@app.post("/api/upload")
async def upload_pdf(file: UploadFile = File(...)) -> JSONResponse:
    filename = (file.filename or "").lower()
    if not filename.endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    ensure_dirs()

    async with LOCK:
        if has_presentation():
            raise HTTPException(
                status_code=409,
                detail="Presentation already exists. Delete current presentation first.",
            )

        with tempfile.TemporaryDirectory(prefix="pdf-upload-") as tmp_root_str:
            tmp_root = Path(tmp_root_str)
            tmp_pdf = tmp_root / "upload.pdf"
            tmp_slides = tmp_root / "slides"
            tmp_slides.mkdir(parents=True, exist_ok=True)

            with tmp_pdf.open("wb") as out:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)

            try:
                convert_pdf_to_png(tmp_pdf, tmp_slides)
                replace_slides_from_temp(tmp_slides, tmp_pdf)
            except RuntimeError as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc

    return JSONResponse({"ok": True})


@app.delete("/api/presentation")
async def delete_presentation() -> dict[str, bool]:
    async with LOCK:
        clear_storage()
    return {"ok": True}

