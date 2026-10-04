"""Password verifiers and scoped, signed browser sessions."""
import base64
import hashlib
import hmac
import json
import secrets
import time

ITERATIONS = 310_000


def hash_password(value: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", value.encode("utf-8"), bytes.fromhex(salt), ITERATIONS)
    return "$".join(("pbkdf2_sha256", str(ITERATIONS), salt, digest.hex()))


def verify_password(value: str, verifier: str) -> bool:
    try:
        algorithm, rounds, salt, expected = verifier.split("$")
        if algorithm != "pbkdf2_sha256" or not 100_000 <= int(rounds) <= 2_000_000:
            return False
        digest = hashlib.pbkdf2_hmac("sha256", value.encode("utf-8"), bytes.fromhex(salt), int(rounds))
        return hmac.compare_digest(digest.hex(), expected)
    except (ValueError, TypeError):
        return False


def sign_session(secret: str, scope: str, version: str, lifetime: int = 43200) -> tuple[str, str]:
    csrf = secrets.token_urlsafe(24)
    payload = json.dumps({"scope": scope, "version": version, "exp": int(time.time()) + lifetime,
                          "csrf": csrf}, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}", csrf


def read_session(secret: str, token: str | None, scope: str, version: str) -> dict | None:
    if not token or len(token) > 2048:
        return None
    try:
        encoded, signature = token.split(".")
        expected = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        if (payload.get("scope") != scope or payload.get("version") != version
                or not isinstance(payload.get("exp"), int) or payload["exp"] <= time.time()):
            return None
        return payload
    except (ValueError, TypeError, KeyError):
        return None
