# Codex Orchestrator Reference

Use this reference when Codex is the Swarm orchestrator.

## Runtime Assumptions

- Engine: `codex`
- Canonical CLI: `python3 ~/.agents/skills/swarm/scripts/swarm.py --engine codex`
- Codex workers are round-based:
  - `spawn` starts a persistent tmux window running `worker-loop`
  - `send` enqueues one headless `codex exec` round
  - completion is reported back as `[worker-x] TASK_DONE ...`
- Worker hand-offs live under `/tmp/agent-swarm/topics/<topic>/workers/<worker>/handoff.md`.

## Required Behavior

- Before spawning or reusing workers for a real task, check current state with `status` unless the user explicitly asked not to.
- Use `send` for an existing worker and `spawn` only when a new worker is justified.
- Review the latest hand-off before accepting a result or assigning a follow-up.
- Treat `status=ok` as "the execution round completed", not as proof that the work is done.
- If a hand-off is vague, overconfident, or prematurely final, send a corrective follow-up.
- Verify that code-change hand-offs match the actual repo diff.

## Worker Management

- Typical target is 2-3 reusable workers.
- Use more only when work is truly parallel and reviewable.
- Prefer descriptive names such as `worker-ble`, `worker-ui`, or `worker-tests`.
- For fragile hardware exploration, prefer `send --fresh`.
- Never have a worker run `kill`, `send --fresh`, or `spawn --replace` against its own window from inside its active round.

## Hardware Work

For single-device hardware tasks:

- require serialized access to the physical device
- forbid overlapping serial, flash, probe, or board commands
- treat `port in use` and serial-busy failures as likely orchestration mistakes first
- require workers to state the board baseline before making causal claims
- require sticky status bits and retained power state to be considered for PMIC / IRQ / wake work

## Lazy-Worker Signals

- claims success without evidence
- summarizes intent instead of verified results
- quietly narrows the task to something easier
- stops at the first blocker without attempting a reasonable next step
- updates docs without materially advancing the underlying task

## Useful Commands

```bash
python3 ~/.agents/skills/swarm/scripts/swarm.py --engine codex status
python3 ~/.agents/skills/swarm/scripts/swarm.py --engine codex show --name <worker-name>
python3 ~/.agents/skills/swarm/scripts/swarm.py --engine codex tail --name <worker-name> stderr
python3 ~/.agents/skills/swarm/scripts/swarm.py --engine codex tail --name <worker-name> last_message
python3 ~/.agents/skills/swarm/scripts/swarm.py --engine codex note --name <worker-name> "<what this round taught us>"
```

## Timer Ticks

Human may run `supervise` in a nearby pane. Timer messages are prompts to review workers, hand-offs, TODOs, and the current plan. If useful work is already running, it is acceptable to ignore the tick.
