# Swarm CLI Reference

Canonical CLI:

- `python3 ~/.agents/skills/swarm/scripts/swarm.py --engine codex`

Primary verbs:

- `spawn`
- `send`
- `kill`
- `status`
- `show`
- `tail`
- `note`
- `ping`
- `supervise`

Preferred forms:

- short inline task:
  - `python3 ~/.agents/skills/swarm/scripts/swarm.py --engine codex send --name worker-x --cwd /repo --message "do X and report Y"`
- fresh round for fragile hardware work:
  - `python3 ~/.agents/skills/swarm/scripts/swarm.py --engine codex send --name worker-x --fresh --cwd /repo --message "do X with serialized board access"`
- long or audit-worthy task:
  - `python3 ~/.agents/skills/swarm/scripts/swarm.py --engine codex send --name worker-x --fresh --cwd /repo --prompt-file /tmp/worker-x-task.txt`
- new worker:
  - `python3 ~/.agents/skills/swarm/scripts/swarm.py --engine codex spawn --name worker-x --cwd /repo --message "do X"`
- replace an existing worker window:
  - `python3 ~/.agents/skills/swarm/scripts/swarm.py --engine codex spawn --name worker-x --replace --cwd /repo --message "do X"`
- inspect artifacts:
  - `python3 ~/.agents/skills/swarm/scripts/swarm.py --engine codex show --name worker-x`
  - `python3 ~/.agents/skills/swarm/scripts/swarm.py --engine codex tail --name worker-x raw_message`

Task text rules:

- prefer `--message` for short inline tasks
- prefer `--prompt-file` for long prompts, punctuation-heavy prompts, or prompts that need later auditability
- pass worker names with `--name`; the new CLI does not rely on positional worker/task arguments
- do not mix inline task text with `--prompt-file`
- if you are unsure about argument order, run `python3 ~/.agents/skills/swarm/scripts/swarm.py --engine codex send -h` or `spawn -h`

Runtime facts:

- default runtime root is `/tmp/agent-swarm`
- topic is the current tmux session name
- worker hand-offs live under `/tmp/agent-swarm/topics/<topic>/workers/<worker>/handoff.md`
- full task/result messages are persisted before tmux receives a short reference or notification
- tmux inline Worker -> Orchestrator messages are capped at 1000 characters; longer messages are reported by artifact path

Timer supervision:

- run this in a pane inside the target tmux session:
  - `python3 ~/.agents/skills/swarm/scripts/swarm.py --engine codex supervise --interval-minutes 60`
- `supervise` sleeps first, then sends timer ticks forever
- timer ticks go directly to `orchestrator` / `orchestrator-*`
- it does not inspect worker state, write artifacts, or prevent duplicate timers

Internal commands:

- `worker-loop` is used by Codex worker windows
- `stop-hook` is used by Claude Code Stop hooks
- normal agents should not call internal commands directly

Shell safety:

- treat the task text as input that must survive local shell parsing unchanged
- do not include Markdown code spans or shell-active syntax such as backticks, `$()`, unescaped `$VAR`, pipes, redirects, or command chains unless you intentionally escaped them for the local shell
- prefer plain-language task text such as `run brew update`
- if the literal task text cannot be made shell-safe, stop and use `--prompt-file`

Known failure mode:

- a long trailing quoted string after options is easy to mistype from memory
- backticks inside the outer shell command may execute locally before `swarm.py` receives the argument
- that can create real orchestrator-side effects while the worker receives a truncated or corrupted task
