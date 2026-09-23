"""Master + client token authentication helpers."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from http.cookies import SimpleCookie
from typing import TYPE_CHECKING

from .store import normalize_upstream_type

if TYPE_CHECKING:
    from .store import TokenStore, UpstreamStore

# In-memory session tokens (master login). Survives only for process lifetime.
_sessions: dict[str, float] = {}  # session_id -> expires_at
SESSION_TTL = 7 * 24 * 3600  # 7 days
SESSION_COOKIE = "stp_session"


def _new_session_id() -> str:
    return secrets.token_urlsafe(32)


def create_session() -> str:
    sid = _new_session_id()
    _sessions[sid] = time.time() + SESSION_TTL
    _purge_sessions()
    return sid


def destroy_session(sid: str | None) -> None:
    if sid:
        _sessions.pop(sid, None)


def _purge_sessions() -> None:
    now = time.time()
    dead = [k for k, exp in _sessions.items() if exp < now]
    for k in dead:
        _sessions.pop(k, None)


def session_valid(sid: str | None) -> bool:
    if not sid:
        return False
    exp = _sessions.get(sid)
    if exp is None:
        return False
    if exp < time.time():
        _sessions.pop(sid, None)
        return False
    return True


def parse_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.strip().split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


def parse_cookie_session(cookie_header: str | None) -> str | None:
    if not cookie_header:
        return None
    c = SimpleCookie()
    try:
        c.load(cookie_header)
    except Exception:
        return None
    morsel = c.get(SESSION_COOKIE)
    return morsel.value if morsel else None


def constant_eq(a: str, b: str) -> bool:
    if a is None or b is None:
        return False
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


class AuthContext:
    """Result of authenticating a request."""

    def __init__(
        self,
        *,
        is_master: bool = False,
        is_api: bool = False,
        token_id: str | None = None,
        session_id: str | None = None,
        upstream_id: str = "",
        upstream_key: str | None = None,
        upstream_type: str = "stepfun",
        upstream_base: str = "",
    ) -> None:
        self.is_master = is_master
        self.is_api = is_api  # valid client or master token for API routes
        self.token_id = token_id
        self.session_id = session_id
        # Upstream key this request must authenticate with (client tokens only).
        self.upstream_id = upstream_id
        self.upstream_key = upstream_key
        # Node kind of the bound upstream ("stepfun" / "anthropic") plus its
        # optional base-url override ("" = use the global default).
        self.upstream_type = upstream_type
        self.upstream_base = upstream_base

    @property
    def ok_admin(self) -> bool:
        return self.is_master

    @property
    def ok_api(self) -> bool:
        return self.is_api or self.is_master


def authenticate(
    *,
    authorization: str | None,
    cookie_header: str | None,
    master_token: str,
    store: "TokenStore",
    upstreams: "UpstreamStore | None" = None,
    raw_api_key: str | None = None,
) -> AuthContext:
    """Authenticate for either admin (master/session) or API (client token)."""
    bearer = parse_bearer(authorization)
    if not bearer and raw_api_key:
        bearer = raw_api_key.strip()
    sid = parse_cookie_session(cookie_header)

    # Master via Bearer
    if bearer and constant_eq(bearer, master_token):
        return AuthContext(is_master=True, is_api=True)

    # Session cookie (master WebUI)
    if session_valid(sid):
        return AuthContext(is_master=True, session_id=sid)

    # Client API token
    if bearer:
        meta = store.validate(bearer)
        if meta:
            upstream_id = meta.get("upstream_id") or ""
            key = upstreams.active_key(upstream_id) if upstreams else None
            upstream_type = "stepfun"
            upstream_base = ""
            if upstreams:
                record = upstreams.active_record(upstream_id)
                if record:
                    key = record.get("key") or key
                    upstream_type = normalize_upstream_type(record.get("type"))
                    upstream_base = record.get("base_url") or ""
            return AuthContext(
                is_api=True,
                token_id=meta["id"],
                upstream_id=upstream_id,
                upstream_key=key,
                upstream_type=upstream_type,
                upstream_base=upstream_base,
            )

    return AuthContext()


def login_with_master(provided: str, master_token: str) -> str | None:
    """Validate master token and return new session id, or None."""
    if provided and constant_eq(provided.strip(), master_token):
        return create_session()
    return None
