"""Configuration loading shared with the Rust core.

`configs/config.example.toml` holds every default; `configs/config.toml` (optional, git-ignored) is
deep-merged on top. Secrets are never read from TOML: `*_secret` keys only name a secret, which is
resolved by `resolve_secret`.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

EXAMPLE_CONFIG = Path("configs") / "config.example.toml"
USER_CONFIG = Path("configs") / "config.toml"
CREDENTIAL_TARGET_PREFIX = "local-gemini-agent/"


class ConfigError(RuntimeError):
    """Configuration is missing or invalid."""


def find_repo_root(start: Path | None = None) -> Path:
    """Locate the repository root: `LGA_ROOT` if set, otherwise the nearest ancestor with the example config."""
    env_root = os.environ.get("LGA_ROOT")
    if env_root:
        root = Path(env_root).resolve()
        if not (root / EXAMPLE_CONFIG).is_file():
            raise ConfigError(f"LGA_ROOT={root} does not contain {EXAMPLE_CONFIG}")
        return root
    here = (start or Path(__file__)).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / EXAMPLE_CONFIG).is_file():
            return candidate
    raise ConfigError(f"could not find {EXAMPLE_CONFIG} above {here}; set LGA_ROOT")


def _deep_merge(base: dict[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


@dataclass(frozen=True)
class Config:
    root: Path
    data: dict[str, Any]

    def section(self, name: str) -> dict[str, Any]:
        node: Any = self.data
        for part in name.split("."):
            if not isinstance(node, dict) or part not in node:
                raise ConfigError(f"missing config section [{name}]")
            node = node[part]
        if not isinstance(node, dict):
            raise ConfigError(f"[{name}] is not a table")
        return node

    def get(self, dotted: str) -> Any:
        section, _, key = dotted.rpartition(".")
        table = self.section(section) if section else self.data
        if key not in table:
            raise ConfigError(f"missing config key {dotted}")
        return table[key]

    def path(self, dotted: str) -> Path:
        """A config value interpreted as a path relative to the repo root. Empty string is an error."""
        value = self.get(dotted)
        if not isinstance(value, str) or not value:
            raise ConfigError(f"{dotted} must be a non-empty path string")
        candidate = Path(os.path.expandvars(value))
        return candidate if candidate.is_absolute() else (self.root / candidate)

    def seconds(self, dotted: str) -> float:
        """A `*_ms` value converted to seconds."""
        value = self.get(dotted)
        if not isinstance(value, int | float) or value < 0:
            raise ConfigError(f"{dotted} must be a non-negative number of milliseconds")
        return float(value) / 1000.0


def load_config(root: Path | None = None) -> Config:
    repo_root = root or find_repo_root()
    example = repo_root / EXAMPLE_CONFIG
    with example.open("rb") as fh:
        data = tomllib.load(fh)
    user = repo_root / USER_CONFIG
    if user.is_file():
        with user.open("rb") as fh:
            data = _deep_merge(data, tomllib.load(fh))
    override = os.environ.get("LGA_CONFIG")
    if override:
        override_path = Path(override)
        with override_path.open("rb") as fh:
            data = _deep_merge(data, tomllib.load(fh))
    return Config(root=repo_root, data=data)


def _read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _read_credential_manager(name: str) -> str | None:
    import pywintypes
    import win32cred

    try:
        credential = win32cred.CredRead(CREDENTIAL_TARGET_PREFIX + name, win32cred.CRED_TYPE_GENERIC)
    except pywintypes.error as exc:
        # ERROR_NOT_FOUND (1168) simply means the secret is not stored there.
        if exc.winerror == 1168:
            return None
        raise
    blob = credential.get("CredentialBlob") or b""
    return blob.decode("utf-16-le") if blob else None


def resolve_secret(config: Config, name: str) -> str | None:
    """Resolve a secret by name: process env, then repo `.env`, then Windows Credential Manager.

    Returns None when the secret is not configured anywhere. Values are never logged.
    """
    if not name:
        return None
    value = os.environ.get(name)
    if value:
        return value
    value = _read_dotenv(config.root / ".env").get(name)
    if value:
        return value
    return _read_credential_manager(name)
