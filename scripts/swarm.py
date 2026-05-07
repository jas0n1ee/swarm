#!/usr/bin/env python3
"""Unified tmux Swarm runtime for Claude and Codex workers."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
VENDOR_DIR = SCRIPT_DIR / "vendor"
if VENDOR_DIR.exists() and str(VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(VENDOR_DIR))

try:
    import libtmux
except ImportError as exc:
    raise SystemExit(
        "libtmux is unavailable. Run "
        "`python3 ~/.agents/skills/swarm/scripts/install.py` to vendor dependencies."
    ) from exc

RUNTIME_ROOT = Path(os.environ.get("SWARM_RUNTIME_ROOT", "/tmp/agent-swarm"))
LOG_DIR = RUNTIME_ROOT / "logs"
LAST_SEND_TS = RUNTIME_ROOT / "last_orchestrator_send"
ORCHESTRATOR_NAME = "orchestrator"
ORCHESTRATOR_SEND_GAP = 0.6
ORCHESTRATOR_NOTIFY_LIMIT = int(os.environ.get("SWARM_NOTIFY_LIMIT", "1000"))
CLAUDE_BIN = os.environ.get("SWARM_CLAUDE_BIN", "claude")
CODEX_BIN = os.environ.get("SWARM_CODEX_BIN", "codex")
DEFAULT_TICK_TEMPLATE = (
    "[timer] {interval}s tick: review workers, hand-offs, and decide next round; "
    "if you have something running, ignore this message. else, think deeper, "
    "what you can do in the TODO list or your plan, find it out. "
    "do experiments, make more plans and test it."
)


def resolve_engine(value: str | None = None) -> str:
    if value in {"claude", "codex"}:
        return value
    env_value = os.environ.get("SWARM_ENGINE")
    if env_value in {"claude", "codex"}:
        return env_value
    return "codex"


ENGINE = resolve_engine()


def get_server() -> libtmux.Server:
    return libtmux.Server()


def get_current_pane(server: libtmux.Server) -> libtmux.Pane:
    pane_id = os.environ.get("TMUX_PANE")
    if not pane_id:
        raise RuntimeError("TMUX_PANE not set - run this inside tmux")
    for session in server.sessions:
        for window in session.windows:
            for pane in window.panes:
                if pane.id == pane_id:
                    return pane
    raise RuntimeError(f"Pane {pane_id!r} not found in any tmux session")


def find_window(session: libtmux.Session, name: str) -> libtmux.Window | None:
    for window in session.windows:
        if window.name == name:
            return window
    return None


def find_orchestrator_window(session: libtmux.Session) -> libtmux.Window | None:
    for window in session.windows:
        if window.name == ORCHESTRATOR_NAME or window.name.startswith(ORCHESTRATOR_NAME + "-"):
            return window
    return None


def ensure_orchestrator_window(session: libtmux.Session, current_window: libtmux.Window) -> libtmux.Window:
    orchestrator = find_orchestrator_window(session)
    if orchestrator:
        return orchestrator
    current_window.rename_window(ORCHESTRATOR_NAME)
    current_window.set_option("automatic-rename", "off")
    return current_window


def topic_root(topic: str) -> Path:
    return RUNTIME_ROOT / "topics" / topic


def worker_root(topic: str, worker_name: str) -> Path:
    return topic_root(topic) / "workers" / worker_name


def worker_queue_dir(topic: str, worker_name: str) -> Path:
    return worker_root(topic, worker_name) / "queue"


def worker_runs_dir(topic: str, worker_name: str) -> Path:
    return worker_root(topic, worker_name) / "runs"


def worker_handoff_file(topic: str, worker_name: str) -> Path:
    return worker_root(topic, worker_name) / "handoff.md"


def worker_state_file(topic: str, worker_name: str) -> Path:
    return worker_root(topic, worker_name) / "state.json"


def worker_latest_file(topic: str, worker_name: str) -> Path:
    return worker_root(topic, worker_name) / "latest.json"


def worker_review_file(topic: str, worker_name: str) -> Path:
    return worker_root(topic, worker_name) / "review.md"


def event_log_file(topic: str) -> Path:
    return topic_root(topic) / "events.jsonl"


def _timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _slug() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _log(message: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_DIR / "swarm.log", "a", encoding="utf-8") as handle:
        handle.write(f"[{_timestamp()}] {message}\n")


def append_event(topic: str, payload: dict[str, Any]) -> None:
    payload = {
        "topic": topic,
        "created_at": datetime.now().isoformat(),
        **payload,
    }
    path = event_log_file(topic)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _wait_for_copy_mode_exit(pane: libtmux.Pane, timeout: float = 30) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        try:
            result = pane.cmd("display-message", "-p", "-t", pane.id, "#{pane_in_mode}")
            in_mode = result.stdout[0].strip() if result.stdout else "0"
            if in_mode == "0":
                return True
        except Exception:
            return True
        time.sleep(0.1)
    return False


def send_raw_to_window(window: libtmux.Window, message: str) -> None:
    pane = window.panes[0]
    _wait_for_copy_mode_exit(pane, timeout=10)
    pane.send_keys(message, enter=True)
    time.sleep(0.2)
    pane.send_keys("", enter=True)


def compact_notice(sender: str, raw_message: str, artifact: Path) -> str:
    if len(raw_message) <= ORCHESTRATOR_NOTIFY_LIMIT:
        return raw_message
    return (
        f"TASK_DONE from {sender}: message too long for tmux inline delivery "
        f"({len(raw_message)} chars). Full raw message: {artifact}"
    )


def send_to_orchestrator_safe(session: libtmux.Session, sender: str, message: str) -> None:
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    if LAST_SEND_TS.exists():
        try:
            elapsed = time.time() - float(LAST_SEND_TS.read_text(encoding="utf-8").strip())
            if elapsed < ORCHESTRATOR_SEND_GAP:
                time.sleep(ORCHESTRATOR_SEND_GAP - elapsed + 0.05)
        except (OSError, ValueError):
            pass
    LAST_SEND_TS.write_text(str(time.time()), encoding="utf-8")
    orchestrator = find_orchestrator_window(session)
    if not orchestrator:
        _log(
            f"[{session.name}:{sender}] notify skipped: no orchestrator window exists"
        )
        return
    send_raw_to_window(orchestrator, f"[{sender}] {message}")


def resolve_prompt_text(message: str | None, prompt_file: str | None) -> tuple[str, str]:
    if message and prompt_file:
        raise SystemExit("Use either --message or --prompt-file, not both")
    if prompt_file:
        path = Path(prompt_file).expanduser().resolve()
        if not path.is_file():
            raise SystemExit(f"Prompt file not found: {path}")
        return path.read_text(encoding="utf-8"), str(path)
    if message:
        return message, "inline"
    raise SystemExit("--message or --prompt-file is required")


def write_message_artifact(
    *,
    topic: str,
    engine: str,
    sender: str,
    recipient: str,
    message: str,
    source: str,
    kind: str,
) -> Path:
    msg_id = f"{_slug()}-{os.getpid()}"
    base = topic_root(topic) / "messages" / msg_id
    base.mkdir(parents=True, exist_ok=True)
    raw_path = base / "message.txt"
    raw_path.write_text(message, encoding="utf-8")
    meta = {
        "id": msg_id,
        "topic": topic,
        "session": topic,
        "engine": engine,
        "sender": sender,
        "recipient": recipient,
        "kind": kind,
        "source": source,
        "raw_message": str(raw_path),
        "created_at": datetime.now().isoformat(),
    }
    _write_json(base / "message.json", meta)
    append_event(topic, {"event": "message", **meta})
    return raw_path


def set_worker_state(topic: str, worker_name: str, **fields: Any) -> None:
    payload = {
        "topic": topic,
        "session": topic,
        "engine": ENGINE,
        "worker": worker_name,
        "updated_at": datetime.now().isoformat(),
    }
    payload.update(fields)
    _write_json(worker_state_file(topic, worker_name), payload)


def worker_queue_paths(topic: str, worker_name: str, pattern: str) -> list[Path]:
    queue_dir = worker_queue_dir(topic, worker_name)
    if not queue_dir.exists():
        return []
    return sorted(path for path in queue_dir.glob(pattern) if path.is_file())


def worker_run_dirs(topic: str, worker_name: str) -> list[Path]:
    runs_dir = worker_runs_dir(topic, worker_name)
    if not runs_dir.exists():
        return []
    return sorted(path for path in runs_dir.iterdir() if path.is_dir())


def enqueue_task(
    *,
    topic: str,
    worker_name: str,
    cwd: str,
    prompt_text: str,
    source: str,
    codex_args: list[str],
    exec_args: list[str],
) -> Path:
    queue_dir = worker_queue_dir(topic, worker_name)
    queue_dir.mkdir(parents=True, exist_ok=True)
    task_id = f"{_slug()}-{os.getpid()}"
    prompt_path = write_message_artifact(
        topic=topic,
        engine="codex",
        sender=ORCHESTRATOR_NAME,
        recipient=worker_name,
        message=prompt_text,
        source=source,
        kind="task",
    )
    payload = {
        "task_id": task_id,
        "topic": topic,
        "session": topic,
        "engine": "codex",
        "worker": worker_name,
        "sender": ORCHESTRATOR_NAME,
        "recipient": worker_name,
        "created_at": datetime.now().isoformat(),
        "cwd": str(Path(cwd).expanduser().resolve()),
        "prompt_file": str(prompt_path),
        "source": source,
        "codex_args": codex_args,
        "exec_args": exec_args,
    }
    task_path = queue_dir / f"{task_id}.json"
    _write_json(task_path, payload)
    append_event(topic, {"event": "task_queued", "worker": worker_name, "task_id": task_id, "artifact": str(task_path)})
    return task_path


def append_handoff(
    *,
    handoff_file: Path,
    worker_name: str,
    task_id: str,
    status_text: str,
    cwd: str,
    source: str,
    message_file: Path,
) -> None:
    handoff_file.parent.mkdir(parents=True, exist_ok=True)
    with open(handoff_file, "a", encoding="utf-8") as handle:
        handle.write(f"\n## {_timestamp()}\n\n")
        handle.write(f"- Worker: `{worker_name}`\n")
        handle.write(f"- Task: `{task_id}`\n")
        handle.write(f"- Status: `{status_text}`\n")
        handle.write(f"- CWD: `{cwd}`\n")
        handle.write(f"- Prompt: `{source}`\n\n")
        if message_file.exists() and message_file.stat().st_size > 0:
            handle.write(message_file.read_text(encoding="utf-8", errors="replace"))
        else:
            handle.write("_No message content captured._\n")
        handle.write("\n")


def build_codex_command(*, cwd: str, out_file: Path, codex_args: list[str], exec_args: list[str]) -> list[str]:
    command = [CODEX_BIN]
    command.extend(codex_args)
    command.extend([
        "exec",
        "-C",
        cwd,
        "--dangerously-bypass-approvals-and-sandbox",
        "--output-last-message",
        str(out_file),
        "--json",
    ])
    command.extend(exec_args)
    command.append("-")
    return command


def process_task(*, session: libtmux.Session, worker_name: str, task_path: Path) -> None:
    topic = session.name
    task = _read_json(task_path)
    task_id = task["task_id"]
    cwd = task["cwd"]
    source = task["source"]
    prompt_file = Path(task["prompt_file"])
    prompt_text = prompt_file.read_text(encoding="utf-8")
    codex_args = list(task.get("codex_args", []))
    exec_args = list(task.get("exec_args", []))
    run_dir = worker_runs_dir(topic, worker_name) / task_id
    run_dir.mkdir(parents=True, exist_ok=True)

    run_prompt_file = run_dir / "prompt.txt"
    run_prompt_file.write_text(prompt_text, encoding="utf-8")
    out_file = run_dir / "last-message.txt"
    json_file = run_dir / "events.jsonl"
    err_file = run_dir / "stderr.log"
    set_worker_state(topic, worker_name, status="running", current_task=task_id, cwd=cwd, source=source)

    command = build_codex_command(cwd=cwd, out_file=out_file, codex_args=codex_args, exec_args=exec_args)
    append_event(topic, {"event": "run_started", "engine": "codex", "worker": worker_name, "task_id": task_id, "cwd": cwd})
    with open(json_file, "wb") as stdout_handle, open(err_file, "wb") as stderr_handle:
        result = subprocess.run(
            command,
            input=prompt_text.encode("utf-8"),
            stdout=stdout_handle,
            stderr=stderr_handle,
            check=False,
        )

    status_text = "ok" if out_file.exists() and out_file.stat().st_size > 0 else "failed"
    if status_text != "ok":
        failure_message = ["Codex run did not produce a final message.", f"Exit status: {result.returncode}"]
        if err_file.exists() and err_file.stat().st_size > 0:
            failure_message.extend(["", "Stderr tail:"])
            failure_message.extend(err_file.read_text(encoding="utf-8", errors="replace").splitlines()[-40:])
        out_file.write_text("\n".join(failure_message) + "\n", encoding="utf-8")

    handoff_file = worker_handoff_file(topic, worker_name)
    append_handoff(
        handoff_file=handoff_file,
        worker_name=worker_name,
        task_id=task_id,
        status_text=status_text,
        cwd=cwd,
        source=source,
        message_file=out_file,
    )
    raw_message = out_file.read_text(encoding="utf-8", errors="replace")
    raw_artifact = write_message_artifact(
        topic=topic,
        engine="codex",
        sender=worker_name,
        recipient=ORCHESTRATOR_NAME,
        message=raw_message,
        source=str(out_file),
        kind="result",
    )
    latest = {
        "topic": topic,
        "session": topic,
        "engine": "codex",
        "worker": worker_name,
        "task_id": task_id,
        "status": status_text,
        "cwd": cwd,
        "source": source,
        "handoff": str(handoff_file),
        "last_message": str(out_file),
        "raw_message": str(raw_artifact),
        "events": str(json_file),
        "stderr": str(err_file),
        "exit_status": result.returncode,
        "updated_at": datetime.now().isoformat(),
    }
    _write_json(worker_latest_file(topic, worker_name), latest)
    set_worker_state(topic, worker_name, status="idle", current_task=None, cwd=cwd, source=source)
    append_event(topic, {"event": "run_finished", **latest})
    notice = compact_notice(
        worker_name,
        f"TASK_DONE status={status_text} handoff={handoff_file} last_message={out_file} stderr={err_file}",
        raw_artifact,
    )
    send_to_orchestrator_safe(session, worker_name, notice)
    _log(f"[{topic}] worker={worker_name} task={task_id} status={status_text}")


def next_task_file(queue_dir: Path) -> Path | None:
    candidates = sorted(path for path in queue_dir.glob("*.json") if path.is_file())
    return candidates[0] if candidates else None


def create_codex_worker_window(session: libtmux.Session, worker_name: str) -> libtmux.Window:
    window = session.new_window(window_name=worker_name, attach=False)
    window.set_option("automatic-rename", "off")
    window.set_option("remain-on-exit", "on")
    env_prefix = (
        f"SWARM_RUNTIME_ROOT={shlex.quote(str(RUNTIME_ROOT))} "
        f"SWARM_CODEX_BIN={shlex.quote(CODEX_BIN)} "
        f"SWARM_ENGINE=codex "
    )
    launcher = (
        f"{env_prefix}python3 {shlex.quote(str(Path(__file__).resolve()))} "
        f"--engine codex worker-loop --session {shlex.quote(session.name)} --name {shlex.quote(worker_name)}; "
        'exit_code=$?; printf "\\n[swarm worker exit=%s]\\n" "$exit_code"; exec zsh -i'
    )
    window.panes[0].send_keys(launcher, enter=True)
    return window


def create_claude_worker_window(session: libtmux.Session, worker_name: str) -> libtmux.Window:
    window = session.new_window(window_name=worker_name, attach=False)
    window.set_option("automatic-rename", "off")
    window.set_option("remain-on-exit", "on")
    command = (
        f"SWARM_RUNTIME_ROOT={shlex.quote(str(RUNTIME_ROOT))} "
        f"SWARM_CLAUDE_BIN={shlex.quote(CLAUDE_BIN)} "
        f"SWARM_ENGINE=claude "
        f"{shlex.quote(CLAUDE_BIN)} --dangerously-skip-permissions"
    )
    window.panes[0].send_keys(command, enter=True)
    return window


def create_worker_window(session: libtmux.Session, worker_name: str, engine: str) -> libtmux.Window:
    if find_window(session, worker_name):
        raise SystemExit(f"Worker '{worker_name}' already exists in session '{session.name}'")
    if engine == "claude":
        return create_claude_worker_window(session, worker_name)
    return create_codex_worker_window(session, worker_name)


def kill_worker_if_exists(session: libtmux.Session, worker_name: str) -> bool:
    window = find_window(session, worker_name)
    if not window:
        return False
    window.kill()
    return True


def deliver_claude_task(session: libtmux.Session, worker_name: str, prompt_text: str, source: str) -> Path:
    window = find_window(session, worker_name)
    if not window:
        raise SystemExit(f"Worker window not found: {worker_name}")
    path = write_message_artifact(
        topic=session.name,
        engine="claude",
        sender=ORCHESTRATOR_NAME,
        recipient=worker_name,
        message=prompt_text,
        source=source,
        kind="task",
    )
    notice = f"SWARM_TASK file={path} sender={ORCHESTRATOR_NAME}. Read the file, execute it, and report back."
    send_raw_to_window(window, notice)
    set_worker_state(session.name, worker_name, status="delivered", current_task=None, source=source, task_file=str(path))
    append_event(session.name, {"event": "task_delivered", "engine": "claude", "worker": worker_name, "artifact": str(path)})
    return path


def known_workers(topic: str, session: libtmux.Session) -> list[str]:
    names = {window.name for window in session.windows if window.name != ORCHESTRATOR_NAME}
    root = topic_root(topic) / "workers"
    if root.exists():
        names.update(path.name for path in root.iterdir() if path.is_dir())
    return sorted(names)


def inferred_worker_latest(topic: str, worker_name: str) -> dict[str, Any] | None:
    latest_path = worker_latest_file(topic, worker_name)
    if latest_path.exists():
        return _read_json(latest_path)
    state_path = worker_state_file(topic, worker_name)
    state_payload = _read_json(state_path) if state_path.exists() else None
    handoff_path = worker_handoff_file(topic, worker_name)
    queued_paths = worker_queue_paths(topic, worker_name, "*.json")
    working_paths = worker_queue_paths(topic, worker_name, "*.working")
    error_paths = worker_queue_paths(topic, worker_name, "*.error")
    run_dirs = worker_run_dirs(topic, worker_name)
    latest_run_dir = run_dirs[-1] if run_dirs else None
    if not any([state_payload, handoff_path.exists(), queued_paths, working_paths, error_paths, latest_run_dir]):
        return None
    payload: dict[str, Any] = {
        "topic": topic,
        "session": topic,
        "engine": state_payload.get("engine", ENGINE) if state_payload else ENGINE,
        "worker": worker_name,
        "task_id": state_payload.get("current_task", "-") if state_payload else "-",
        "status": state_payload.get("status", "-") if state_payload else "-",
        "cwd": state_payload.get("cwd", "-") if state_payload else "-",
        "source": state_payload.get("source", "-") if state_payload else "-",
        "handoff": str(handoff_path) if handoff_path.exists() else "-",
        "last_message": "-",
        "raw_message": state_payload.get("task_file", "-") if state_payload else "-",
        "events": "-",
        "stderr": "-",
        "exit_status": None,
        "updated_at": state_payload.get("updated_at", "-") if state_payload else "-",
        "inferred": True,
        "queue_pending": len(queued_paths),
        "queue_working": len(working_paths),
        "queue_error": len(error_paths),
    }
    if latest_run_dir:
        payload["task_id"] = latest_run_dir.name
        payload["last_message"] = str(latest_run_dir / "last-message.txt")
        payload["events"] = str(latest_run_dir / "events.jsonl")
        payload["stderr"] = str(latest_run_dir / "stderr.log")
        payload["updated_at"] = datetime.fromtimestamp(latest_run_dir.stat().st_mtime).isoformat()
    return payload


def worker_runtime_state(
    window: libtmux.Window | None,
    state_payload: dict[str, Any] | None,
    working_paths: list[Path] | None = None,
) -> tuple[str, str | None]:
    raw_state = state_payload.get("status", "-") if state_payload else "-"
    working_paths = working_paths or []
    if raw_state == "running" and window is None:
        if working_paths:
            names = ", ".join(path.name for path in working_paths[:2])
            extra = len(working_paths) - 2
            suffix = f" (+{extra} more)" if extra > 0 else ""
            return "stale", f"state says running but tmux window is missing; stuck queue item(s): {names}{suffix}"
        return "stale", "state says running but tmux window is missing"
    return raw_state, None


def tail_text_file(path: Path, lines: int) -> str:
    if not path.exists():
        return f"[missing] {path}"
    content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if not content:
        return f"[empty] {path}"
    return "\n".join(content[-lines:])


def append_review_note(*, topic: str, worker_name: str, note: str, handoff: str | None = None) -> Path:
    path = worker_review_file(topic, worker_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"\n## {_timestamp()}\n\n")
        handle.write(note.rstrip() + "\n")
        if handoff:
            handle.write(f"\n- Handoff: `{handoff}`\n")
    return path


def refuse_self_target(*, session: libtmux.Session, current_window: libtmux.Window, target_name: str, action: str) -> None:
    if current_window.session.id != session.id or current_window.name != target_name:
        return
    _log(f"[{session.name}:{target_name}] blocked self-target action={action}")
    raise SystemExit(f"Refusing to {action} the current window '{target_name}' from inside itself")


def cmd_spawn(args: argparse.Namespace) -> None:
    server = get_server()
    current_pane = get_current_pane(server)
    session = current_pane.window.session
    ensure_orchestrator_window(session, current_pane.window)
    worker_name = args.name
    if args.replace:
        refuse_self_target(session=session, current_window=current_pane.window, target_name=worker_name, action="replace")
        kill_worker_if_exists(session, worker_name)
    create_worker_window(session, worker_name, ENGINE)
    delivered = None
    if args.message or args.prompt_file:
        prompt_text, source = resolve_prompt_text(args.message, args.prompt_file)
        if ENGINE == "codex":
            delivered = enqueue_task(
                topic=session.name,
                worker_name=worker_name,
                cwd=args.cwd,
                prompt_text=prompt_text,
                source=source,
                codex_args=args.codex_arg,
                exec_args=args.exec_arg,
            )
        else:
            time.sleep(args.startup_delay)
            delivered = deliver_claude_task(session, worker_name, prompt_text, source)
    print(f"Spawned {ENGINE} worker '{worker_name}' in topic '{session.name}'")
    if delivered:
        print(f"Task artifact: {delivered}")
    _log(f"[{session.name}] spawned {ENGINE} {worker_name}")


def cmd_send(args: argparse.Namespace) -> None:
    server = get_server()
    current_pane = get_current_pane(server)
    session = current_pane.window.session
    ensure_orchestrator_window(session, current_pane.window)
    if args.raw:
        message, _source = resolve_prompt_text(args.message, args.prompt_file)
        window = find_window(session, args.name)
        if not window:
            raise SystemExit(f"Window not found: {args.name}")
        artifact = write_message_artifact(
            topic=session.name,
            engine=ENGINE,
            sender=ORCHESTRATOR_NAME,
            recipient=args.name,
            message=message,
            source=_source,
            kind="raw",
        )
        send_raw_to_window(window, message)
        print(f"Sent raw message to '{args.name}' (artifact: {artifact})")
        return
    if args.fresh:
        refuse_self_target(session=session, current_window=current_pane.window, target_name=args.name, action="refresh")
        kill_worker_if_exists(session, args.name)
        create_worker_window(session, args.name, ENGINE)
        if ENGINE == "claude":
            time.sleep(args.startup_delay)
    elif not find_window(session, args.name):
        raise SystemExit(f"Worker window not found: {args.name}")
    prompt_text, source = resolve_prompt_text(args.message, args.prompt_file)
    if ENGINE == "codex":
        task_path = enqueue_task(
            topic=session.name,
            worker_name=args.name,
            cwd=args.cwd,
            prompt_text=prompt_text,
            source=source,
            codex_args=args.codex_arg,
            exec_args=args.exec_arg,
        )
        print(f"Queued task for '{args.name}': {task_path}")
    else:
        task_path = deliver_claude_task(session, args.name, prompt_text, source)
        print(f"Delivered task reference to '{args.name}': {task_path}")
    _log(f"[{session.name}] sent task to {args.name}")


def cmd_show(args: argparse.Namespace) -> None:
    server = get_server()
    current_pane = get_current_pane(server)
    session = current_pane.window.session
    ensure_orchestrator_window(session, current_pane.window)
    latest = inferred_worker_latest(session.name, args.name)
    if not latest:
        raise SystemExit(f"No worker runtime artifacts found for '{args.name}'")
    window = find_window(session, args.name)
    state_path = worker_state_file(session.name, args.name)
    state_payload = _read_json(state_path) if state_path.exists() else None
    queued_paths = worker_queue_paths(session.name, args.name, "*.json")
    working_paths = worker_queue_paths(session.name, args.name, "*.working")
    error_paths = worker_queue_paths(session.name, args.name, "*.error")
    runtime_state, runtime_note = worker_runtime_state(window, state_payload, working_paths)
    print(f"Topic: {session.name}")
    print(f"Worker: {args.name}")
    print(f"Engine: {latest.get('engine', ENGINE)}")
    print(f"Window: {'yes' if window else 'no'}")
    print(f"Runtime state: {runtime_state}")
    if runtime_note:
        print(f"Runtime note: {runtime_note}")
    if latest.get("inferred"):
        print("Result source: inferred from runtime artifacts (latest.json missing)")
    for label, key in [
        ("Status", "status"), ("Task", "task_id"), ("CWD", "cwd"), ("Prompt", "source"),
        ("Updated", "updated_at"), ("Handoff", "handoff"), ("Last message", "last_message"),
        ("Raw message", "raw_message"), ("Events", "events"), ("Stderr", "stderr"),
    ]:
        print(f"{label}: {latest.get(key, '-')}")
    print(f"Queue pending: {len(queued_paths)}")
    print(f"Queue working: {len(working_paths)}")
    print(f"Queue error: {len(error_paths)}")
    review_path = worker_review_file(session.name, args.name)
    print(f"Review: {review_path if review_path.exists() else '-'}")
    for key, title in [("handoff", "handoff tail"), ("last_message", "last message tail"), ("raw_message", "raw message tail")]:
        value = latest.get(key, "-")
        if value and value != "-" and Path(value).exists():
            print(f"\n--- {title} ---")
            print(tail_text_file(Path(value), args.lines))
            break
    if review_path.exists():
        print("\n--- review tail ---")
        print(tail_text_file(review_path, min(args.lines, 40)))


def cmd_tail(args: argparse.Namespace) -> None:
    server = get_server()
    current_pane = get_current_pane(server)
    session = current_pane.window.session
    ensure_orchestrator_window(session, current_pane.window)
    latest = inferred_worker_latest(session.name, args.name)
    if not latest:
        raise SystemExit(f"No worker runtime artifacts found for '{args.name}'")
    mapping = {
        "handoff": Path(latest["handoff"]) if latest.get("handoff") not in (None, "-") else None,
        "last_message": Path(latest["last_message"]) if latest.get("last_message") not in (None, "-") else None,
        "raw_message": Path(latest["raw_message"]) if latest.get("raw_message") not in (None, "-") else None,
        "stderr": Path(latest["stderr"]) if latest.get("stderr") not in (None, "-") else None,
        "events": Path(latest["events"]) if latest.get("events") not in (None, "-") else None,
        "review": worker_review_file(session.name, args.name),
    }
    path = mapping[args.surface]
    if path is None:
        raise SystemExit(f"No {args.surface} artifact found for '{args.name}'")
    print(tail_text_file(path, args.lines))


def cmd_kill(args: argparse.Namespace) -> None:
    server = get_server()
    current_pane = get_current_pane(server)
    session = current_pane.window.session
    refuse_self_target(session=session, current_window=current_pane.window, target_name=args.name, action="kill")
    if not kill_worker_if_exists(session, args.name):
        raise SystemExit(f"Window not found: {args.name}")
    set_worker_state(session.name, args.name, status="killed")
    append_event(session.name, {"event": "worker_killed", "worker": args.name})
    print(f"Killed '{args.name}'")


def cmd_ping(args: argparse.Namespace) -> None:
    server = get_server()
    current_pane = get_current_pane(server)
    session = current_pane.window.session
    orchestrator = ensure_orchestrator_window(session, current_pane.window)
    message = args.message or "请检查当前 worker 状态、artifact 和下一步。"
    artifact = write_message_artifact(
        topic=session.name,
        engine=ENGINE,
        sender=current_pane.window.name,
        recipient=ORCHESTRATOR_NAME,
        message=message,
        source="inline",
        kind="ping",
    )
    send_raw_to_window(orchestrator, compact_notice(current_pane.window.name, message, artifact))
    print(f"Pinged orchestrator in topic '{session.name}'")


def cmd_status(args: argparse.Namespace) -> None:
    server = get_server()
    current_pane = get_current_pane(server)
    session = current_pane.window.session
    ensure_orchestrator_window(session, current_pane.window)
    print(f"Topic: {session.name}")
    print(f"Engine: {ENGINE}")
    print(f"Runtime: {RUNTIME_ROOT}")
    print(f"\n{'Worker':<24} {'Window':<8} {'Active':<6} {'Queued':<6} {'State':<10} Handoff")
    print("-" * 98)
    active_window_id = current_pane.window.id
    print(f"{ORCHESTRATOR_NAME:<24} {'yes':<8} {'yes':<6} {'-':<6} {'-':<10} -")
    for worker_name in known_workers(session.name, session):
        window = find_window(session, worker_name)
        window_live = "yes" if window else "no"
        active = "yes" if window and window.id == active_window_id else ""
        queued_paths = worker_queue_paths(session.name, worker_name, "*.json")
        working_paths = worker_queue_paths(session.name, worker_name, "*.working")
        queued = str(len(queued_paths) + len(working_paths))
        state_file = worker_state_file(session.name, worker_name)
        state_payload = _read_json(state_file) if state_file.exists() else None
        state, state_note = worker_runtime_state(window, state_payload, working_paths)
        handoff_path = worker_handoff_file(session.name, worker_name)
        handoff = str(handoff_path) if handoff_path.exists() else "-"
        if state_note:
            handoff = f"{handoff} ({state_note})" if handoff != "-" else state_note
        print(f"{worker_name:<24} {window_live:<8} {active:<6} {queued:<6} {state:<10} {handoff}")


def cmd_worker_loop(args: argparse.Namespace) -> None:
    server = get_server()
    current_pane = get_current_pane(server)
    session = current_pane.window.session
    if session.name != args.session:
        _log(f"worker-loop session mismatch: expected '{args.session}', got '{session.name}'")
    queue_dir = worker_queue_dir(args.session, args.name)
    queue_dir.mkdir(parents=True, exist_ok=True)
    set_worker_state(args.session, args.name, status="idle", current_task=None)
    print(f"[swarm] worker-loop ready: topic={args.session} worker={args.name}")
    while True:
        task_path = next_task_file(queue_dir)
        if not task_path:
            time.sleep(args.poll_interval)
            continue
        working_path = task_path.with_suffix(".working")
        try:
            task_path.rename(working_path)
        except FileNotFoundError:
            continue
        try:
            process_task(session=session, worker_name=args.name, task_path=working_path)
        except Exception as exc:
            _log(f"worker '{args.name}' failed while processing {working_path.name}: {exc}")
            error_file = working_path.with_suffix(".error")
            working_path.rename(error_file)
            set_worker_state(args.session, args.name, status="error", current_task=working_path.name)
            send_to_orchestrator_safe(session, args.name, f"TASK_DONE status=failed error={exc}")
        else:
            working_path.unlink(missing_ok=True)


def cmd_note(args: argparse.Namespace) -> None:
    server = get_server()
    current_pane = get_current_pane(server)
    session = current_pane.window.session
    ensure_orchestrator_window(session, current_pane.window)
    handoff = None
    latest_path = worker_latest_file(session.name, args.name)
    if latest_path.exists():
        handoff = _read_json(latest_path).get("handoff")
    path = append_review_note(topic=session.name, worker_name=args.name, note=args.note, handoff=handoff)
    print(f"Review note appended to {path}")


def cmd_stop_hook(_args: argparse.Namespace) -> None:
    if not os.environ.get("TMUX"):
        return
    try:
        server = get_server()
        current_pane = get_current_pane(server)
        current_window = current_pane.window
        session = current_window.session
        identity = current_window.name
        raw = sys.stdin.read()
        try:
            data = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as exc:
            _log(f"stop-hook JSON parse failed: {exc}; raw first 300: {raw[:300]}")
            return
        last_message = data.get("last_assistant_message") or data.get("message") or ""
        if identity.startswith(ORCHESTRATOR_NAME):
            return
        if not last_message:
            return
        artifact = write_message_artifact(
            topic=session.name,
            engine="claude",
            sender=identity,
            recipient=ORCHESTRATOR_NAME,
            message=last_message,
            source="stop-hook",
            kind="result",
        )
        handoff = worker_handoff_file(session.name, identity)
        append_handoff(
            handoff_file=handoff,
            worker_name=identity,
            task_id=Path(artifact).parent.name,
            status_text="reported",
            cwd="-",
            source="stop-hook",
            message_file=artifact,
        )
        _write_json(worker_latest_file(session.name, identity), {
            "topic": session.name,
            "session": session.name,
            "engine": "claude",
            "worker": identity,
            "task_id": Path(artifact).parent.name,
            "status": "reported",
            "cwd": "-",
            "source": "stop-hook",
            "handoff": str(handoff),
            "last_message": str(artifact),
            "raw_message": str(artifact),
            "events": "-",
            "stderr": "-",
            "exit_status": None,
            "updated_at": datetime.now().isoformat(),
        })
        send_to_orchestrator_safe(session, identity, compact_notice(identity, last_message, artifact))
    except Exception as exc:
        _log(f"stop-hook failed: {exc}")


def cmd_supervise(args: argparse.Namespace) -> None:
    interval_seconds = args.interval_seconds
    if args.interval_minutes is not None:
        interval_seconds = args.interval_minutes * 60
    if interval_seconds <= 0:
        raise SystemExit("--interval-seconds/--interval-minutes must be greater than zero")
    server = get_server()
    current_pane = get_current_pane(server)
    session = current_pane.window.session
    if not find_orchestrator_window(session):
        raise SystemExit(f"No orchestrator window found in current tmux session '{session.name}'")
    message = args.message or DEFAULT_TICK_TEMPLATE.format(interval=int(interval_seconds))
    print(f"[swarm] supervising topic={session.name} interval={interval_seconds:g}s")
    while True:
        time.sleep(interval_seconds)
        orchestrator = find_orchestrator_window(session)
        if not orchestrator:
            raise SystemExit(f"No orchestrator window found in current tmux session '{session.name}'")
        send_raw_to_window(orchestrator, message)
        _log(f"[{session.name}] supervise tick sent interval={interval_seconds:g}s")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Unified tmux Swarm runtime for Claude and Codex")
    parser.add_argument("--engine", choices=["codex", "claude"], help="Provider engine for this command")
    public_commands = "{spawn,send,kill,status,show,tail,note,ping,supervise}"
    subparsers = parser.add_subparsers(dest="command", required=True, metavar=public_commands)

    def add_name(cmd: argparse.ArgumentParser) -> None:
        cmd.add_argument("--name", required=True, help="Worker/window name, for example worker-x")

    def add_task_options(cmd: argparse.ArgumentParser) -> None:
        cmd.add_argument("--message", help="Inline prompt/message text")
        cmd.add_argument("--prompt-file", help="Read prompt/message text from file")
        cmd.add_argument("--cwd", default=os.getcwd(), help="Working directory for Codex workers")
        cmd.add_argument("--codex-arg", action="append", default=[], help="Extra top-level arg for `codex`")
        cmd.add_argument("--exec-arg", action="append", default=[], help="Extra arg after `codex exec`")
        cmd.add_argument("--startup-delay", type=float, default=3.0, help=argparse.SUPPRESS)

    spawn = subparsers.add_parser("spawn", help="Create a worker window and optionally deliver the first task")
    add_name(spawn)
    spawn.add_argument("--replace", action="store_true", help="Kill an existing worker window with the same name first")
    add_task_options(spawn)
    spawn.set_defaults(func=cmd_spawn)

    send = subparsers.add_parser("send", help="Deliver a task to a worker")
    add_name(send)
    send.add_argument("--raw", action="store_true", help="Send raw text to an existing tmux window")
    send.add_argument("--fresh", action="store_true", help="Kill and recreate the worker window before delivery")
    add_task_options(send)
    send.set_defaults(func=cmd_send)

    kill = subparsers.add_parser("kill", help="Kill a worker window")
    add_name(kill)
    kill.set_defaults(func=cmd_kill)

    status = subparsers.add_parser("status", help="Show session windows and worker queue state")
    status.set_defaults(func=cmd_status)

    show = subparsers.add_parser("show", help="Show latest result metadata and artifact tail for a worker")
    add_name(show)
    show.add_argument("--lines", type=int, default=40)
    show.set_defaults(func=cmd_show)

    tail = subparsers.add_parser("tail", help="Tail the latest worker artifact")
    add_name(tail)
    tail.add_argument("surface", choices=["handoff", "last_message", "raw_message", "stderr", "events", "review"], nargs="?", default="stderr")
    tail.add_argument("--lines", type=int, default=40)
    tail.set_defaults(func=cmd_tail)

    ping = subparsers.add_parser("ping", help="Send a message to the orchestrator window")
    ping.add_argument("--message", help="Inline ping text")
    ping.add_argument("--prompt-file", help="Read ping text from file")
    ping.set_defaults(func=lambda args: setattr(args, "message", resolve_prompt_text(args.message, args.prompt_file)[0] if args.prompt_file else args.message) or cmd_ping(args))

    worker_loop = subparsers.add_parser("worker-loop", help=argparse.SUPPRESS)
    worker_loop.add_argument("--session", required=True)
    worker_loop.add_argument("--name", required=True)
    worker_loop.add_argument("--poll-interval", type=float, default=1.0)
    worker_loop.set_defaults(func=cmd_worker_loop)

    stop_hook = subparsers.add_parser("stop-hook", help=argparse.SUPPRESS)
    stop_hook.set_defaults(func=cmd_stop_hook)

    note = subparsers.add_parser("note", help="Append an orchestrator review note for a worker")
    add_name(note)
    note.add_argument("note")
    note.set_defaults(func=cmd_note)

    supervise = subparsers.add_parser("supervise", help="Run a timer that nudges the orchestrator in this tmux session")
    supervise.add_argument("--interval-minutes", type=float, help="Tick interval in minutes")
    supervise.add_argument("--interval-seconds", type=float, default=3600.0, help="Tick interval in seconds")
    supervise.add_argument("--message", help="Override the default timer tick message")
    supervise.set_defaults(func=cmd_supervise)
    subparsers._choices_actions = [  # type: ignore[attr-defined]
        action for action in subparsers._choices_actions  # type: ignore[attr-defined]
        if action.dest not in {"worker-loop", "stop-hook"}
    ]
    return parser


def main() -> None:
    global ENGINE
    parser = build_parser()
    args = parser.parse_args()
    ENGINE = resolve_engine(args.engine)
    try:
        args.func(args)
    except KeyboardInterrupt:
        raise SystemExit(130)


if __name__ == "__main__":
    main()
