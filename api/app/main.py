import asyncio
import hashlib
import hmac
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid
from collections import OrderedDict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .security import hash_password, read_session, sign_session, verify_password
from .storage import Storage, now, render_pdf, validate_slug

WEB_DIR = Path(os.getenv("WEB_DIR", str(Path(__file__).resolve().parents[2] / "web")))


class RequestTooLarge(HTTPException):
    def __init__(self):
        super().__init__(413, "Файл слишком большой")


class BodyLimit:
    def __init__(self, app, limit: int):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        try:
            length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            length = self.limit + 1
        if length > self.limit:
            return await JSONResponse({"detail": "Файл слишком большой"}, 413)(scope, receive, send)
        received, started = 0, False

        async def limited_receive():
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if received > self.limit:
                raise RequestTooLarge()
            return message

        async def tracked_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except RequestTooLarge:
            if not started:
                await JSONResponse({"detail": "Файл слишком большой"}, 413)(scope, receive, send)


class Credentials(BaseModel):
    key: str


class PasswordInput(BaseModel):
    password: str


class LiveInput(BaseModel):
    active: bool
    slide: int


def verify_verifier_format(value: str) -> bool:
    try:
        kind, rounds, salt, digest = value.split("$")
        return (kind == "pbkdf2_sha256" and 100000 <= int(rounds) <= 2000000
                and len(bytes.fromhex(salt)) == 16 and len(bytes.fromhex(digest)) == 32)
    except (ValueError, TypeError):
        return False


