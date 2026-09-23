"""Configuration from environment variables + WebUI overrides."""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .settings import SettingsStore


def _truthy(val: str | None, default: str = "0") -> bool:
    return (val if val is not None else default).strip().lower() in ("1", "true", "yes", "on")


class Config:
    def __init__(self) -> None:
        self.host: str = os.environ.get("PROXY_HOST", "0.0.0.0")
        self.port: int = int(os.environ.get("PROXY_PORT", "8722"))
        # Env values are the fallback whenever no WebUI override is stored.
        self._env_upstream: str = os.environ.get(
            "STEP_UPSTREAM", "https://api.stepfun.ai/step_plan/v1"
        ).rstrip("/")
        self._env_forward_reasoning_history: bool = _truthy(
            os.environ.get("FORWARD_REASONING_HISTORY"), "0"
        )
        self.force_buffer: bool = _truthy(os.environ.get("FORCE_BUFFER"), "1")
        self.debug_dump: bool = _truthy(os.environ.get("DEBUG_DUMP"), "0")
        # Default base URL for "Claude Code / Anthropic SDK" upstream nodes —
        # StepFun's Anthropic-compatible Plan endpoint. Only the base goes in;
        # the client appends /v1/messages. ANTHROPIC_BASE_URL is accepted as an
        # alias (StepFun's access-info page uses that name).
        self._env_anthropic_upstream: str = (
            os.environ.get("ANTHROPIC_UPSTREAM")
            or os.environ.get("ANTHROPIC_BASE_URL")
            or "https://api.stepfun.ai/step_plan"
        ).rstrip("/")
        # Fallback max_tokens for Anthropic requests, which require the field.
        try:
            self.anthropic_max_tokens: int = int(
                os.environ.get("ANTHROPIC_MAX_TOKENS", "8192") or "8192"
            )
        except ValueError:
            self.anthropic_max_tokens = 8192

        data_dir = os.environ.get("DATA_DIR", "./data")
        self.data_dir: Path = Path(data_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.settings = SettingsStore(self.data_dir)

        # Effective values: start from env, then let stored overrides win.
        self.upstream: str = self._env_upstream
        self.forward_reasoning_history: bool = self._env_forward_reasoning_history
        self.anthropic_upstream: str = self._env_anthropic_upstream
        self._apply_overrides(self.settings.all())

        self.master_token: str = self._resolve_master_token()

    def seed_api_key(self) -> str:
        """Key used to seed the upstream-key store on first run.

        Precedence: the retired settings.json value, then STEP_API_KEY.
        """
        return self.settings.legacy_api_key() or os.environ.get("STEP_API_KEY", "").strip()

    def _apply_overrides(self, overrides: dict[str, Any]) -> None:
        if overrides.get("step_upstream"):
            self.upstream = str(overrides["step_upstream"]).rstrip("/")
        if overrides.get("anthropic_upstream"):
            self.anthropic_upstream = str(overrides["anthropic_upstream"]).rstrip("/")
        if "forward_reasoning_history" in overrides:
            self.forward_reasoning_history = bool(overrides["forward_reasoning_history"])

    def apply_settings(self, patch: dict[str, Any]) -> None:
        """Persist a WebUI patch and re-derive the effective config values."""
        stored = self.settings.update(patch)
        self._apply_overrides(stored)

    def _resolve_master_token(self) -> str:
        env = os.environ.get("PROXY_MASTER_TOKEN", "").strip()
        if env:
            return env
        token_file = self.data_dir / "master.token"
        if token_file.is_file():
            saved = token_file.read_text(encoding="utf-8").strip()
            if saved:
                return saved
        # First start: generate, print, persist
        token = "mtp_" + secrets.token_urlsafe(32)
        token_file.write_text(token + "\n", encoding="utf-8")
        try:
            token_file.chmod(0o600)
        except OSError:
            pass
        print("=" * 60, flush=True)
        print("PROXY_MASTER_TOKEN was unset — generated a new master token:", flush=True)
        print(f"  {token}", flush=True)
        print(f"Saved to {token_file}", flush=True)
        print("Use this to log in to the WebUI. Keep it secret.", flush=True)
        print("=" * 60, flush=True)
        return token

    @property
    def upstream_host(self) -> str:
        try:
            return urlparse(self.upstream).netloc or self.upstream
        except Exception:
            return self.upstream

    def status_dict(
        self,
        token_count: int = 0,
        upstream_count: int = 0,
        anthropic_count: int = 0,
    ) -> dict:
        return {
            "host": self.host,
            "port": self.port,
            "upstream": self.upstream,
            "upstream_host": self.upstream_host,
            "anthropic_upstream": self.anthropic_upstream,
            "anthropic_count": anthropic_count,
            "force_buffer": self.force_buffer,
            "forward_reasoning_history": self.forward_reasoning_history,
            "token_count": token_count,
            "upstream_count": upstream_count,
            "data_dir": str(self.data_dir),
        }

    def settings_dict(self) -> dict:
        """Values for the WebUI settings card."""
        stored = self.settings.all()
        return {
            "step_upstream": self.upstream,
            "anthropic_upstream": self.anthropic_upstream,
            "forward_reasoning_history": self.forward_reasoning_history,
            "force_buffer": self.force_buffer,
            "max_tokens_per_upstream": 3,
            "overridden": {
                "step_upstream": "step_upstream" in stored,
                "anthropic_upstream": "anthropic_upstream" in stored,
                "forward_reasoning_history": "forward_reasoning_history" in stored,
            },
        }


_cfg: Config | None = None


def get_config() -> Config:
    global _cfg
    if _cfg is None:
        _cfg = Config()
    return _cfg


def reset_config() -> Config:
    global _cfg
    _cfg = Config()
    return _cfg
