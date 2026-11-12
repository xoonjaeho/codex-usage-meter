"""Codex usage hook, one script for two events.

UserPromptSubmit injects a context/quota usage line and may only lower stored bands. Stop emits the threshold
warnings and the prompt-cache lines. Any other event, or input without an event name, prints {}.
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

CHUNK_SIZE = 1 << 20
SCAN_BUDGET = 16 << 20
ERROR_LINE = "ctx unknown (usage error)"
CONFIG_NAME = "codex-usage-meter.json"
CONFIG_LIMIT = 64 << 10

WINDOW_LABELS = {300: "5h", 10080: "wk"}

DEFAULT_CONTEXT_WARNINGS = [
    {"at": 50, "message": "⚠ Context {pct}% — consider /compact after this task"},
    {"at": 75, "message": "⚠ Context {pct}% — /compact or start a new session soon"},
    {"at": 90, "message": "⚠ Context {pct}% — near the limit, /compact now"},
]
DEFAULT_QUOTA_WARNINGS = [
    {"at": 80, "message": "⚠ Codex {window} usage {pct}% — pace your usage (reset: {reset})"},
    {"at": 95, "message": "⚠ Codex {window} usage {pct}% — nearly used up (reset: {reset})"},
]
# OpenAI's documented default prompt_cache_options.ttl for GPT-5.6 and later; Codex sends no TTL of its own.
DEFAULT_CACHE_TTL_MINUTES = 30
MAX_CACHE_TTL_MINUTES = 1440
DEFAULT_CACHE_WARM_MESSAGE = "🧊 Prompt cache warm until {until} ({ttl}m TTL)"
DEFAULT_CACHE_COLD_MESSAGE = "🌊 This turn started on a cold prompt cache (idle {idle})"
CACHE_MARKERS = (b'"token_usage_record"', b'"task_started"', b'"compacted"')


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def valid_info(info: Any) -> bool:
    if not isinstance(info, dict) or not isinstance(info.get("last_token_usage"), dict):
        return False
    tokens, window = info["last_token_usage"].get("total_tokens"), info.get("model_context_window")
    return is_int(tokens) and tokens >= 0 and is_int(window) and window > 0


def check_bool(value: Any) -> str | None:
    return None if isinstance(value, bool) else "must be true or false"


def check_warnings(value: Any) -> str | None:
    if not isinstance(value, list) or not value:
        return "must be a non-empty list of {at, message} objects"
    previous = 0
    for entry in value:
        if not isinstance(entry, dict):
            return "every entry must be an object"
        at = entry.get("at")
        if not is_finite_number(at) or not 1 <= at <= 100:
            return "at must be a number from 1 to 100"
        if not isinstance(entry.get("message"), str) or not entry["message"].strip():
            return "message must be a non-empty string"
        if at <= previous:
            return "at values must be strictly ascending"
        previous = at
    return None


def check_message(value: Any) -> str | None:
    return None if isinstance(value, str) and value.strip() else "must be a non-empty string"


def check_ttl(value: Any) -> str | None:
    if is_finite_number(value) and 0 < value <= MAX_CACHE_TTL_MINUTES:
        return None
    return f"must be a number above 0 and at most {MAX_CACHE_TTL_MINUTES}"


# One row per setting: (default, validator returning a reason or None). A rejected key falls back to its default.
CONFIG_KEYS: dict[str, tuple[Any, Callable[[Any], str | None]]] = {
    "warnings": (True, check_bool),
    "context_warnings": (DEFAULT_CONTEXT_WARNINGS, check_warnings),
    "quota_warnings": (DEFAULT_QUOTA_WARNINGS, check_warnings),
    "cache_notices": (True, check_bool),
    "cache_ttl_minutes": (DEFAULT_CACHE_TTL_MINUTES, check_ttl),
    "cache_warm_message": (DEFAULT_CACHE_WARM_MESSAGE, check_message),
    "cache_cold_message": (DEFAULT_CACHE_COLD_MESSAGE, check_message),
}


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def read_config_file() -> tuple[dict, str | None]:
    """Return (raw settings, reason the whole file was ignored); a missing or unreadable file is not a problem."""
    try:
        with open(codex_home() / CONFIG_NAME, "rb") as f:
            data = f.read(CONFIG_LIMIT + 1)
    except OSError:
        return {}, None
    if len(data) > CONFIG_LIMIT:
        return {}, "larger than 64 KiB"
    try:
        raw = json.loads(data.decode("utf-8-sig"))
    except (ValueError, RecursionError):
        return {}, "not valid JSON"
    if not isinstance(raw, dict):
        return {}, "not a JSON object"
    return raw, None


def default_config() -> dict[str, Any]:
    return {**{key: default for key, (default, _) in CONFIG_KEYS.items()}, "problems": []}


def load_config() -> dict[str, Any]:
    """Settings with defaults applied; "problems" lists the notices for rejected keys. Never raises."""
    try:
        config = default_config()
        raw, file_problem = read_config_file()
        problems = [("config file", file_problem)] if file_problem else []
        for key, (_, validate) in CONFIG_KEYS.items():
            if key not in raw:
                continue
            reason = validate(raw[key])
            if reason:
                problems.append((key, reason))
            else:
                config[key] = raw[key]
        config["problems"] = [f"{CONFIG_NAME}: {key} ignored ({reason}) — using defaults" for key, reason in problems]
        return config
    except Exception:
        return default_config()


def valid_window(window: Any) -> bool:
    return (
        isinstance(window, dict)
        and is_finite_number(window.get("used_percent"))
        and is_int(window.get("window_minutes"))
        and window["window_minutes"] > 0
    )


def reversed_lines(path: str, chunk_size: int = CHUNK_SIZE, budget: int = SCAN_BUDGET) -> Iterator[bytes]:
    """Yield the file's lines newest first, reading at most `budget` bytes back."""
    with open(path, "rb") as f:
        pos = f.seek(0, os.SEEK_END)
        floor = max(0, pos - budget - 1)
        carry = b""
        while pos > floor:
            size = min(chunk_size, pos - floor)
            pos -= size
            f.seek(pos)
            parts = (f.read(size) + carry).split(b"\n")
            # The first part may be the tail of a line that began in an earlier chunk.
            carry = parts[0] if pos > 0 else b""
            yield from reversed(parts[1:] if pos > 0 else parts)


