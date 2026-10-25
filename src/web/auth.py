"""Dashboard access control.

Rules:
  * DASHBOARD_PASSWORD unset → only direct loopback requests are allowed
    (a request that came through a proxy — e.g. `tailscale serve` — is not
    loopback, even though the TCP peer is 127.0.0.1).
  * DASHBOARD_PASSWORD set → a signed session cookie is required, except for
    direct loopback requests when DASHBOARD_TRUST_LOCALHOST is true (default).
  * State-changing API calls must carry the X-Requested-With header the
    dashboard sends (blocks cross-site form posts) and, when present, an Origin
    matching the Host.

Sessions are HMAC-signed expiry timestamps keyed by a secret stored in
data/.session_secret, so a phone stays signed in across restarts.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import HTTPConnection, Request
from starlette.responses import JSONResponse, RedirectResponse, Response

COOKIE = "kalshi_session"
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "kalshi-dashboard"
SESSION_DAYS = 30
_PROXY_HEADERS = ("x-forwarded-for", "forwarded", "x-real-ip", "tailscale-user-login",
                  "cf-connecting-ip")
_PUBLIC = ("/login", "/api/login", "/api/health", "/static/css/", "/static/login.js",
           "/static/icon.svg", "/static/manifest.webmanifest")

_SECRET_PATH = Path(os.getenv("KALSHI_DATA_DIR") or Path(__file__).parents[2] / "data") / ".session_secret"
_attempts: dict[str, deque] = defaultdict(deque)


def _password() -> str | None:
    return os.getenv("DASHBOARD_PASSWORD") or None


def _trust_localhost() -> bool:
    return os.getenv("DASHBOARD_TRUST_LOCALHOST", "true").lower() not in ("0", "false", "no")


def _secret() -> bytes:
    env = os.getenv("DASHBOARD_SECRET")
    if env:
        return env.encode()
    if not _SECRET_PATH.exists():
        _SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
        _SECRET_PATH.write_text(secrets.token_hex(32), encoding="utf-8")
        try:
            _SECRET_PATH.chmod(0o600)
        except OSError:
            pass
    return _SECRET_PATH.read_text(encoding="utf-8").strip().encode()


def make_session() -> str:
    expiry = str(int(time.time()) + SESSION_DAYS * 86400)
    sig = hmac.new(_secret(), expiry.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{expiry}.{sig}".encode()).decode()


def valid_session(token: str | None) -> bool:
    if not token:
        return False
    try:
        expiry, sig = base64.urlsafe_b64decode(token.encode()).decode().split(".", 1)
    except Exception:
        return False
    good = hmac.new(_secret(), expiry.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good) and expiry.isdigit() and int(expiry) > time.time()


def check_password(candidate: str) -> bool:
    pw = _password()
    if not pw:
        return False
    return hmac.compare_digest(hashlib.sha256(candidate.encode()).digest(),
                               hashlib.sha256(pw.encode()).digest())


def rate_limited(ip: str, limit: int = 5, window: float = 300.0) -> bool:
    now = time.monotonic()
    q = _attempts[ip]
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        return True
    q.append(now)
    return False


def is_direct_loopback(conn: HTTPConnection) -> bool:
    host = conn.client.host if conn.client else ""
    if host not in ("127.0.0.1", "::1", "localhost", "testclient"):
        return False
    return not any(h in conn.headers for h in _PROXY_HEADERS)


def authorized(conn: HTTPConnection) -> bool:
    if is_direct_loopback(conn) and (_password() is None or _trust_localhost()):
        return True
    if _password() is None:
        return False
    return valid_session(conn.cookies.get(COOKIE))


def origin_ok(conn: HTTPConnection) -> bool:
    origin = conn.headers.get("origin")
    if not origin:
        return True
    host = conn.headers.get("host", "")
    return origin.split("://", 1)[-1] == host


SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if not any(path == p or path.startswith(p) for p in _PUBLIC):
            if not authorized(request):
                if path.startswith("/api/"):
                    reason = "login_required" if _password() else "remote_access_disabled"
                    return JSONResponse({"error": reason}, status_code=401)
                return RedirectResponse("/login")
            if request.method not in ("GET", "HEAD", "OPTIONS") and path.startswith("/api/"):
                if request.headers.get(CSRF_HEADER) != CSRF_VALUE or not origin_ok(request):
                    return JSONResponse({"error": "csrf_check_failed"}, status_code=403)
        response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        if path.startswith("/static/"):
            # Revalidate (ETag) on every load so dashboard updates show up without a hard refresh
            response.headers["Cache-Control"] = "no-cache"
        return response


def cookie_secure(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
