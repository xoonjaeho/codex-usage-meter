# codex-usage-meter

A Codex command hook with two registrations of one script. On `UserPromptSubmit` it injects a usage line into the
model's context (`additionalContext`). On `Stop`, when a threshold band was crossed upward, it shows a one-off
`↳ Hook · ...` message to the human (`systemMessage`). Codex CLI, TUI and the Desktop app all read
`~/.codex/hooks.json`, so one install covers all.

It is the Codex counterpart of the Claude Code mod `claude-usage-meter`. Stdlib only, Python 3.10+.

## Line format

```
ctx 19.5% (92,684 / 475,000) | 5h 12% (reset: 2h 10m) wk 74% (reset: 1d 17h)
```

- `ctx p% (tokens / window)`: `last_token_usage.total_tokens` over `model_context_window`, capped at 100%.
  Same formula as the Codex Desktop composer indicator. `ctx unknown` when no figures exist yet.
- Quota windows (`primary`, `secondary`): `5h` (300 min), `wk` (10080 min), otherwise `{N}m`; each with a reset
  countdown (`2h 10m`, `1d 17h`, `passed`).
- Any unexpected error on `UserPromptSubmit` yields `ctx unknown (usage error)`. As long as stdout is writable,
  every path prints one valid JSON object and exits 0 (a closed stdout cannot be reported). `Stop` failures, unknown
  events, input without `hook_event_name` and unparseable input print `{}`.

Figures come from the newest `token_count` events in the session rollout (`transcript_path`), scanned backwards in
chunks, reading at most the last 16 MiB of the rollout (a side not found there reads as unknown or is omitted).

## Threshold messages

Shown at `Stop`, right after the turn ends, so `/compact` or `/new` can be chosen before the next prompt. The TUI
shows them as `↳ Hook · ...` under the reply. `UserPromptSubmit` never emits a
message; it only lowers a stored band, so a drop seen at the next prompt (e.g. after `/compact`, which may not fire
`Stop`) re-arms the warning. Shown once per upward crossing, per session
(state in `{CODEX_HOME or ~/.codex}/codex-usage-meter/state/`):

| Source | Bands |
|---|---|
| context | 25, 50, 75 |
| each quota window | 70, 90 |

Input with `agent_id` or without `session_id` gets no message and no state.

## Install

Add to `~/.codex/hooks.json`:

```json
"UserPromptSubmit": [
  {"hooks": [{"type": "command", "command": "<python> <path-to>/codex-usage-meter/hooks/usage_meter.py"}]}
],
"Stop": [
  {"hooks": [{"type": "command", "command": "<python> <path-to>/codex-usage-meter/hooks/usage_meter.py"}]}
]
```

Without the `Stop` entry no warnings appear, only the line.

Use the absolute interpreter path: the Desktop app is a Store app and its PATH can differ.

Then trust each registration once (they are trusted separately) via `/hooks` in the TUI, or Settings -> Hooks in the
Desktop app. Changing the command string changes the trust hash and needs re-trusting; editing the script does not.

## Limits

- On `UserPromptSubmit`, figures are the snapshot from the last completed turn, not the current prompt. On `Stop`
  they are those of the turn that just ended (its `token_count` is written before `Stop` fires).
- `codex exec` does not print the warning text, only `hook: Stop Completed`. Desktop display of the message is
  unverified.
- The first prompt of a session reads `ctx unknown` (no `token_count` yet).
- The TUI status line's `Context N% used` reads lower because codex's TUI subtracts a 12,000-token baseline. The
  Desktop composer indicator (Settings -> General -> "Show context window usage") uses the same formula as this line.

## Tests

```
python -m pytest
```

