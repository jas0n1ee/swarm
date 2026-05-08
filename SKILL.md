---
name: "swarm"
description: "Use when the user asks to run or supervise the local tmux Swarm orchestrator-worker runtime, create or manage Codex/Claude workers through swarm.py, review worker TASK_DONE reports, use the supervise timer, or improve Swarm rules/runtime behavior."
---

# Swarm

Swarm is a local tmux orchestrator-worker runtime. It is a protocol and runtime boundary, not a general preference for delegation.

## Activation

- Treat skill activation as prompt loading.
- If the user only asks to enter Swarm mode, acknowledge readiness briefly and wait for a concrete task.
- If the same message includes a concrete task, start orchestrating immediately.
- Stay within same-provider orchestration for now:
  - Codex orchestrators control Codex workers with `--engine codex`.
  - Claude Code orchestrators control Claude Code workers with `--engine claude`.
- Do not silently replace Swarm semantics with ad hoc tmux windows.

## Runtime

- Canonical CLI: `python3 ~/.agents/skills/swarm/scripts/swarm.py`
- Default runtime root: `/tmp/agent-swarm`
- Override runtime root with `SWARM_RUNTIME_ROOT`.
- Topic is the current tmux session name.
- The orchestrator window is named `orchestrator` or `orchestrator-*`.
- Worker windows use the explicit name passed with `--name`.

## Commands

Use the canonical CLI with an explicit engine:

```bash
python3 ~/.agents/skills/swarm/scripts/swarm.py --engine codex status
python3 ~/.agents/skills/swarm/scripts/swarm.py --engine claude status
```

Public verbs:

- `spawn`
- `send`
- `kill`
- `status`
- `show`
- `tail`
- `note`
- `ping`
- `supervise`

Internal verbs:

- `worker-loop`
- `stop-hook`

Do not use `session-start`, `report-issue`, or `NOTIFY HUMAN`; they are not part of the current Swarm surface.

Full command shapes and shell-safety rules live in `references/swarm-cli.md`.

## Orchestrator Duties

- Break user work into clear, bounded sub-tasks.
- Spawn or reuse workers only when parallelism helps.
- Review worker reports and hand-off artifacts before accepting results.
- Keep the user updated on worker assignment, blockers, and completion.
- If a timer tick arrives, decide whether to inspect status, review workers, continue the plan, or ignore it because useful work is already running.

For Codex-specific orchestration details, read `references/codex-orchestrator.md`.
For Claude-specific orchestration details, read `references/claude-orchestrator.md`.
For Claude worker expectations, read `references/claude-worker.md`.

## Human Escalation

Remote-control transports such as the Feishu bridge belong to Everywhere, not to Swarm runtime. Swarm can still page the human by explicitly invoking the bridge when available.

For a short escalation from a repo that contains `.everywhere`:

```bash
everywhere feishu notify --message "<summary and decision needed>"
```

If no binding exists and remote-control escalation is appropriate:

```bash
everywhere feishu attach
```

For a long handoff, write Markdown first and send:

```bash
everywhere feishu notify --message-file <path>
```

Escalation messages should include the topic, blocker or decision needed, options considered, and exact human input needed next.

## Self-Improving

When you discover a defect in Swarm rules, runtime, installer, or provider integration, follow `references/self-improving.md`.

Do not call a Swarm runtime issue-reporting command; no such command is supported. The agent writes the issue archive and index directly.

## What Not To Do

- Do not create standalone tmux sessions as a substitute for workers.
- Do not bypass `swarm.py` for spawn, send, kill, or status when `swarm.py` is available.
- Do not treat a worker self-report as proof of task completion.
- Do not use magic output strings to page humans.
- Do not rely on provider-local runtimes under `~/.codex/swarm` or `~/.claude/swarm`.
