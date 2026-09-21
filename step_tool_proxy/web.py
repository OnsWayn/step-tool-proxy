"""WebUI pages + admin JSON API handlers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .auth import (
    SESSION_COOKIE,
    SESSION_TTL,
    AuthContext,
    destroy_session,
    login_with_master,
    parse_cookie_session,
)
from .store import LimitError, UpstreamStore, mask_secret

if TYPE_CHECKING:
    from .config import Config
    from .store import TokenStore

# Package-relative static dir: ../static from this file
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def json_bytes(obj: Any, status: int = 200) -> tuple[int, bytes, str]:
    return status, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8"


def read_index() -> bytes:
    path = STATIC_DIR / "index.html"
    return path.read_bytes()


def _upstream_names(upstreams: UpstreamStore) -> dict[str, str]:
    return {u["id"]: u["name"] for u in upstreams.list()}


def handle_admin(
    method: str,
    path: str,
    body: bytes,
    auth: AuthContext,
    cfg: "Config",
    store: "TokenStore",
    cookie_header: str | None,
    upstreams: UpstreamStore,
) -> tuple[int, bytes, str, list[tuple[str, str]]]:
    """
    Handle /api/* admin routes.
    Returns (status, body, content_type, extra_headers).
    """
    extra: list[tuple[str, str]] = []
    parts = path.rstrip("/").split("/")
    # parts[0] == '', parts[1] == 'api', ...

    # POST /api/login — public
    if method == "POST" and path.rstrip("/") == "/api/login":
        try:
            data = json.loads(body or b"{}")
        except Exception:
            return (*json_bytes({"error": "invalid JSON"}, 400), extra)
        provided = (data.get("master_token") or data.get("token") or "").strip()
        sid = login_with_master(provided, cfg.master_token)
        if not sid:
            return (*json_bytes({"error": "管理密钥无效"}, 401), extra)
        extra.append(
            (
                "Set-Cookie",
                f"{SESSION_COOKIE}={sid}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_TTL}",
            )
        )
        return (*json_bytes({"ok": True}), extra)

    # POST /api/logout
    if method == "POST" and path.rstrip("/") == "/api/logout":
        sid = parse_cookie_session(cookie_header)
        destroy_session(sid)
        extra.append(
            (
                "Set-Cookie",
                f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0",
            )
        )
        return (*json_bytes({"ok": True}), extra)

    # All other /api/* require master
    if not auth.ok_admin:
        return (*json_bytes({"error": "未授权，请先登录"}, 401), extra)

    if method == "GET" and path.rstrip("/") == "/api/status":
        return (
            *json_bytes(
                cfg.status_dict(
                    token_count=store.active_count(),
                    upstream_count=upstreams.active_count(),
                )
            ),
            extra,
        )

    if method == "GET" and path.rstrip("/") == "/api/settings":
        return (*json_bytes(cfg.settings_dict()), extra)

    if method == "POST" and path.rstrip("/") == "/api/settings":
        try:
            data = json.loads(body or b"{}")
        except Exception:
            return (*json_bytes({"error": "invalid JSON"}, 400), extra)
        try:
            cfg.apply_settings(data)
        except ValueError as e:
            return (*json_bytes({"error": str(e)}, 400), extra)
        return (*json_bytes(cfg.settings_dict()), extra)

    # ---- upstream keys ----

    if method == "GET" and path.rstrip("/") == "/api/upstreams":
        items = upstreams.list()
        for u in items:
            u["token_count"] = store.count_for_upstream(u["id"])
        return (*json_bytes({"upstreams": items}), extra)

    if method == "POST" and path.rstrip("/") == "/api/upstreams":
        try:
            data = json.loads(body or b"{}")
        except Exception:
            data = {}
        name = (data.get("name") or "").strip()
        key = (data.get("key") or "").strip()
        try:
            record = upstreams.add(name, key)
        except ValueError as e:
            return (*json_bytes({"error": str(e)}, 400), extra)
        store.adopt_orphans(record["id"])
        return (
            *json_bytes(
                {
                    "id": record["id"],
                    "name": record["name"],
                    "key_masked": mask_secret(record["key"]),
                    "created_at": record["created_at"],
                }
            ),
            extra,
        )

    # POST /api/upstreams/{id}/revoke — cascades to its client tokens
    if method == "POST" and len(parts) >= 5 and parts[2] == "upstreams" and parts[4] == "revoke":
        uid = parts[3]
        if not upstreams.revoke(uid):
            return (*json_bytes({"error": "上游密钥不存在或已撤销"}, 404), extra)
        revoked_tokens = store.revoke_for_upstream(uid)
        return (
            *json_bytes({"ok": True, "id": uid, "revoked_tokens": revoked_tokens}),
            extra,
        )

    # ---- client tokens ----

    if method == "GET" and path.rstrip("/") == "/api/tokens":
        names = _upstream_names(upstreams)
        tokens = store.list_tokens()
        for t in tokens:
            t["upstream_name"] = names.get(t.get("upstream_id", ""), "—")
        return (*json_bytes({"tokens": tokens}), extra)

    if method == "POST" and path.rstrip("/") == "/api/tokens":
        try:
            data = json.loads(body or b"{}")
        except Exception:
            data = {}
        name = (data.get("name") or "").strip()
        upstream_id = (data.get("upstream_id") or "").strip() or upstreams.default_id()
        if not upstreams.get(upstream_id):
            return (*json_bytes({"error": "上游密钥不存在或已撤销"}, 400), extra)
        try:
            issued = store.issue(name, upstream_id)
        except (ValueError, LimitError) as e:
            return (*json_bytes({"error": str(e)}, 400), extra)
        names = _upstream_names(upstreams)
        issued["upstream_name"] = names.get(upstream_id, "—")
        return (*json_bytes(issued, 201), extra)

    # POST /api/tokens/{id}/revoke
    if method == "POST" and len(parts) >= 5 and parts[2] == "tokens" and parts[4] == "revoke":
        tid = parts[3]
        if not store.revoke(tid):
            return (*json_bytes({"error": "密钥不存在或已撤销"}, 404), extra)
        return (*json_bytes({"ok": True, "id": tid}), extra)

    # DELETE /api/tokens/{id}
    if method == "DELETE" and len(parts) == 4 and parts[2] == "tokens":
        tid = parts[3]
        if not store.revoke(tid):
            return (*json_bytes({"error": "密钥不存在或已撤销"}, 404), extra)
        return (*json_bytes({"ok": True, "id": tid}), extra)

    return (*json_bytes({"error": "not found"}, 404), extra)