def scan_rollout(
    path: str, chunk_size: int = CHUNK_SIZE, budget: int = SCAN_BUDGET,
) -> tuple[dict | None, dict | None]:
    """Return the newest valid (info, rate_limits) of token_count events, reading at most `budget` bytes back."""
    info: dict | None = None
    limits: dict | None = None
    for raw in reversed_lines(path, chunk_size, budget):
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


def parse_time(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def valid_usage(usage: Any) -> bool:
    return isinstance(usage, dict) and all(
        is_int(usage.get(key)) and usage[key] >= 0 for key in ("input_tokens", "cached_input_tokens")
    )


def cold_idle(
    first: tuple[float, int, int], started: float, previous: tuple[float, int], compactions: list[float],
) -> float | None:
    """Idle seconds before the turn if its first request reused under half of the previous request's input, else None.

    A compaction between the two requests rewrites the prefix, so a miss there says nothing about the TTL. A prompt
    shorter than the previous one (e.g. after a rollback) is measured against its own size instead.
    """
    (first_at, cached, own_input), (previous_at, previous_input) = first, previous
    if cached * 2 >= min(previous_input, own_input):
        return None
    if any(previous_at <= at <= first_at for at in compactions):
        return None
    return max(0.0, started - previous_at)


def scan_cache(
    path: str, chunk_size: int = CHUNK_SIZE, budget: int = SCAN_BUDGET,
) -> tuple[float | None, float | None] | None:
    """Return (warm_from, idle), or None when no request is recorded.

    warm_from is the time of the newest model request, or None when a compaction followed it: the next request sends
    the rewritten prefix, which that request never cached. idle is cold_idle of the newest request's turn, and None
    unless that turn is also the newest one started.
    """
    newest: float | None = None
    turn: Any = None
    first: tuple[float, int, int] | None = None
    latest_start: tuple[Any, float] | None = None
    compactions: list[float] = []
    for raw in reversed_lines(path, chunk_size, budget):
        if not any(marker in raw for marker in CACHE_MARKERS):
            continue
        try:
            record = json.loads(raw)
            kind, payload, at = record["type"], record["payload"], parse_time(record["timestamp"])
        except (ValueError, KeyError, TypeError, RecursionError):
            continue
        if at is None or not isinstance(payload, dict):
            continue
        if kind == "compacted":
            compactions.append(at)
        elif kind == "event_msg" and payload.get("type") == "task_started":
            if latest_start is None:
                latest_start = (payload.get("turn_id"), at)
        elif kind == "token_usage_record" and valid_usage(payload.get("usage")):
            usage = payload["usage"]
            if newest is None:
                newest, turn = at, payload.get("turn_id")
            if payload.get("turn_id") == turn:
                first = (at, usage["cached_input_tokens"], usage["input_tokens"])
                continue
            idle = None
            if latest_start is not None and latest_start[0] == turn:
                idle = cold_idle(first, latest_start[1], (at, usage["input_tokens"]), compactions)
            return warm_from(newest, compactions), idle
    return None if newest is None else (warm_from(newest, compactions), None)


def warm_from(newest: float, compactions: list[float]) -> float | None:
    return None if any(at >= newest for at in compactions) else newest


def clock_text(epoch: float, now: float) -> str:
    stamp = time.localtime(epoch)
    return time.strftime("%H:%M" if stamp[:3] == time.localtime(now)[:3] else "%m-%d %H:%M", stamp)


def reset_text(resets_at: Any, now: float) -> str | None:
    if not is_finite_number(resets_at):
        return None
    left = resets_at - now
    return "passed" if left <= 0 else duration_text(left)


def duration_text(seconds: float) -> str:
    minutes = round(seconds / 60)
    days, hours, mins = minutes // 1440, (minutes % 1440) // 60, minutes % 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def format_reset(resets_at: Any, now: float) -> str:
    text = reset_text(resets_at, now)
    return f" (reset: {text})" if text else ""


def quota_label(window: dict) -> str:
    minutes = window["window_minutes"]
    return WINDOW_LABELS.get(minutes, f"{minutes}m")


def context_percent(info: dict) -> tuple[float, int, int]:
    tokens = info["last_token_usage"]["total_tokens"]
    window = info["model_context_window"]
    return min(tokens, window) / window * 100, tokens, window


Quota = dict[str, tuple[float, str]]


def build_line(info: dict | None, limits: dict | None, now: float) -> tuple[str, float | None, Quota]:
    """Return the usage line, the context percent (None if unknown) and (used_percent, reset text) per label."""
    if info is None:
        ctx, pct = "ctx unknown", None
    else:
        pct, tokens, window = context_percent(info)
        ctx = f"ctx {pct:.1f}% ({tokens:,} / {window:,})"
    parts: list[str] = []
    quota: Quota = {}
    for key in ("primary", "secondary"):
        window_info = (limits or {}).get(key)
        if not valid_window(window_info):
            continue
        label = quota_label(window_info)
        used = window_info["used_percent"]
        quota[label] = (used, reset_text(window_info.get("resets_at"), now) or "unknown")
        parts.append(f"{label} {round(used)}%{format_reset(window_info.get('resets_at'), now)}")
    return (f"{ctx} | {' '.join(parts)}" if parts else ctx), pct, quota


def band_of(value: float, thresholds: list) -> float:
    return max((t for t in thresholds if value >= t), default=0)


def thresholds_of(warnings: list[dict]) -> list:
    return [w["at"] for w in warnings]


def message_for(band: float, warnings: list[dict]) -> str:
    return next(w["message"] for w in warnings if w["at"] == band)


def format_band(band: float) -> str:
    return f"{band:.10g}"


def render(template: str, values: dict[str, str]) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: values.get(m.group(1), m.group(0)), template)