def create_app(data_dir: Path | None = None) -> FastAPI:
    storage = Storage(data_dir or Path(os.getenv("DATA_DIR", "/data")))
    secret = os.getenv("SESSION_SECRET", "")
    public_url = os.getenv("PUBLIC_URL", "https://preza.shmoza.net").rstrip("/")
    admin_hash = os.getenv("ADMIN_KEY_HASH", "")
    raw_key = os.getenv("ADMIN_ACCESS_KEY", "")
    if not admin_hash and raw_key:
        admin_hash = hash_password(raw_key)
    admin_version = hashlib.sha256(admin_hash.encode()).hexdigest()
    secure = urlsplit(public_url).scheme == "https"
    max_upload = int(os.getenv("MAX_UPLOAD_MB", "256")) * 1024 * 1024
    failures: OrderedDict[tuple, deque] = OrderedDict()

    @asynccontextmanager
    async def lifespan(app):
        if len(secret) < 32:
            raise RuntimeError("SESSION_SECRET must be generated (at least 32 characters)")
        if not admin_hash or not verify_verifier_format(admin_hash):
            raise RuntimeError("Set ADMIN_KEY_HASH or an explicit ADMIN_ACCESS_KEY")
        parsed = urlsplit(public_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.path or parsed.query or parsed.fragment:
            raise RuntimeError("PUBLIC_URL must be an origin without a path")
        if not 72 <= int(os.getenv("PDF_RENDER_DPI", "300")) <= 600:
            raise RuntimeError("PDF_RENDER_DPI must be between 72 and 600")
        storage.initialize()
        app.state.change_event = asyncio.Event()
        app.state.render_lock = asyncio.Lock()
        yield

    app = FastAPI(title="Preza", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.storage = storage
    app.add_middleware(BodyLimit, limit=max_upload + 1024 * 1024)

    @app.middleware("http")
    async def response_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
        if not request.url.path.startswith("/assets/"):
            response.headers["Cache-Control"] = "private, no-store"
        return response

    def origin(request: Request):
        if request.headers.get("origin") != public_url:
            raise HTTPException(403, "Запрос должен исходить с этого сайта")

    def admin_session(request: Request):
        return read_session(secret, request.cookies.get("preza_admin"), "admin", admin_version)

    def admin(request: Request):
        session = admin_session(request)
        if not session:
            raise HTTPException(401, "Войдите в админку")
        if request.method not in ("GET", "HEAD"):
            origin(request)
            if not hmac.compare_digest(request.headers.get("x-csrf-token", ""), session["csrf"]):
                raise HTTPException(403, "Сессия устарела. Обновите страницу")
        return session

    def record_or_404(*, id=None, slug=None):
        record = storage.get(id=id, slug=slug)
        if not record:
            raise HTTPException(404, "Презентация не найдена")
        return record

    def viewer_allowed(request: Request, record: dict) -> bool:
        return not record["password_hash"] or bool(admin_session(request)) or bool(
            read_session(secret, request.cookies.get("preza_view_" + record["id"]),
                         record["id"], record["access_version"]))

    def require_viewer(request, record):
        if not viewer_allowed(request, record):
            raise HTTPException(401, "Введите пароль презентации")

    def state(record, *, owner=False):
        result = {key: record[key] for key in ("id", "slug", "title", "default_mode", "slide_count",
                  "content_version", "current_slide", "revision", "created_at", "updated_at")}
        result.update(allow_switch=bool(record["allow_switch"]), live_active=bool(record["live_active"]),
                      password_required=bool(record["password_hash"]), url=f"/p/{record['slug']}",
                      slide_base=f"/api/p/{record['id']}/slides/{record['content_version']}/")
        if owner:
            result["preview_base"] = f"/api/admin/presentations/{record['id']}/slides/{record['content_version']}/"
        return result

    def changed():
        event = app.state.change_event
        app.state.change_event = asyncio.Event()
        event.set()

    def throttle(request, scope):
        key = (request.client.host if request.client else "unknown", scope)
        stamp = time.monotonic()
        if key not in failures:
            if len(failures) >= 1024:
                failures.popitem(last=False)
            failures[key] = deque()
        attempts = failures[key]
        failures.move_to_end(key)
        while attempts and attempts[0] < stamp - 60:
            attempts.popleft()
        if len(attempts) >= 10:
            raise HTTPException(429, "Слишком много попыток. Подождите минуту.", headers={"Retry-After": "60"})
        attempts.append(stamp)
        return key

    def cookie(response, name, scope, version):
        token, csrf = sign_session(secret, scope, version)
        response.set_cookie(name, token, httponly=True, secure=secure, samesite="lax",
                            path="/", max_age=43200)
        return csrf

    @app.get("/api/health")
    async def health():
        with storage.connect() as db:
            db.execute("SELECT 1").fetchone()
        return {"status": "ok", "version": os.getenv("PREZA_VERSION", "development")}

    @app.get("/api/admin/session")
    async def session_info(request: Request):
        session = admin_session(request)
        return {"authenticated": bool(session), "csrf": session["csrf"] if session else ""}

    @app.post("/api/admin/login")
    async def login(body: Credentials, request: Request):
        origin(request)
        key = throttle(request, "admin")
        if not await asyncio.to_thread(verify_password, body.key, admin_hash):
            raise HTTPException(401, "Неверный Access Key")
        failures.pop(key, None)
        response = JSONResponse({})
        csrf = cookie(response, "preza_admin", "admin", admin_version)
        response.body = json.dumps({"authenticated": True, "csrf": csrf}).encode()
        response.headers["content-length"] = str(len(response.body))
        return response

    @app.post("/api/admin/logout")
    async def logout(request: Request, _=Depends(admin)):
        response = JSONResponse({"ok": True})
        response.delete_cookie("preza_admin", path="/", secure=secure, httponly=True, samesite="lax")
        return response

    @app.get("/api/admin/presentations", dependencies=[Depends(admin)])
    async def list_presentations():
        return {"presentations": [state(record, owner=True) for record in storage.all()]}

    def validate_fields(title, slug):
        title = title.strip()
        if not title or len(title) > 200:
            raise HTTPException(400, "Название должно содержать от 1 до 200 символов")
        if not slug:
            slug = uuid.uuid4().hex[:12]
        try:
            validate_slug(slug)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return title, slug

    async def prepare_pdf(file: UploadFile):
        if not (file.filename or "").lower().endswith(".pdf"):
            raise HTTPException(400, "Поддерживаются только PDF")
        directory = Path(tempfile.mkdtemp(prefix="upload-", dir=storage.staging))
        try:
            pdf, total = directory / "source.pdf", 0
            with pdf.open("wb") as out:
                while chunk := await file.read(1024 * 1024):
                    total += len(chunk)
                    if total > max_upload:
                        raise HTTPException(413, "PDF превышает допустимый размер")
                    out.write(chunk)
            with pdf.open("rb") as stream:
                if b"%PDF-" not in stream.read(1024):
                    raise HTTPException(400, "Файл не является PDF")
            async with app.state.render_lock:
                task = asyncio.create_task(asyncio.to_thread(
                    render_pdf, pdf, directory, int(os.getenv("PDF_RENDER_DPI", "300")),
                    int(os.getenv("MAX_PDF_PAGES", "500")), int(os.getenv("RENDER_TIMEOUT", "300"))))
                try:
                    pages = await asyncio.shield(task)
                except asyncio.CancelledError:
                    await task
                    raise
            return directory, pages
        except (ValueError, subprocess.TimeoutExpired) as exc:
            shutil.rmtree(directory, ignore_errors=True)
            message = "Конвертация PDF заняла слишком много времени" if isinstance(exc, subprocess.TimeoutExpired) else str(exc)
            raise HTTPException(400, message) from exc
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        finally:
            await file.close()

    @app.post("/api/admin/presentations", status_code=201, dependencies=[Depends(admin)])
    async def create_presentation(file: UploadFile = File(...), title: str = Form(...),
            slug: str = Form(""), default_mode: Literal["scroll", "slides"] = Form("scroll"),
            allow_switch: bool = Form(True), password: str = Form("")):
        title, slug = validate_fields(title, slug)
        if storage.get(slug=slug):
            raise HTTPException(409, "Этот адрес уже занят")
        staged, pages = await prepare_pdf(file)
        record = dict(id=uuid.uuid4().hex, slug=slug, title=title, default_mode=default_mode,
                      allow_switch=int(allow_switch),
                      password_hash=await asyncio.to_thread(hash_password, password) if password else "",
                      access_version=uuid.uuid4().hex, content_version=uuid.uuid4().hex,
                      slide_count=pages, created_at=now(), updated_at=now())
        destination = storage.version_path(record)
        try:
            destination.parent.mkdir(parents=True)
            staged.rename(destination)
            storage.insert(record)
        except sqlite3.IntegrityError as exc:
            shutil.rmtree(destination.parent, ignore_errors=True)
            raise HTTPException(409, "Этот адрес уже занят") from exc
        finally:
            shutil.rmtree(staged, ignore_errors=True)
        changed()
        return state(record_or_404(id=record["id"]), owner=True)

    @app.get("/api/admin/presentations/{id}", dependencies=[Depends(admin)])
    async def get_presentation(id: str):
        return state(record_or_404(id=id), owner=True)

    @app.put("/api/admin/presentations/{id}", dependencies=[Depends(admin)])
    async def edit_presentation(id: str, title: str = Form(...), slug: str = Form(...),
            default_mode: Literal["scroll", "slides"] = Form("scroll"), allow_switch: bool = Form(True),
            password_action: Literal["keep", "set", "remove"] = Form("keep"), password: str = Form(""),
            expected_revision: int = Form(...), file: UploadFile | None = File(None)):
        previous = record_or_404(id=id)
        title, slug = validate_fields(title, slug)
        if previous["revision"] != expected_revision:
            raise HTTPException(409, "Презентация изменилась. Откройте ее заново")
        password_hash = previous["password_hash"]
        if password_action == "set":
            if password == "":
                raise HTTPException(400, "Введите пароль или выберите «Убрать пароль»")
            password_hash = await asyncio.to_thread(hash_password, password)
        elif password_action == "remove":
            password_hash = ""
        staged, pages = (await prepare_pdf(file)) if file and file.filename else (None, previous["slide_count"])
        version = uuid.uuid4().hex if staged else previous["content_version"]
        destination = storage.presentations / id / version
        try:
            if staged:
                staged.rename(destination)
            with storage.connect() as db:
                update = db.execute("""UPDATE presentations SET title=?,slug=?,default_mode=?,allow_switch=?,
                    password_hash=?,access_version=?,content_version=?,slide_count=?,live_active=?,
                    current_slide=?,revision=revision+1,updated_at=? WHERE id=? AND revision=?""",
                    (title, slug, default_mode, int(allow_switch), password_hash,
                     uuid.uuid4().hex if password_action != "keep" else previous["access_version"],
                     version, pages, 0 if staged else previous["live_active"],
                     0 if staged else previous["current_slide"], now(), id, expected_revision))
                if not update.rowcount:
                    raise HTTPException(409, "Презентация изменилась. Откройте ее заново")
        except BaseException as exc:
            if staged:
                shutil.rmtree(destination, ignore_errors=True)
            if isinstance(exc, sqlite3.IntegrityError):
                raise HTTPException(409, "Этот адрес уже занят") from exc
            raise
        finally:
            if staged:
                shutil.rmtree(staged, ignore_errors=True)
        if staged:
            shutil.rmtree(storage.version_path(previous), ignore_errors=True)
        changed()
        return state(record_or_404(id=id), owner=True)

    @app.delete("/api/admin/presentations/{id}", dependencies=[Depends(admin)])
    async def delete_presentation(id: str):
        record_or_404(id=id)
        with storage.connect() as db:
            db.execute("DELETE FROM presentations WHERE id=?", (id,))
        changed()
        shutil.rmtree(storage.presentations / id, ignore_errors=True)
        return {"ok": True}

    @app.post("/api/admin/presentations/{id}/live", dependencies=[Depends(admin)])
    async def control_live(id: str, body: LiveInput):
        record = record_or_404(id=id)
        if not 0 <= body.slide < record["slide_count"]:
            raise HTTPException(400, "Номер слайда вне диапазона")
        with storage.connect() as db:
            db.execute("""UPDATE presentations SET live_active=?,current_slide=?,
                       revision=revision+1,updated_at=? WHERE id=?""", (int(body.active), body.slide, now(), id))
        changed()
        return state(record_or_404(id=id), owner=True)

    @app.get("/api/public/presentations/{slug}")
    async def public_state(slug: str, request: Request):
        record = record_or_404(slug=slug)
        if not viewer_allowed(request, record):
            return {"locked": True, "title": record["title"], "slug": record["slug"]}
        return {"locked": False, **state(record)}

    @app.post("/api/public/presentations/{slug}/unlock")
    async def unlock(slug: str, body: PasswordInput, request: Request):
        origin(request)
        record = record_or_404(slug=slug)
        key = throttle(request, record["id"])
        if record["password_hash"] and not await asyncio.to_thread(verify_password, body.password, record["password_hash"]):
            raise HTTPException(401, "Неверный пароль")
        failures.pop(key, None)
        response = JSONResponse({"ok": True})
        cookie(response, "preza_view_" + record["id"], record["id"], record["access_version"])
        return response

    def slide_response(record, version, index):
        if version != record["content_version"] or not 0 <= index < record["slide_count"]:
            raise HTTPException(404, "Слайд не найден")
        path = storage.version_path(record) / f"slide-{index + 1}.png"
        if not path.is_file():
            raise HTTPException(404, "Слайд не найден")
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, no-store"})

    @app.get("/api/p/{id}/slides/{version}/{index}")
    async def public_slide(id: str, version: str, index: int, request: Request):
        record = record_or_404(id=id)
        require_viewer(request, record)
        if record["live_active"] and index != record["current_slide"]:
            raise HTTPException(409, "Этот слайд сейчас не показывается")
        return slide_response(record, version, index)

    @app.get("/api/admin/presentations/{id}/slides/{version}/{index}", dependencies=[Depends(admin)])
    async def preview_slide(id: str, version: str, index: int):
        return slide_response(record_or_404(id=id), version, index)

    @app.get("/api/p/{id}/events")
    async def events(id: str, request: Request):
        require_viewer(request, record_or_404(id=id))

        async def stream():
            revision = -1
            while not await request.is_disconnected():
                event = app.state.change_event
                current = storage.get(id=id)
                if not current:
                    yield 'event: deleted\ndata: {}\n\n'
                    return
                if not viewer_allowed(request, current):
                    yield 'event: locked\ndata: {}\n\n'
                    return
                if current["revision"] != revision:
                    revision = current["revision"]
                    yield "event: state\ndata: " + json.dumps(state(current), ensure_ascii=False) + "\n\n"
                try:
                    await asyncio.wait_for(event.wait(), timeout=10)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"X-Accel-Buffering": "no", "Cache-Control": "private, no-store"})

    @app.get("/")
    async def root():
        return RedirectResponse("/admin", status_code=302)

    @app.get("/view")
    @app.get("/view/")
    async def legacy_view():
        record = next((item for item in storage.all() if item["legacy"]), None)
        if not record:
            raise HTTPException(404, "Презентация не найдена")
        return RedirectResponse(f"/p/{record['slug']}", status_code=302)

    @app.get("/admin")
    @app.get("/admin/")
    async def admin_page():
        return FileResponse(WEB_DIR / "admin.html")

    @app.get("/p/{slug}")
    async def viewer_page(slug: str):
        record_or_404(slug=slug)
        return FileResponse(WEB_DIR / "view.html")

    app.mount("/assets", StaticFiles(directory=WEB_DIR), name="assets")
    return app


app = create_app()
