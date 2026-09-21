"""Upstream-key and client-token stores — JSON files under data/.

Both stores keep plaintext values: an upstream key must be replayable to
authenticate to StepFun, and client tokens must be re-readable so the WebUI can
copy them after creation. Both files are written with owner-only permissions.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
import uuid
from pathlib import Path
from typing import Any

# Active client tokens allowed per upstream key.
MAX_TOKENS_PER_UPSTREAM = 3


class LimitError(Exception):
    """Raised when a create would exceed MAX_TOKENS_PER_UPSTREAM."""


def generate_token() -> str:
    return "stp_" + secrets.token_urlsafe(32)


def mask_secret(value: str) -> str:
    if len(value) > 12:
        return f"{value[:6]}…{value[-4:]}"
    if value:
        return f"{value[:2]}…{value[-2:]}"
    return ""


def _chmod_private(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass


class JsonFileStore:
    """Atomic JSON document guarded by a re-entrant lock."""

    def __init__(self, path: Path, root_key: str) -> None:
        self.path = path
        self.root_key = root_key
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {root_key: []}
        self._load()

    def _load(self) -> None:
        with self._lock:
            if not self.path.is_file():
                self._save()
                return
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                loaded = None
            items = loaded.get(self.root_key) if isinstance(loaded, dict) else None
            self._data = {self.root_key: items if isinstance(items, list) else []}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp.replace(self.path)
        _chmod_private(self.path)


class UpstreamStore(JsonFileStore):
    """StepFun API keys the proxy can authenticate with."""

    def __init__(self, data_dir: Path, seed_key: str = "", seed_name: str = "默认") -> None:
        super().__init__(data_dir / "upstreams.json", "upstreams")
        # Only seed when we actually have a key. An empty "默认" row would
        # otherwise eat a token slot and 503 every request bound to it.
        seed_key = (seed_key or "").strip()
        with self._lock:
            if seed_key and not self._data["upstreams"]:
                self._data["upstreams"].append(self._record(seed_name, seed_key))
                self._save()

    @staticmethod
    def _record(name: str, key: str) -> dict[str, Any]:
        return {
            "id": uuid.uuid4().hex[:16],
            "name": (name or "").strip() or "未命名",
            "key": (key or "").strip(),
            "created_at": int(time.time()),
            "revoked": False,
        }

    def list(self) -> list[dict[str, Any]]:
        """Public view — never includes the raw key."""
        with self._lock:
            return [
                {
                    "id": u["id"],
                    "name": u.get("name", ""),
                    "key_masked": mask_secret(u.get("key", "")),
                    "key_set": bool(u.get("key")),
                    "created_at": u.get("created_at"),
                    "revoked": bool(u.get("revoked")),
                }
                for u in self._data["upstreams"]
            ]

    def default_id(self) -> str:
        with self._lock:
            for u in self._data["upstreams"]:
                if not u.get("revoked"):
                    return u["id"]
            return ""

    def first_active_key(self) -> str | None:
        with self._lock:
            for u in self._data["upstreams"]:
                if not u.get("revoked") and u.get("key"):
                    return u["key"]
            return None

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for u in self._data["upstreams"] if not u.get("revoked"))

    def get(self, upstream_id: str) -> dict[str, Any] | None:
        """Raw record including the key. Returns None when missing or revoked."""
        with self._lock:
            for u in self._data["upstreams"]:
                if u["id"] == upstream_id and not u.get("revoked"):
                    return dict(u)
        return None

    def add(self, name: str, key: str) -> dict[str, Any]:
        key = (key or "").strip()
        if not key:
            raise ValueError("上游密钥不能为空")
        with self._lock:
            record = self._record(name, key)
            self._data["upstreams"].append(record)
            self._save()
            return dict(record)

    def revoke(self, upstream_id: str) -> bool:
        with self._lock:
            for u in self._data["upstreams"]:
                if u["id"] == upstream_id and not u.get("revoked"):
                    u["revoked"] = True
                    u["revoked_at"] = int(time.time())
                    self._save()
                    return True
        return False

    def active_key(self, upstream_id: str) -> str | None:
        """Key for a usable upstream, or None if missing/revoked/empty."""
        record = self.get(upstream_id)
        if not record or not record.get("key"):
            return None
        return record["key"]


class TokenStore(JsonFileStore):
    """Client tokens, each bound to one upstream key."""

    def __init__(self, data_dir: Path, default_upstream_id: str = "") -> None:
        super().__init__(data_dir / "tokens.json", "tokens")
        self._default_upstream_id = default_upstream_id
        self._adopt_orphans()

    def adopt_orphans(self, upstream_id: str) -> int:
        """Attach tokens that have no upstream_id to ``upstream_id``.

        Used once at startup (legacy tokens.json) and again when the first
        upstream key is added via the WebUI.
        """
        if not upstream_id:
            return 0
        with self._lock:
            n = 0
            for t in self._data["tokens"]:
                if not t.get("upstream_id"):
                    t["upstream_id"] = upstream_id
                    n += 1
            if n:
                self._save()
            return n

    def _adopt_orphans(self) -> None:
        self.adopt_orphans(self._default_upstream_id)

    def count_for_upstream(self, upstream_id: str) -> int:
        with self._lock:
            return sum(
                1
                for t in self._data["tokens"]
                if t.get("upstream_id") == upstream_id and not t.get("revoked")
            )

    def issue(self, name: str = "", upstream_id: str = "") -> dict[str, Any]:
        """Create a client token. Returns the record including plaintext."""
        uid = upstream_id or self._default_upstream_id
        if not uid:
            raise ValueError("没有可用的上游密钥，请先添加上游密钥")
        with self._lock:
            if self.count_for_upstream(uid) >= MAX_TOKENS_PER_UPSTREAM:
                raise LimitError(
                    f"该上游密钥已有 {MAX_TOKENS_PER_UPSTREAM} 个有效下游密钥，"
                    "请先撤销或换一个上游密钥"
                )
            plaintext = generate_token()
            record = {
                "id": uuid.uuid4().hex[:16],
                "name": (name or "").strip() or "unnamed",
                "token": plaintext,
                "upstream_id": uid,
                "created_at": int(time.time()),
                "revoked": False,
            }
            self._data["tokens"].append(record)
            self._save()
            return {
                "id": record["id"],
                "name": record["name"],
                "token": plaintext,
                "upstream_id": uid,
                "created_at": record["created_at"],
            }

    def list_tokens(self, include_revoked: bool = True) -> list[dict[str, Any]]:
        """Admin view — includes plaintext so the WebUI can copy tokens."""
        with self._lock:
            out = []
            for t in self._data["tokens"]:
                if not include_revoked and t.get("revoked"):
                    continue
                plaintext = t.get("token") or ""
                out.append(
                    {
                        "id": t["id"],
                        "name": t.get("name", ""),
                        "masked": mask_secret(plaintext),
                        "token": plaintext,
                        # True for hashed-only records from before plaintext
                        # storage: they still authenticate but cannot be copied.
                        "copyable": bool(plaintext),
                        "upstream_id": t.get("upstream_id", ""),
                        "created_at": t.get("created_at"),
                        "revoked": bool(t.get("revoked")),
                    }
                )
            return out

    def revoke(self, token_id: str) -> bool:
        with self._lock:
            for t in self._data["tokens"]:
                if t["id"] == token_id and not t.get("revoked"):
                    t["revoked"] = True
                    t["revoked_at"] = int(time.time())
                    self._save()
                    return True
            return False

    def revoke_for_upstream(self, upstream_id: str) -> int:
        """Cascade revoke. Returns how many tokens were revoked."""
        with self._lock:
            count = 0
            now = int(time.time())
            for t in self._data["tokens"]:
                if t.get("upstream_id") == upstream_id and not t.get("revoked"):
                    t["revoked"] = True
                    t["revoked_at"] = now
                    count += 1
            if count:
                self._save()
            return count

    def validate(self, plaintext: str) -> dict[str, Any] | None:
        """Return token metadata if valid (not revoked), else None.

        Matches the stored plaintext, and falls back to the sha256 hash so
        tokens issued before plaintext storage keep working.
        """
        if not plaintext:
            return None
        digest = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
        with self._lock:
            for t in self._data["tokens"]:
                if t.get("revoked"):
                    continue
                if t.get("token") == plaintext or (
                    t.get("token_hash") and t["token_hash"] == digest
                ):
                    return {
                        "id": t["id"],
                        "name": t.get("name", ""),
                        "created_at": t.get("created_at"),
                        "upstream_id": t.get("upstream_id", ""),
                    }
        return None

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for t in self._data["tokens"] if not t.get("revoked"))
