"""Narrow authenticated connection to the host's Preza update service."""
import http.client
import json
import socket


class UpdateServiceError(Exception):
    def __init__(self, status, detail):
        self.status, self.detail = status, detail


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=20)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class UpdateClient:
    def __init__(self, path, token):
        self.path, self.token = path, token

    @property
    def enabled(self):
        return bool(self.path and self.token)

    def request(self, method, path, body=None):
        if not self.enabled:
            raise UpdateServiceError(503, "Обновления из интерфейса доступны после установки серверной службы обновления.")
        connection = UnixConnection(self.path)
        try:
            connection.request(method, path, body=json.dumps(body) if body is not None else None,
                               headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
            response = connection.getresponse()
            raw = response.read(65537)
            if len(raw) > 65536:
                raise ValueError("Oversized updater response")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("Invalid updater response")
            if response.status >= 400:
                raise UpdateServiceError(response.status, data.get("detail", "Служба обновления отклонила запрос."))
            return data
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise UpdateServiceError(503, "Служба обновления недоступна. Проверьте её запуск на сервере.") from exc
        finally:
            connection.close()
