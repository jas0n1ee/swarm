#!/usr/bin/env python3
"""Install bootstrap for the Swarm skill."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_ROOT / "scripts"
VENDOR_DIR = SCRIPTS_DIR / "vendor"
CANONICAL_SWARM = Path.home() / ".agents" / "skills" / "swarm" / "scripts" / "swarm.py"
CLAUDE_STOP_COMMAND = "SWARM_ENGINE=claude python3 ~/.agents/skills/swarm/scripts/swarm.py stop-hook"


def log(message: str) -> None:
    print(f"[swarm install] {message}")


def warn(message: str) -> None:
    print(f"[swarm install] warning: {message}", file=sys.stderr)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {path}: {exc}") from exc


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def ensure_claude_stop_hook() -> None:
    claude_home = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")).expanduser()
    if not claude_home.exists():
        log(f"Claude config not found at {claude_home}; skipping Claude Stop hook")
        return
    settings_path = claude_home / "settings.json"
    settings = load_json(settings_path, {})
    if not isinstance(settings, dict):
        raise SystemExit(f"{settings_path} must contain a JSON object")
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise SystemExit(f"{settings_path} hooks must be a JSON object")
    stop_entries = hooks.setdefault("Stop", [])
    if not isinstance(stop_entries, list):
        raise SystemExit(f"{settings_path} hooks.Stop must be a JSON array")

    for entry in stop_entries:
        if not isinstance(entry, dict):
            continue
        for hook in entry.get("hooks", []):
            if isinstance(hook, dict) and hook.get("command") == CLAUDE_STOP_COMMAND:
                log("Claude Stop hook already configured")
                return

    stop_entries.append({
        "matcher": "",
        "hooks": [
            {
                "type": "command",
                "command": CLAUDE_STOP_COMMAND,
            }
        ],
    })
    write_json(settings_path, settings)
    log(f"Added Claude Stop hook to {settings_path}")


def check_skill_link(path: Path) -> None:
    if not path.exists():
        warn(f"{path} is missing; run `npx skills add github.com/jas0n1ee/swarm -g -a codex -a claude-code -y`")
        return
    try:
        resolved = path.resolve()
    except OSError:
        warn(f"{path} exists but cannot be resolved")
        return
    if resolved != SKILL_ROOT:
        warn(f"{path} resolves to {resolved}, expected {SKILL_ROOT}")
    else:
        log(f"Verified skill link {path}")


def check_everywhere() -> None:
    candidates = [
        Path.cwd() / ".everywhere" / "bin" / "feishu-bridge",
        Path.home() / ".everywhere" / "bin" / "feishu-bridge",
    ]
    if shutil.which("feishu-bridge") or any(path.exists() for path in candidates):
        log("Everywhere bridge appears available")
    else:
        warn("Everywhere bridge not found; human escalation will require installing Everywhere separately")


def libtmux_available() -> bool:
    if importlib.util.find_spec("libtmux") is not None:
        return True
    vendor = VENDOR_DIR / "libtmux"
    return vendor.exists()


def fetch_bytes(url: str) -> bytes:
    try:
        with urllib.request.urlopen(url) as response:
            return response.read()
    except Exception as urlopen_error:
        try:
            return subprocess.check_output(["curl", "-fsSL", url])
        except Exception as curl_error:
            raise RuntimeError(
                f"Failed to download {url}: urlopen={urlopen_error}; curl={curl_error}"
            ) from curl_error


def vendor_libtmux() -> None:
    VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    if (VENDOR_DIR / "libtmux").exists():
        log(f"libtmux already vendored in {VENDOR_DIR}")
        return
    log("Vendoring libtmux from PyPI")
    data = json.loads(fetch_bytes("https://pypi.org/pypi/libtmux/json"))
    latest = data["info"]["version"]
    wheels = [
        entry for entry in data["releases"][latest]
        if entry["filename"].endswith("-py3-none-any.whl")
    ]
    if not wheels:
        raise SystemExit("No pure-Python libtmux wheel found")
    with tempfile.NamedTemporaryFile(suffix=".whl", delete=False) as tmp:
        tmp.write(fetch_bytes(wheels[0]["url"]))
        tmp_path = Path(tmp.name)
    try:
        with zipfile.ZipFile(tmp_path) as wheel:
            members = [name for name in wheel.namelist() if name.startswith("libtmux/")]
            wheel.extractall(VENDOR_DIR, members)
    finally:
        tmp_path.unlink(missing_ok=True)
    log(f"Vendored libtmux into {VENDOR_DIR}")


def prepare_dependencies(offline: bool) -> None:
    if libtmux_available():
        log("libtmux available")
        return
    if offline:
        warn("libtmux unavailable and --offline was set; rerun without --offline to vendor dependencies")
        return
    vendor_libtmux()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bootstrap the Swarm skill")
    parser.add_argument("--offline", action="store_true", help="Do not download missing dependencies")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if CANONICAL_SWARM.resolve() != (SCRIPTS_DIR / "swarm.py").resolve():
        warn(f"running from {SKILL_ROOT}; canonical path is expected at {CANONICAL_SWARM}")
    check_skill_link(Path.home() / ".codex" / "skills" / "swarm")
    check_skill_link(Path.home() / ".claude" / "skills" / "swarm")
    check_everywhere()
    prepare_dependencies(args.offline)
    ensure_claude_stop_hook()
    log("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
