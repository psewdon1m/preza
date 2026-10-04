"""SQLite metadata and immutable versions of rendered presentations."""
import re
import shutil
import sqlite3
import subprocess
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS presentations (
    id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
    default_mode TEXT NOT NULL CHECK(default_mode IN ('scroll','slides')),
    allow_switch INTEGER NOT NULL, password_hash TEXT NOT NULL DEFAULT '',
    access_version TEXT NOT NULL, content_version TEXT NOT NULL,
    slide_count INTEGER NOT NULL, live_active INTEGER NOT NULL DEFAULT 0,
    current_slide INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 1,
    legacy INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_slug(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value) or len(value) > 80:
        raise ValueError("Адрес: латинские строчные буквы, цифры и дефис; до 80 символов")
    return value


def page_key(path: Path) -> int:
    match = re.search(r"(\d+)\.png$", path.name)
    return int(match.group(1)) if match else 10**9


class Storage:
    def __init__(self, root: Path):
        self.root = root
        self.presentations = root / "presentations"
        self.staging = root / ".staging"
        self.database = root / "preza.sqlite3"

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def initialize(self):
        self.presentations.mkdir(parents=True, exist_ok=True)
        self.staging.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
        self.migrate_legacy()
        for entry in self.staging.iterdir():
            if entry.is_dir():
                shutil.rmtree(entry)
        with self.connect() as db:
            active = {row["id"]: row["content_version"] for row in db.execute("SELECT * FROM presentations")}
        for directory in self.presentations.iterdir():
            if not directory.is_dir():
                continue
            if directory.name not in active:
                shutil.rmtree(directory)
            else:
                for version in directory.iterdir():
                    if version.is_dir() and version.name != active[directory.name]:
                        shutil.rmtree(version)

    def get(self, *, id: str | None = None, slug: str | None = None):
        with self.connect() as db:
            row = db.execute("SELECT * FROM presentations WHERE " + ("id=?" if id else "slug=?"),
                             (id or slug,)).fetchone()
            return dict(row) if row else None

    def all(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM presentations ORDER BY created_at DESC")]

    def insert(self, record: dict):
        with self.connect() as db:
            db.execute("INSERT INTO presentations (" + ",".join(record) + ") VALUES (" +
                       ",".join("?" for _ in record) + ")", tuple(record.values()))

    def version_path(self, record: dict) -> Path:
        return self.presentations / record["id"] / record["content_version"]

    def migrate_legacy(self):
        with self.connect() as db:
            if db.execute("SELECT 1 FROM settings WHERE key='legacy_migrated'").fetchone():
                return
        slides = sorted((self.root / "slides").glob("*.png"), key=page_key)
        if slides:
            id, version = uuid.uuid4().hex, uuid.uuid4().hex
            destination = self.presentations / id / version
            destination.mkdir(parents=True)
            try:
                for index, slide in enumerate(slides, 1):
                    shutil.copy2(slide, destination / f"slide-{index}.png")
                pdf = self.root / "source" / "current.pdf"
                if pdf.exists():
                    shutil.copy2(pdf, destination / "source.pdf")
                record = dict(id=id, slug="presentation", title="Презентация", default_mode="scroll",
                              allow_switch=1, password_hash="", access_version=uuid.uuid4().hex,
                              content_version=version, slide_count=len(slides), legacy=1,
                              created_at=now(), updated_at=now())
                with self.connect() as db:
                    db.execute("INSERT INTO presentations (" + ",".join(record) + ") VALUES (" +
                               ",".join("?" for _ in record) + ")", tuple(record.values()))
                    db.execute("INSERT INTO settings VALUES ('legacy_migrated','1')")
            except BaseException:
                shutil.rmtree(destination.parent, ignore_errors=True)
                raise
        else:
            with self.connect() as db:
                db.execute("INSERT INTO settings VALUES ('legacy_migrated','1')")


def render_pdf(pdf: Path, destination: Path, dpi: int, max_pages: int, timeout: int):
    info = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True, timeout=30)
    match = re.search(r"^Pages:\s+(\d+)", info.stdout, re.M)
    if info.returncode or not match:
        raise ValueError("Не удалось прочитать PDF. Проверьте файл; зашифрованные PDF не поддерживаются.")
    pages = int(match.group(1))
    if not 1 <= pages <= max_pages:
        raise ValueError(f"В PDF должно быть от 1 до {max_pages} страниц")
    boxes = subprocess.run(["pdfinfo", "-f", "1", "-l", str(pages), str(pdf)],
                           capture_output=True, text=True, timeout=30)
    sizes = re.findall(r"(?:Page\s+\d+\s+size|Page size):\s+([\d.]+)\s+x\s+([\d.]+)", boxes.stdout)
    if boxes.returncode or len(sizes) != pages:
        raise ValueError("Не удалось определить размеры страниц PDF")
    if any(float(w) * float(h) * (dpi / 72) ** 2 > 40_000_000 for w, h in sizes):
        raise ValueError("Слишком большой размер страницы PDF для выбранного DPI")
    result = subprocess.run(["pdftoppm", "-png", "-r", str(dpi), str(pdf), str(destination / "page")],
                            capture_output=True, text=True, timeout=timeout)
    generated = sorted(destination.glob("page-*.png"), key=page_key)
    if result.returncode or len(generated) != pages:
        raise ValueError("Не удалось преобразовать PDF в слайды")
    if sum(path.stat().st_size for path in generated) > 512 * 1024 * 1024:
        raise ValueError("Слайды занимают слишком много места (лимит 512 МБ)")
    for index, slide in enumerate(generated, 1):
        slide.rename(destination / f"slide-{index}.png")
    return pages
