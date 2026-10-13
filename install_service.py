#!/usr/bin/env python3
"""Render a user systemd service from private .env settings. Does not start it."""
import argparse
import os
from pathlib import Path

from env_config import ROOT, get_setting, local_path, read_env


def systemd_quote(value, *, exec_argument=False):
    value = str(value)
    if any(ch in value for ch in "\n\r\0"):
        raise ValueError("Service paths must not contain control characters")
    # Escape specifiers and quotes; values are arguments, never shell commands.
    value = value.replace("%", "%%")
    if exec_argument:
        value = value.replace("$", "$$")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_service(env_file):
    settings = read_env(env_file, required=True)
    if not get_setting(settings, "WHIPPER_LOG_FILE"):
        raise ValueError("Set WHIPPER_LOG_FILE before installing the background service")
    root = local_path(get_setting(settings, "WHIPPER_INSTALL_DIR", str(ROOT))).resolve()
    python = local_path(get_setting(settings, "WHIPPER_PYTHON", str(root / ".venv/bin/python")), base=root).absolute()
    script = root / "whipper.py"
    if not script.is_file() or not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError("Project script or Python interpreter missing; set up .venv first")
    template = (ROOT / "codex-whipper.service").read_text()
    values = {"PYTHON": python, "SCRIPT": script, "ENV_FILE": env_file.resolve()}
    for key, value in values.items():
        template = template.replace("@" + key + "@", systemd_quote(value, exec_argument=True))
    return template


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--output", type=Path, default=Path.home() / ".config/systemd/user/codex-whipper.service")
    args = parser.parse_args(argv)
    try:
        text = render_service(args.env_file.expanduser())
        output = args.output.expanduser()
        if output.resolve().is_relative_to(ROOT):
            raise ValueError("Write private generated services outside the project directory")
        if list(output.with_name(output.name + ".d").glob("*.conf")):
            raise ValueError("Existing service drop-ins may override .env; review them before installing")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text)
        print(f"Service written to {output}")
        print("Run: systemctl --user daemon-reload && systemctl --user start codex-whipper.service")
        return 0
    except (OSError, ValueError) as e:
        parser.error(str(e))


if __name__ == "__main__":
    raise SystemExit(main())