def state_path(session_id: str) -> Path:
    return codex_home() / "codex-usage-meter" / "state" / (re.sub(r"[^A-Za-z0-9._-]", "_", session_id) + ".json")


def stored_band(value: Any, thresholds: list) -> float:
    """Map a stored band onto the current configuration: the highest configured band not above it, else 0."""
    return band_of(value, thresholds) if is_finite_number(value) else 0


def load_state(path: Path, config: dict[str, Any]) -> dict:
    """Return {"ctx": band, "quota": {label: band}, "announced": [notice]}; invalid parts read as empty."""
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        state = None
    state = state if isinstance(state, dict) else {}
    quota = state.get("quota") if isinstance(state.get("quota"), dict) else {}
    announced = state.get("announced") if isinstance(state.get("announced"), list) else []
    ctx_thresholds = thresholds_of(config["context_warnings"])
    quota_thresholds = thresholds_of(config["quota_warnings"])
    return {
        "ctx": stored_band(state.get("ctx"), ctx_thresholds),
        "quota": {label: stored_band(band, quota_thresholds) for label, band in quota.items()},
        "announced": [a for a in announced if isinstance(a, str)],
    }


def save_state(path: Path, state: dict) -> None:
    content = {"ctx": state["ctx"], "quota": state["quota"]}
    if state["announced"]:
        content["announced"] = state["announced"]
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(content, f)
        os.replace(tmp, path)
    except OSError:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def announce_problems(state: dict, problems: list[str]) -> list[str]:
    """Return the config notices not yet announced in this session, and record the current set."""
    current = sorted(problems)
    fresh = list(current) if current != state["announced"] else []
    state["announced"] = current
    return fresh


