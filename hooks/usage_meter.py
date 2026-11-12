"""Codex UserPromptSubmit hook: injects a context/quota usage line read from the session rollout."""
from __future__ import annotations

import json
import math
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

CHUNK_SIZE = 1 << 20
SCAN_BUDGET = 16 << 20
ERROR_LINE = "ctx unknown (usage error)"

CTX_BANDS = (
    (75, "stop at a clean point, then /compact or /new"),
    (50, "/compact after the current task"),
    (25, "protect: delegate reads, tighten output caps"),
)
QUOTA_BANDS = (
    (90, "orchestrator only, delegate execution"),
    (70, "prefer engines on another pool"),
)
WINDOW_LABELS = {300: "5h", 10080: "wk"}
CTX_BAND_VALUES = (0, *(t for t, _ in CTX_BANDS))
QUOTA_BAND_VALUES = (0, *(t for t, _ in QUOTA_BANDS))


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def valid_info(info: Any) -> bool:
    if not isinstance(info, dict) or not isinstance(info.get("last_token_usage"), dict):
        return False
    tokens, window = info["last_token_usage"].get("total_tokens"), info.get("model_context_window")
    return is_int(tokens) and tokens >= 0 and is_int(window) and window > 0


def valid_window(window: Any) -> bool:
    return (
        isinstance(window, dict)
        and is_finite_number(window.get("used_percent"))
        and is_int(window.get("window_minutes"))
        and window["window_minutes"] > 0
    )


def scan_rollout(
    path: str, chunk_size: int = CHUNK_SIZE, budget: int = SCAN_BUDGET,
) -> tuple[dict | None, dict | None]:
    """Return the newest valid (info, rate_limits) of token_count events, reading at most `budget` bytes back."""
    info: dict | None = None
    limits: dict | None = None
    with open(path, "rb") as f:
        pos = f.seek(0, os.SEEK_END)
        floor = max(0, pos - budget - 1)
        carry = b""
        while pos > floor and (info is None or limits is None):
            size = min(chunk_size, pos - floor)
            pos -= size
            f.seek(pos)
            parts = (f.read(size) + carry).split(b"\n")
            # The first part may be the tail of a line that began in an earlier chunk.
            carry = parts[0] if pos > 0 else b""
            lines = parts[1:] if pos > 0 else parts
            for raw in reversed(lines):
                if b'"token_count"' not in raw:
                    continue
                try:
                    event = json.loads(raw)
                    payload = event["payload"]
                    if event["type"] != "event_msg" or payload["type"] != "token_count":
                        continue
                except (ValueError, KeyError, TypeError, RecursionError):
                    continue
                if info is None and valid_info(payload.get("info")):
                    info = payload["info"]
                if limits is None and isinstance(payload.get("rate_limits"), dict):
                    limits = payload["rate_limits"]
                if info is not None and limits is not None:
                    break
    return info, limits


def format_reset(resets_at: Any, now: float) -> str:
    if not is_finite_number(resets_at):
        return ""
    left = resets_at - now
    if left <= 0:
        return " (reset: passed)"
    minutes = round(left / 60)
    days, hours, mins = minutes // 1440, (minutes % 1440) // 60, minutes % 60
    if days:
        return f" (reset: {days}d {hours}h)"
    if hours:
        return f" (reset: {hours}h {mins}m)"
    return f" (reset: {mins}m)"


def quota_label(window: dict) -> str:
    minutes = window["window_minutes"]
    return WINDOW_LABELS.get(minutes, f"{minutes}m")


def context_percent(info: dict) -> tuple[float, int, int]:
    tokens = info["last_token_usage"]["total_tokens"]
    window = info["model_context_window"]
    return min(tokens, window) / window * 100, tokens, window


def build_line(info: dict | None, limits: dict | None, now: float) -> tuple[str, float | None, dict[str, float]]:
    """Return the usage line, the context percent (None if unknown) and used_percent per quota label."""
    if info is None:
        ctx, pct = "ctx unknown", None
    else:
        pct, tokens, window = context_percent(info)
        ctx = f"ctx {pct:.1f}% ({tokens:,} / {window:,})"
    parts: list[str] = []
    quota: dict[str, float] = {}
    for key in ("primary", "secondary"):
        window_info = (limits or {}).get(key)
        if not valid_window(window_info):
            continue
        label = quota_label(window_info)
        used = window_info["used_percent"]
        quota[label] = used
        parts.append(f"{label} {round(used)}%{format_reset(window_info.get('resets_at'), now)}")
    return (f"{ctx} | {' '.join(parts)}" if parts else ctx), pct, quota


def band_of(value: float, bands: tuple[tuple[int, str], ...]) -> int:
    return next((threshold for threshold, _ in bands if value >= threshold), 0)


def advice_for(band: int, bands: tuple[tuple[int, str], ...]) -> str:
    return next(text for threshold, text in bands if threshold == band)


def state_path(session_id: str) -> Path:
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    return home / "codex-usage-meter" / "state" / (re.sub(r"[^A-Za-z0-9._-]", "_", session_id) + ".json")


def load_state(path: Path) -> dict:
    """Return {"ctx": band, "quota": {label: band}}; anything missing or invalid reads as band 0 / no labels."""
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = None
    state = state if isinstance(state, dict) else {}
    quota = state.get("quota") if isinstance(state.get("quota"), dict) else {}
    return {
        "ctx": state["ctx"] if state.get("ctx") in CTX_BAND_VALUES else 0,
        "quota": {label: band if band in QUOTA_BAND_VALUES else 0 for label, band in quota.items()},
    }


def save_state(path: Path, state: dict) -> None:
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, path)
    except OSError:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def crossed_messages(session_id: str, pct: float | None, quota: dict[str, float]) -> str:
    path = state_path(session_id)
    state = load_state(path)
    old_quota = state["quota"]
    new_quota = dict(old_quota)
    messages: list[str] = []

    if pct is not None:
        band = band_of(pct, CTX_BANDS)
        if band > state["ctx"]:
            messages.append(f"⚠ ctx {pct:.1f}% crossed — {advice_for(band, CTX_BANDS)}")
        state["ctx"] = band
    for label, used in quota.items():
        band = band_of(used, QUOTA_BANDS)
        if band > old_quota.get(label, 0):
            messages.append(f"⚠ Codex {label} {round(used)}% — {advice_for(band, QUOTA_BANDS)}")
        new_quota[label] = band
    state["quota"] = new_quota
    save_state(path, state)
    return "\n".join(messages)


def usage_output(line: str, message: str = "") -> dict:
    output: dict[str, Any] = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": line}}
    if message:
        output["systemMessage"] = message
    return output


def run(data: dict, now: float) -> dict:
    try:
        transcript = data.get("transcript_path")
        transcript = transcript if isinstance(transcript, str) else None
        info, limits = scan_rollout(transcript) if transcript else (None, None)
        line, pct, quota = build_line(info, limits, now)
    except Exception:  # a hook must never block the prompt, whatever the rollout contains
        return usage_output(ERROR_LINE)
    message = ""
    session_id = data.get("session_id")
    if session_id and "agent_id" not in data:
        try:
            message = crossed_messages(str(session_id), pct, quota)
        except Exception:
            message = ""
    return usage_output(line, message)


def main() -> int:
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
        output = run(data if isinstance(data, dict) else {}, time.time())
    except Exception:
        output = usage_output(ERROR_LINE)
    sys.stdout.write(json.dumps(output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
