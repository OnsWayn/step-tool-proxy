"""Runtime settings overrides — JSON file under data/.

Values stored here take precedence over the environment-derived Config and
survive restarts, so the WebUI can edit the upstream URL and flags. Upstream
API keys live in ``upstreams.json`` (see ``store.UpstreamStore``) because there
can be several of them.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

# Keys the WebUI is allowed to change at runtime.
EDITABLE_KEYS = ("step_upstream", "anthropic_upstream", "forward_reasoning_history")

# Retired setting, migrated into upstreams.json on first load.
LEGACY_API_KEY = "step_api_key"


def _normalize_upstream(value: str) -> str:
    return value.strip().rstrip("/")


def _validate(key: str, value: Any) -> Any:
    """Return the normalized value, or raise ValueError with a user-facing reason."""
    if key in ("step_upstream", "anthropic_upstream"):
        text = _normalize_upstream(str(value or ""))
        if not text:
            raise ValueError("上游地址不能为空")
        if not text.startswith(("http://", "https://")):
            raise ValueError("上游地址必须以 http:// 或 https:// 开头")
        return text
    if key == "forward_reasoning_history":
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    raise ValueError(f"不支持的设置项: {key}")


class SettingsStore:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "settings.json"
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {}
        self._legacy_api_key: str = ""
        self._load()

    def _load(self) -> None:
        with self._lock:
            self._data = {}
            if not self.path.is_file():
                return
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                return
            if not isinstance(loaded, dict):
                return

            # Pull the retired single-key setting out before it is dropped, so
            # the caller can seed the upstream-key store with it exactly once.
            legacy = loaded.pop(LEGACY_API_KEY, None)
            self._legacy_api_key = str(legacy or "").strip()

            # Keep only known keys so a stale/corrupt file cannot poison config.
            for key in EDITABLE_KEYS:
                if key in loaded:
                    try:
                        self._data[key] = _validate(key, loaded[key])
                    except ValueError:
                        continue
            if legacy is not None:
                self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def legacy_api_key(self) -> str:
        """The pre-multi-key ``step_api_key`` value, consumed once at startup."""
        with self._lock:
            return self._legacy_api_key

    def all(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)

    def update(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Validate and apply a partial patch. Raises ValueError on bad input."""
        cleaned: dict[str, Any] = {}
        for key, value in (patch or {}).items():
            if key not in EDITABLE_KEYS:
                continue
            cleaned[key] = _validate(key, value)
        with self._lock:
            self._data.update(cleaned)
            self._save()
            return dict(self._data)