def crossed_messages(state: dict, pct: float | None, quota: Quota, config: dict[str, Any]) -> list[str]:
    messages: list[str] = []
    if pct is not None:
        warnings = config["context_warnings"]
        band = band_of(pct, thresholds_of(warnings))
        if band > state["ctx"]:
            values = {"pct": f"{pct:.1f}", "band": format_band(band)}
            messages.append(render(message_for(band, warnings), values))
        state["ctx"] = band
    for label, (used, reset) in quota.items():
        warnings = config["quota_warnings"]
        band = band_of(used, thresholds_of(warnings))
        if band > state["quota"].get(label, 0):
            values = {"pct": str(round(used)), "band": format_band(band), "window": label, "reset": reset}
            messages.append(render(message_for(band, warnings), values))
        state["quota"][label] = band
    return messages


def stop_message(session_id: str, pct: float | None, quota: Quota, config: dict[str, Any]) -> str:
    path = state_path(session_id)
    state = load_state(path, config)
    messages = announce_problems(state, config["problems"])
    if config["warnings"]:
        messages += crossed_messages(state, pct, quota, config)
    save_state(path, state)
    return "\n".join(messages)


def lower_bands(session_id: str, pct: float | None, quota: Quota, config: dict[str, Any]) -> None:
    """Re-arm warnings after a drop (e.g. a compaction, which may not fire Stop); never raises a stored band."""
    path = state_path(session_id)
    state = load_state(path, config)
    changed = False
    if pct is not None and band_of(pct, thresholds_of(config["context_warnings"])) < state["ctx"]:
        state["ctx"] = band_of(pct, thresholds_of(config["context_warnings"]))
        changed = True
    for label, (used, _) in quota.items():
        if band_of(used, thresholds_of(config["quota_warnings"])) < state["quota"].get(label, 0):
            state["quota"][label] = band_of(used, thresholds_of(config["quota_warnings"]))
            changed = True
    if changed:
        save_state(path, state)


def read_usage(data: dict, now: float) -> tuple[str, float | None, Quota]:
    transcript = data.get("transcript_path")
    transcript = transcript if isinstance(transcript, str) else None
    info, limits = scan_rollout(transcript) if transcript else (None, None)
    return build_line(info, limits, now)


def prompt_output(data: dict, now: float) -> dict:
    try:
        line, pct, quota = read_usage(data, now)
    except Exception:  # a hook must never block the prompt, whatever the rollout contains
        line, pct, quota = ERROR_LINE, None, {}
    session_id = data.get("session_id")
    if session_id and "agent_id" not in data:
        try:
            lower_bands(str(session_id), pct, quota, load_config())
        except Exception:
            pass
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": line}}


def clear_announced(session_id: str, config: dict[str, Any]) -> None:
    """Forget announced notices once the problems are gone, so a recurrence is announced again."""
    path = state_path(session_id)
    state = load_state(path, config)
    if state["announced"]:
        state["announced"] = []
        save_state(path, state)


def cache_messages(data: dict, now: float, config: dict[str, Any]) -> list[str]:
    transcript = data.get("transcript_path")
    found = scan_cache(transcript) if isinstance(transcript, str) and transcript else None
    if found is None:
        return []
    newest, idle = found
    ttl = config["cache_ttl_minutes"]
    messages = []
    # A Stop re-fired inside the same turn (stop_hook_active) already showed this turn's cold line.
    if idle is not None and data.get("stop_hook_active") is not True:
        idle_text = "<1m" if idle < 30 else duration_text(idle)
        messages.append(render(config["cache_cold_message"], {"idle": idle_text, "ttl": f"{ttl:.10g}"}))
    if newest is not None and newest + ttl * 60 > now:
        until_text = clock_text(newest + ttl * 60, now)
        messages.append(render(config["cache_warm_message"], {"until": until_text, "ttl": f"{ttl:.10g}"}))
    return messages


def stop_output(data: dict, now: float) -> dict:
    """Stop output may only ever carry systemMessage: any decision field would restart the turn."""
    session_id = data.get("session_id")
    if not session_id or "agent_id" in data:
        return {}
    config = load_config()
    pct, quota = None, {}
    if config["warnings"]:
        try:
            _, pct, quota = read_usage(data, now)
        except Exception:  # an unreadable rollout must not hide the config notices
            pass
    cache_lines: list[str] = []
    if config["cache_notices"]:
        try:
            cache_lines = cache_messages(data, now, config)
        except Exception:
            pass
    if pct is None and not quota and not config["problems"]:
        clear_announced(str(session_id), config)
        message = ""
    else:
        message = stop_message(str(session_id), pct, quota, config)
    message = "\n".join(filter(None, [message, *cache_lines]))
    return {"systemMessage": message} if message else {}


def run(data: dict, now: float) -> dict:
    event = data.get("hook_event_name")
    if event == "UserPromptSubmit":
        return prompt_output(data, now)
    if event == "Stop":
        return stop_output(data, now)
    return {}


def main() -> int:
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
        output = run(data, time.time()) if isinstance(data, dict) else {}
    except Exception:
        output = {}
    sys.stdout.write(json.dumps(output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
