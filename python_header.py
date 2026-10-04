"""
Shared environment bootstrap for all REPOS programs.

Usage — add this as first import in every entrypoint:

    from python_header import env, get, get_int, get_port

How it works:
  1. Loads config.conf from the calling script's directory
  2. Loads auxiliary *.env files, then .env; falls back to env.example if none exist
  3. Injected process env wins over file values
  4. If FASTAPI_HOST was injected by the process, the web server binds 0.0.0.0
  5. All values are accessible via env dict, get(), or os.environ

Requires: pip install python-dotenv
"""

import os
import re
from pathlib import Path

from dotenv import dotenv_values

_process_env = dict(os.environ)
_process_env_has_fastapi_host = "FASTAPI_HOST" in _process_env


def _normalize_env_value(value: str | None) -> str:
    value = "" if value is None else str(value)
    if value.strip().lower() == "blank":
        return ""
    return value


def _find_project_dir() -> Path:
    """Walk the call stack to find the project directory."""
    import inspect
    for frame_info in inspect.stack():
        caller_file = frame_info.filename
        if caller_file and not caller_file.startswith("<"):
            directory = Path(caller_file).resolve().parent
            if (directory / "config.conf").exists() or (directory / "config.conf_example").exists() or (directory / ".env").exists():
                return directory
    return Path.cwd()


def _apply_values(values: dict[str, str], overwrite: bool) -> None:
    for key, value in values.items():
        if not key:
            continue
        if overwrite or key not in os.environ:
            os.environ[key] = _normalize_env_value(value)


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for key, value in dotenv_values(path).items():
        values[key] = _normalize_env_value(value)
    return values


def _read_env_files(env_dir: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    files = sorted(p for p in env_dir.glob("*.env") if p.name != ".env")
    dot_env = env_dir / ".env"
    if dot_env.exists():
        files.append(dot_env)
    if not files:
        env_example = env_dir / "env.example"
        if env_example.exists():
            files.append(env_example)
    for path in files:
        values.update(_read_env_file(path))
    return values


_env_dir = _find_project_dir()
_config_file = _env_dir / "config.conf"
if not _config_file.exists():
    _config_file = _env_dir / "config.conf_example"
_config_values = _read_env_file(_config_file)
_file_values = dict(_config_values)
_file_values.update(_read_env_files(_env_dir))
_apply_values(_file_values, overwrite=False)

_apply_values(_process_env, overwrite=True)

if _process_env_has_fastapi_host:
    os.environ["FASTAPI_HOST"] = "0.0.0.0"


def _ensure_local_sqlite_dir() -> None:
    backend_pattern = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*_DB_BACKEND)\s*=\s*([^#]*)")
    backends: dict[str, str] = {}
    for example in sorted(_env_dir.glob("env*example")):
        for line in example.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("#"):
                continue
            match = backend_pattern.match(line)
            if match:
                backends.setdefault(match.group(1), _normalize_env_value(match.group(2)).strip())

    for key, default in backends.items():
        if os.environ.get(key, default).strip().lower() in {"sqlite", "sqlite3"}:
            (_env_dir / "sqlite").mkdir(parents=True, exist_ok=True)
            return


_ensure_local_sqlite_dir()


def get(key: str, default: str = "") -> str:
    """Get env var as string."""
    return os.environ.get(key, default).strip()


def get_int(key: str, default: int = 0) -> int:
    """Get env var as int, fallback to default on bad input."""
    raw = os.environ.get(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except (ValueError, TypeError):
        return default


def get_bool(key: str, default: bool = False) -> bool:
    """Get env var as bool (1/true/yes/on → True)."""
    raw = os.environ.get(key, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def get_port(key: str, default: int = 8080) -> int:
    """Get env var as validated port number (1-65535)."""
    port = get_int(key, default)
    if not (1 <= port <= 65535):
        raise ValueError(f"{key}={port} is not a valid port (1-65535)")
    return port



# Snapshot for dict-style access: env["KEY"] or env.get("KEY", "default")
env = dict(os.environ)
