# Swarm Self-Improving

Use this reference when you discover a defect in Swarm's rules, runtime, installer, or provider integration.

This is not a separate skill and not a tmux role. The agent that discovers the defect writes the archive directly.

## Archive

Component: `swarm`

Issue directory:

```text
~/.agents/self-improving/issues/swarm/
```

Index:

```text
~/.agents/self-improving/index.jsonl
```

## Process

1. Read the relevant Swarm docs or code before deciding the rule is missing.
2. Write a focused issue file under `~/.agents/self-improving/issues/swarm/`.
3. Append one JSON line to `~/.agents/self-improving/index.jsonl`.
4. If human attention is useful, notify through Everywhere using topic `self-improving`.
5. Keep fixes minimal and localized.

## Issue File Format

Use a filename like:

```text
YYYY-MM-DD-short-slug.md
```

Template:

```markdown
---
date: YYYY-MM-DD
component: swarm
severity: low | medium | high
status: open | fixed
source: human | agent | runtime-log
---

## Symptom

What happened, where it happened, and how it was observed.

## Evidence

Relevant excerpts, file paths, command output, logs, or behavior descriptions.

## Root Cause

Which rule, runtime behavior, installer behavior, or provider integration is missing, ambiguous, or wrong.

## Proposed Fix

The smallest change that would prevent the issue from recurring.
```

Index JSONL shape:

```json
{"date":"YYYY-MM-DD","component":"swarm","severity":"medium","status":"open","path":"~/.agents/self-improving/issues/swarm/YYYY-MM-DD-short-slug.md","summary":"one-line summary"}
```

## Feishu Notification

If remote control is available from a repo with `.everywhere`, send a concise human-facing summary:

```bash
.everywhere/bin/feishu-bridge notify --message "[self-improving] <summary>"
```

For long reports, use `--message-file`.
