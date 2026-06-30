"""
Configuration loader for second-brain-kit components.

Looks for a config file in these locations (first match wins):
  1. $SECOND_BRAIN_CONFIG environment variable
  2. ./config.toml (current working directory)
  3. ~/.second-brain/config.toml
"""

import os
import sys
from pathlib import Path
from typing import Any

_CONFIG_CACHE: dict[str, Any] | None = None

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULTS: dict[str, Any] = {
    "vault": {
        "path": "",
    },
    "supervisor": {
        "enabled": True,
        "port": 49522,
    },
    "writer": {
        "enabled": True,
        "port": 49520,
    },
    "memory": {
        "dirs": [
            "记忆/全局",
            "记忆/编程",
            "记忆/写作",
            "记忆/QQ Bot",
            "记忆/游戏开发",
        ],
    },
    "domains": {
        "default": "全局",
    },
    "paths": {
        "logs_dir": "~/.second-brain/logs",
        "session_file": "~/.second-brain/.current_session",
        "smart_connections_mcp": "",  # optional external tool path
        "smart_env": "",             # optional external env path
    },
}

_CONFIG_SEARCH_DIRS = [
    Path.cwd(),
    Path.home() / ".second-brain",
]


def _load_toml(path: Path) -> dict[str, Any] | None:
    """Load a TOML file. Returns None if tomllib is unavailable."""
    try:
        import tomllib  # Python 3.11+
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (ImportError, FileNotFoundError):
        pass
    # fallback: try tomli (pip package)
    try:
        import tomli as tomllib
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (ImportError, FileNotFoundError):
        pass
    return None


def _load_json(path: Path) -> dict[str, Any] | None:
    """Load a JSON config file."""
    try:
        import json
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def find_config() -> Path | None:
    """Locate the config file (.toml or .json)."""
    env_override = os.environ.get("SECOND_BRAIN_CONFIG")
    if env_override:
        p = Path(env_override)
        if p.exists():
            return p
    for base in _CONFIG_SEARCH_DIRS:
        for name in ("config.toml", "config.json"):
            candidate = base / name
            if candidate.exists():
                return candidate
    return None


def load_config(force_reload: bool = False) -> dict[str, Any]:
    """Return merged config (file overrides DEFAULTS)."""
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None and not force_reload:
        return _CONFIG_CACHE

    cfg = dict(DEFAULTS)  # shallow copy top-level keys
    cfg_path = find_config()
    if cfg_path is None:
        print(f"[config] No config file found, using defaults", file=sys.stderr)
        _CONFIG_CACHE = cfg
        return cfg

    raw: dict[str, Any] | None = None
    if cfg_path.suffix == ".toml":
        raw = _load_toml(cfg_path)
    elif cfg_path.suffix == ".json":
        raw = _load_json(cfg_path)

    if raw is None:
        print(f"[config] Could not parse {cfg_path}, using defaults", file=sys.stderr)
        _CONFIG_CACHE = cfg
        return cfg

    # Merge recursively (simple one-level merge for now)
    for section, values in raw.items():
        if section in cfg and isinstance(cfg[section], dict) and isinstance(values, dict):
            cfg[section].update(values)
        else:
            cfg[section] = values

    _CONFIG_CACHE = cfg
    return cfg


def vault_path() -> Path:
    """Return resolved vault path, falling back to env var or empty."""
    cfg = load_config()
    raw = cfg.get("vault", {}).get("path", "")
    if raw:
        return Path(raw).expanduser().resolve()
    env = os.environ.get("OBSIDIAN_VAULT")
    if env:
        return Path(env).expanduser().resolve()
    return Path()


def logs_dir() -> Path:
    """Return expanded logs directory."""
    cfg = load_config()
    raw = cfg.get("paths", {}).get("logs_dir", "~/.second-brain/logs")
    return Path(raw).expanduser().resolve()


def session_file() -> Path:
    """Return expanded session file path."""
    cfg = load_config()
    raw = cfg.get("paths", {}).get("session_file", "~/.second-brain/.current_session")
    return Path(raw).expanduser().resolve()
