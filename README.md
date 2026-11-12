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
(state in `{CODEX_HOME or ~/.codex}/codex-usage-meter/state/`).

Default bands: context 50, 75, 90; each quota window 80, 95. See Configure to change them and the texts.

Input with `agent_id` or without `session_id` gets no message and no state.

## Configure

Optional file `{CODEX_HOME or ~/.codex}/codex-usage-meter.json`, one JSON object, UTF-8 (a BOM is accepted), at most
64 KiB. A missing file or key means the default. Unknown keys are ignored. All values below are the defaults:

```json
{
  "warnings": true,
  "context_warnings": [
    {"at": 50, "message": "⚠ Context {pct}% — consider /compact after this task"},
    {"at": 75, "message": "⚠ Context {pct}% — /compact or start a new session soon"},
    {"at": 90, "message": "⚠ Context {pct}% — near the limit, /compact now"}
  ],
  "quota_warnings": [
    {"at": 80, "message": "⚠ Codex {window} usage {pct}% — pace your usage (reset: {reset})"},
    {"at": 95, "message": "⚠ Codex {window} usage {pct}% — nearly used up (reset: {reset})"}
  ]
}
```

- `warnings`: `false` makes `Stop` skip the band warnings and leave band state alone. The usage line is still
  injected, and config problems are still announced.
- `context_warnings` / `quota_warnings`: a non-empty list of `{"at", "message"}` objects. `at` is a number from 1 to
  100 (percent, not boolean) and strictly ascending; `message` is a non-empty string. A message fires when the figure
  first reaches its `at`.
- Placeholders: `{pct}` (context with one decimal, quota rounded), `{band}` (the `at` that fired), and for quota
  messages `{window}` (`5h`, `wk` or `{N}m`) and `{reset}` (the countdown shown in the line, e.g. `2h 10m`, `1d 17h`,
  `passed`, or `unknown`). Unknown placeholders stay as written; context messages only know `{pct}` and `{band}`.

Fallback rules: an invalid key falls back to its default and never breaks the line. A file that is larger than
64 KiB, not valid JSON or not an object means all defaults; an unreadable file means all defaults without a notice.
When a key (or the whole file) is rejected, `Stop` prepends one line per problem to its message, for example
`codex-usage-meter.json: context_warnings ignored (at must be a number from 1 to 100) — using defaults`. It is shown
once per session and again only if the set of problems changes. A stored band that is no longer configured reads as
the highest configured band not above it, or 0.

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

