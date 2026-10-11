#!/usr/bin/env python3
"""Host-only update control. Exposes three operations over a private Unix socket."""
import hmac
import json
import os
import signal
import socketserver
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import install as installer
from release_verify import REPOSITORY, validate_version


class ControlError(Exception):
    def __init__(self, status, detail):
        self.status, self.detail = status, detail


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def latest_release():
    url = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                                 "User-Agent": "preza-updater/1"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.url != url:
                raise ValueError("Unexpected release API redirect")
            raw = response.read(512 * 1024 + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    if len(raw) > 512 * 1024:
        raise ValueError("Release API response exceeds limits")
    release = json.loads(raw)
    if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
        raise ValueError("Expected a published stable release")
    tag = release.get("tag_name", "")
    if not isinstance(tag, str) or not tag.startswith("preza-v"):
        raise ValueError("Unexpected release tag")
    version = validate_version(tag.removeprefix("preza-v"))
    assets = {asset.get("name") for asset in release.get("assets", [])}
    if not {"release.json", "release.json.sig", f"preza-{version}-install.tar.gz"}.issubset(assets):
        raise ValueError("Release installation assets are incomplete")
    return version


class Controller:
    def __init__(self, root=installer.ROOT):
        self.root = Path(root)
        self.lock = threading.RLock()
        self.latest = None
        self.checked_at = None
        self.checked_clock = 0
        self.job_path = self.root / "update-job.json"
        self.job = json.loads(self.job_path.read_text()) if self.job_path.exists() else None
        if not self.token:
            raise RuntimeError("Update-control token is missing")

    @property
    def token(self):
        # Read the protected current credential so explicit repair/rotation needs no daemon restart.
        return installer.load_env(self.root / ".env").get("UPDATE_CONTROL_TOKEN", "")

    def current_version(self):
        path = self.root / "installed.json"
        return json.loads(path.read_text())["version"] if path.exists() else installer.load_env(self.root / ".env")["VERSION"]

    def busy(self):
        return bool((self.job and self.job["state"] in ("queued", "running"))
                    or (self.root / "update-control/busy").exists()
                    or (self.root / "update-pending.json").exists())

    def save_job(self):
        installer.atomic_write(self.job_path, json.dumps(self.job, ensure_ascii=False))

    def status(self):
        with self.lock:
            current = self.current_version()
            updating = self.busy()
            available = bool(self.latest and tuple(map(int, self.latest.split("."))) > tuple(map(int, current.split("."))))
            job = {key: value for key, value in self.job.items() if key != "pid"} if self.job else None
            return dict(enabled=True, current_version=current, latest_version=self.latest,
                        checked_at=self.checked_at, update_available=available and not updating
                        and not (self.job and self.job["state"] == "recovery_required"),
                        updating=updating, job=job)

    def check(self):
        with self.lock:
            if self.busy():
                raise ControlError(409, "Обновление уже выполняется.")
            if self.job and self.job["state"] == "recovery_required":
                if not installer.healthy(installer.load_env(self.root / ".env"), wait=0):
                    raise ControlError(409, "Сначала восстановите приложение на сервере.")
                self.job.update(state="failed", message="Приложение восстановлено. Можно проверить обновления.")
                self.save_job()
        try:
            latest = latest_release()
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise ControlError(502, "Не удалось проверить релизы. Повторите попытку позже.") from exc
        with self.lock:
            if self.busy():
                raise ControlError(409, "Обновление уже выполняется.")
            self.latest, self.checked_at, self.checked_clock = latest, timestamp(), time.monotonic()
            return self.status()

    def request_update(self, body):
        try:
            if not isinstance(body, dict) or set(body) != {"version", "request_id"}:
                raise ValueError("Unexpected update fields")
            version = validate_version(body["version"])
            request_id = str(uuid.UUID(body["request_id"]))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ControlError(400, "Некорректный запрос обновления.") from exc
        with self.lock:
            if self.job and self.job["id"] == request_id:
                if self.job["target_version"] != version:
                    raise ControlError(409, "Этот запрос уже использован для другой версии.")
                return self.status()
            if self.busy() or (self.job and self.job["state"] == "recovery_required"):
                raise ControlError(409, "Предыдущее обновление ещё не завершено.")
            current = self.current_version()
            if (version != self.latest or not self.checked_at or time.monotonic() - self.checked_clock > 900
                    or tuple(map(int, version.split("."))) <= tuple(map(int, current.split(".")))):
                raise ControlError(409, "Сначала проверьте наличие новой версии.")
            self.job = dict(id=request_id, target_version=version, previous_version=current, state="queued",
                            message="Обновление принято. Презентации и настройки будут сохранены.",
                            started_at=timestamp(), finished_at=None)
            self.save_job()
            threading.Thread(target=self.worker, daemon=True).start()
            return self.status()

    def run_installer(self, *arguments):
        log = self.root / "update.log"
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as output:
            environment = dict(os.environ, PREZA_UPDATE_REQUEST_ID=self.job["id"])
            process = subprocess.Popen([sys.executable, str(self.root / "current/scripts/install.py"), *arguments],
                                       stdout=output, stderr=subprocess.STDOUT, start_new_session=True, env=environment)
            with self.lock:
                self.job["pid"] = process.pid
                self.save_job()
            try:
                return process.wait(timeout=1800)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                return 1

    def owns_pending_update(self):
        path = self.root / "update-pending.json"
        if not path.exists():
            return False
        journal = json.loads(path.read_text())
        return (journal.get("request_id") == self.job["id"]
                and journal.get("target_version") == self.job["target_version"]
                and journal.get("previous_version") == self.job["previous_version"])

    def worker(self, recovery=False):
        with self.lock:
            self.job.update(state="running", message="Проверяем релиз, сохраняем данные и устанавливаем обновление.")
            self.save_job()
        try:
            code = self.run_installer("rollback") if recovery else self.run_installer("update", self.job["target_version"])
            if code and self.owns_pending_update() and not recovery:
                self.run_installer("rollback")
            values = installer.load_env(self.root / ".env")
            healthy = installer.healthy(values, wait=0)
            pending = (self.root / "update-pending.json").exists()
            if not pending and healthy:
                installer.update_guard(False)
            if pending or not healthy:
                command = "rollback" if pending else "install"
                state, message = "recovery_required", "Восстановление не завершено. На сервере выполните sudo preza-install " + command + "."
            elif not recovery and code == 0 and self.current_version() == self.job["target_version"]:
                state, message = "completed", "Обновление завершено. Презентации, ссылки и настройки сохранены."
            else:
                state, message = "failed", "Обновление не выполнено. Прежняя версия и презентации доступны."
        except Exception:
            state, message = "recovery_required", "Обновление прервано. Проверьте службу и выполните sudo preza-install rollback на сервере."
        with self.lock:
            self.job.update(state=state, message=message, finished_at=timestamp())
            self.save_job()

    def recover(self):
        if not self.job or self.job["state"] not in ("queued", "running"):
            return
        # systemd stops the worker with this service; its persisted journal survives.
        if self.owns_pending_update():
            threading.Thread(target=self.worker, kwargs={"recovery": True}, daemon=True).start()
        else:
            values = installer.load_env(self.root / ".env")
            if installer.healthy(values, wait=0):
                installer.update_guard(False)
                completed = self.current_version() == self.job["target_version"]
                self.job.update(state="completed" if completed else "failed", finished_at=timestamp(),
                                message="Обновление завершено." if completed else "Обновление прервано. Прежняя версия доступна.")
            else:
                self.job.update(state="recovery_required", finished_at=timestamp(),
                                message="Обновление прервано. На сервере выполните sudo preza-install install.")
            self.save_job()


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, *arguments):
        pass

    def respond(self, status, data):
        raw = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def handle_request(self):
        controller = self.server.controller
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 8192:
                return self.respond(413, {"detail": "Некорректный размер запроса."})
            raw = self.rfile.read(length) if length else b""
        except (ValueError, OSError):
            return self.respond(400, {"detail": "Некорректный запрос."})
        # Consume bounded request bodies before rejecting a route or credential:
        # closing a Unix socket with unread data can discard the error response.
        token = controller.token
        if not token or not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
            return self.respond(403, {"detail": "Доступ к службе обновления запрещён."})
        try:
            if self.command == "GET" and self.path == "/status":
                return self.respond(200, controller.status())
            if self.command != "POST" or self.path not in ("/check", "/update"):
                return self.respond(404, {"detail": "Неизвестная операция."})
            if not raw:
                raise ControlError(400, "Некорректный размер запроса.")
            body = json.loads(raw)
            data = controller.check() if self.path == "/check" else controller.request_update(body)
            self.respond(202 if self.path == "/update" else 200, data)
        except ControlError as exc:
            self.respond(exc.status, {"detail": exc.detail})
        except (ValueError, TypeError):
            self.respond(400, {"detail": "Некорректный запрос."})
        except Exception:
            self.respond(503, {"detail": "Служба обновления временно недоступна."})

    do_GET = do_POST = handle_request


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def main():
    if os.geteuid() != 0:
        raise RuntimeError("The host update service must run as root")
    controller = Controller()
    socket = installer.ROOT / "update-control/control.sock"
    socket.unlink(missing_ok=True)
    with Server(str(socket), Handler) as server:
        server.controller = controller
        os.chown(socket, 0, 10001)
        socket.chmod(0o660)
        controller.recover()
        server.serve_forever()


if __name__ == "__main__":
    main()
