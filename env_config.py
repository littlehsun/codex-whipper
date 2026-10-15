"""Read private .env settings without executing shell code or interpolating values."""
from pathlib import Path
import os
import re
import shlex

ROOT = Path(__file__).resolve().parent
KEYS = frozenset({
    "WHIPPER_CODEX_HOME", "WHIPPER_DATABASE", "WHIPPER_SOCKET", "WHIPPER_STATE",
    "WHIPPER_THREADS", "WHIPPER_HERDR_THREADS", "WHIPPER_HERDR_BIN", "WHIPPER_LOG_FILE",
    "WHIPPER_MESSAGE", "WHIPPER_DELAY", "WHIPPER_MAX_DELAY", "WHIPPER_MAX_RETRIES",
    "WHIPPER_MAX_AGE", "WHIPPER_POLL", "WHIPPER_LOOKBACK", "WHIPPER_RECONCILE_ON_START",
    "WHIPPER_DRY_RUN", "WHIPPER_ONCE", "WHIPPER_INSTALL_DIR", "WHIPPER_PYTHON",
    "WHIPPER_SUBAGENTS",
})


def read_env(path: Path, *, required=False):
    path = path.expanduser()
    if not path.exists():
        if required:
            raise ValueError("The requested .env file does not exist")
        return {}
    result = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, equals, value = line.partition("=")
        key = key.strip()
        if not equals or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key not in KEYS:
            raise ValueError(f"Unsupported setting at .env line {number}")
        if key in result:
            raise ValueError(f"Duplicate setting at .env line {number}")
        try:
            parts = shlex.split(value, comments=True, posix=True)
        except ValueError as e:
            raise ValueError(f"Invalid quoting at .env line {number}") from e
        if len(parts) > 1:
            raise ValueError(f"Quote values containing spaces at .env line {number}")
        result[key] = parts[0] if parts else ""
    return result


def get_setting(settings, name, default=None):
    """Process environment takes precedence over the private file."""
    return os.environ.get(name, settings.get(name)) or default


def boolean_setting(settings, name, default=False):
    value = get_setting(settings, name)
    if value is None:
        return default
    if value.lower() in {"1", "true", "yes", "on"}:
        return True
    if value.lower() in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def list_setting(settings, name):
    return [part.strip() for part in get_setting(settings, name, "").split(",") if part.strip()]


def local_path(value, base=ROOT):
    path = Path(value).expanduser()
    return path if path.is_absolute() else base / path
