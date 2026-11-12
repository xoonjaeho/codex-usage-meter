from __future__ import annotations

import copy
import importlib.util
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "usage_meter.py"
NOW = 1794473386  # primary resets_at in the captured line minus 2h 10m

CAPTURED = (
    '{"timestamp":"2026-11-12T06:07:19.342Z","ordinal":84,"type":"event_msg","payload":{"type":"token_count",'
    '"info":{"total_token_usage":{"input_tokens":599884,"cached_input_tokens":524288,"cache_write_input_tokens":0,'
    '"output_tokens":5044,"reasoning_output_tokens":943,"total_tokens":604928},"last_token_usage":{"input_tokens":92268,'
    '"cached_input_tokens":86528,"cache_write_input_tokens":0,"output_tokens":416,"reasoning_output_tokens":194,'
    '"total_tokens":92684},"model_context_window":475000},"rate_limits":{"limit_id":"codex","limit_name":null,'
    '"primary":{"used_percent":12.0,"window_minutes":300,"resets_at":1794481186},"secondary":{"used_percent":74.0,'
    '"window_minutes":10080,"resets_at":1794624083},"credits":{"has_credits":false,"unlimited":false,"balance":"0"},'
    '"individual_limit":null,"spend_control_reached":null,"plan_type":"plus","rate_limit_reached_type":null}}}'
)
CAPTURED_LINE = "ctx 19.5% (92,684 / 475,000) | 5h 12% (reset: 2h 10m) wk 74% (reset: 1d 17h)"
CAPTURED_NORM = "ctx 19.5% (92,684 / 475,000) | 5h 12% (reset: X) wk 74% (reset: X)"
WK_MESSAGE = "quota warn wk 74"


LEGACY_CONFIG = {
    "context_warnings": [
        {"at": 25, "message": "ctx low {pct}"},
        {"at": 50, "message": "ctx mid {pct}"},
        {"at": 75, "message": "ctx high {pct}"},
    ],
    "quota_warnings": [
        {"at": 70, "message": "quota warn {window} {pct}"},
        {"at": 90, "message": "quota high {window} {pct}"},
    ],
}


def write_config(tmp_path: Path, config: object) -> None:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / "codex-usage-meter.json").write_text(json.dumps(config), encoding="utf-8")


def legacy_config(tmp_path: Path) -> None:
    write_config(tmp_path, LEGACY_CONFIG)


def norm(line: str) -> str:
    """Blank the clock-dependent reset countdowns so subprocess tests need no clock seam."""
    return re.sub(r"\(reset: [^)]*\)", "(reset: X)", line)


def load_module():
    spec = importlib.util.spec_from_file_location("usage_meter", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def event(tokens: int | None = None, window: int = 475000, primary: float | None = 12.0, secondary: float | None = 74.0,
          info: bool = True, limits: bool = True) -> str:
    data = json.loads(CAPTURED)
    payload = data["payload"]
    if info and tokens is not None:
        payload["info"]["last_token_usage"]["total_tokens"] = tokens
        payload["info"]["model_context_window"] = window
    if not info:
        payload["info"] = None
    if not limits:
        payload["rate_limits"] = None
    else:
        rl = payload["rate_limits"]
        rl["primary"] = None if primary is None else {**rl["primary"], "used_percent": primary}
        rl["secondary"] = None if secondary is None else {**rl["secondary"], "used_percent": secondary}
    return json.dumps(data)


def write_rollout(path: Path, lines: list[str], trailing: str = "") -> Path:
    path.write_text("\n".join(lines) + "\n" + trailing, encoding="utf-8")
    return path


def run_hook(
    tmp_path: Path, payload: dict, session: str | None = "s1", event: str | None = "UserPromptSubmit",
) -> tuple[dict, subprocess.CompletedProcess]:
    env = {**os.environ, "CODEX_HOME": str(tmp_path / "home")}
    if event is not None:
        payload = {"hook_event_name": event, **payload}
    if session is not None:
        payload = {"session_id": session, **payload}
    proc = subprocess.run(
        [sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout), proc


def run_stop(tmp_path: Path, rollout: Path, session: str | None = "s1", **extra) -> dict:
    payload = {"hook_event_name": "Stop", "transcript_path": str(rollout), **extra}
    return run_hook(tmp_path, payload, session=session)[0]


def context_of(out: dict) -> str:
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    return out["hookSpecificOutput"]["additionalContext"]


def test_prompt_gets_line_and_no_message_or_state(tmp_path):
    rollout = write_rollout(tmp_path / "r.jsonl", [CAPTURED])

    out, _ = run_hook(tmp_path, {"transcript_path": str(rollout)})

    assert norm(context_of(out)) == CAPTURED_NORM
    assert set(out) == {"hookSpecificOutput"}
    assert not (tmp_path / "home").exists()


def test_captured_line_exact_with_fixed_clock(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "home"))
    rollout = write_rollout(tmp_path / "r.jsonl", [CAPTURED])

    data = {"session_id": "s1", "hook_event_name": "UserPromptSubmit", "transcript_path": str(rollout)}
    out = load_module().run(data, NOW)

    assert out == {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": CAPTURED_LINE}}
    assert not (tmp_path / "home").exists()


def test_first_turn_without_token_count_is_unknown(tmp_path):
    rollout = write_rollout(tmp_path / "r.jsonl", ['{"type":"session_meta","payload":{}}'])

    out, _ = run_hook(tmp_path, {"transcript_path": str(rollout)})

    assert out == {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "ctx unknown"}}


def test_transcript_path_null_is_unknown(tmp_path):
    out, _ = run_hook(tmp_path, {"transcript_path": None})

    assert context_of(out) == "ctx unknown"


def test_newest_event_with_null_info_falls_back_to_older_info(tmp_path):
    newest = event(info=False, primary=13.0)
    rollout = write_rollout(tmp_path / "r.jsonl", [event(tokens=237500, secondary=20.0), newest])

    out, _ = run_hook(tmp_path, {"transcript_path": str(rollout)})

    # ctx from the older event, quota from the newest one
    assert norm(context_of(out)) == "ctx 50.0% (237,500 / 475,000) | 5h 13% (reset: X) wk 74% (reset: X)"


def test_partial_trailing_line_is_skipped(tmp_path):
    rollout = write_rollout(tmp_path / "r.jsonl", [CAPTURED], trailing=CAPTURED[:200])

    out, _ = run_hook(tmp_path, {"transcript_path": str(rollout)})

    assert norm(context_of(out)) == CAPTURED_NORM


def test_token_count_beyond_one_scan_chunk_is_found(tmp_path):
    module = load_module()
    filler = json.dumps({"type": "response_item", "payload": {"text": "x" * 900}})
    lines = [CAPTURED] + [filler] * 300
    rollout = write_rollout(tmp_path / "r.jsonl", lines)
    chunk = 4096
    assert rollout.stat().st_size > chunk * 10

    info, limits = module.scan_rollout(str(rollout), chunk_size=chunk)

    assert info["last_token_usage"]["total_tokens"] == 92684
    assert limits["secondary"]["used_percent"] == 74.0
    out, _ = run_hook(tmp_path, {"transcript_path": str(rollout)})
    assert norm(context_of(out)) == CAPTURED_NORM


def test_scan_picks_newest_event_when_line_spans_chunks(tmp_path):
    module = load_module()
    old, new = event(tokens=1000), event(tokens=2000)
    rollout = write_rollout(tmp_path / "r.jsonl", [old, new])

    info, _ = module.scan_rollout(str(rollout), chunk_size=64)

    assert info["last_token_usage"]["total_tokens"] == 2000


def test_band_fires_once_then_again_after_drop_and_recross(tmp_path):
    legacy_config(tmp_path)
    def prompt(tokens: int) -> dict:
        rollout = write_rollout(tmp_path / "r.jsonl", [event(tokens=tokens, secondary=10.0)])
        return run_stop(tmp_path, rollout)

    first = prompt(130000)  # 27.4%
    again = prompt(140000)  # still band 25
    higher = prompt(250000)  # 52.6%
    dropped = prompt(10000)
    recross = prompt(130000)

    assert first["systemMessage"] == "ctx low 27.4"
    assert again == {}
    assert higher["systemMessage"] == "ctx mid 52.6"
    assert dropped == {}
    assert recross["systemMessage"] == first["systemMessage"]


def test_ctx_and_quota_messages_are_joined_by_newline(tmp_path):
    legacy_config(tmp_path)
    rollout = write_rollout(tmp_path / "r.jsonl", [event(tokens=400000, primary=95.0, secondary=70.0)])

    out = run_stop(tmp_path, rollout)

    assert out["systemMessage"] == (
        "ctx high 84.2\n"
        "quota high 5h 95\n"
        "quota warn wk 70"
    )


def test_unknown_info_leaves_context_band_untouched(tmp_path):
    legacy_config(tmp_path)
    high = write_rollout(tmp_path / "r.jsonl", [event(tokens=400000, secondary=10.0)])
    run_stop(tmp_path, high)
    unknown = write_rollout(tmp_path / "r.jsonl", [event(info=False, secondary=10.0)])
    run_stop(tmp_path, unknown)

    back = write_rollout(tmp_path / "r.jsonl", [event(tokens=400000, secondary=10.0)])
    out = run_stop(tmp_path, back)

    assert out == {}


def test_stop_with_agent_id_prints_nothing_and_writes_no_state(tmp_path):
    rollout = write_rollout(tmp_path / "r.jsonl", [CAPTURED])

    out = run_stop(tmp_path, rollout, agent_id="a1", agent_type="worker")

    assert out == {}
    assert not (tmp_path / "home").exists()


def test_stop_without_session_id_prints_nothing_and_writes_no_state(tmp_path):
    rollout = write_rollout(tmp_path / "r.jsonl", [CAPTURED])

    out = run_stop(tmp_path, rollout, session=None)

    assert out == {}
    assert not (tmp_path / "home").exists()


def test_state_file_records_bands_per_session(tmp_path):
    legacy_config(tmp_path)
    rollout = write_rollout(tmp_path / "r.jsonl", [CAPTURED])

    run_stop(tmp_path, rollout, session="abc")

    state = json.loads((tmp_path / "home" / "codex-usage-meter" / "state" / "abc.json").read_text(encoding="utf-8"))
    assert state == {"ctx": 0, "quota": {"5h": 0, "wk": 70}}


def test_corrupt_state_is_treated_as_fresh(tmp_path):
    legacy_config(tmp_path)
    state_dir = tmp_path / "home" / "codex-usage-meter" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "s1.json").write_text("{not json", encoding="utf-8")
    rollout = write_rollout(tmp_path / "r.jsonl", [CAPTURED])

    out = run_stop(tmp_path, rollout)

    assert out == {"systemMessage": WK_MESSAGE}


def test_missing_transcript_file_yields_usage_error(tmp_path):
    out, _ = run_hook(tmp_path, {"transcript_path": str(tmp_path / "nope.jsonl")})

    assert context_of(out) == "ctx unknown (usage error)"


@pytest.mark.parametrize(
    ("left_seconds", "expected"),
    [
        (-5, " (reset: passed)"),
        (0, " (reset: passed)"),
        (29, " (reset: 0m)"),
        (45 * 60, " (reset: 45m)"),
        (60 * 60, " (reset: 1h 0m)"),
        (2 * 3600 + 10 * 60, " (reset: 2h 10m)"),
        (86400, " (reset: 1d 0h)"),
        (3 * 86400 + 4 * 3600, " (reset: 3d 4h)"),
    ],
)
def test_format_reset_forms(left_seconds, expected):
    module = load_module()

    assert module.format_reset(1000 + left_seconds, 1000) == expected


def test_format_reset_missing_resets_at_has_no_suffix():
    assert load_module().format_reset(None, 1000) == ""


def test_quota_label_for_unknown_window_uses_minutes():
    module = load_module()
    limits = copy.deepcopy(json.loads(CAPTURED)["payload"]["rate_limits"])
    limits["primary"]["window_minutes"] = 60

    line, _, quota = module.build_line(None, limits, NOW)

    assert line.startswith("ctx unknown | 60m 12% (reset: 2h 10m) wk 74%")
    assert quota == {"60m": (12.0, "2h 10m"), "wk": (74.0, "1d 17h")}


def test_context_percent_is_capped_at_window(tmp_path):
    rollout = write_rollout(tmp_path / "r.jsonl", [event(tokens=600000, secondary=10.0)])

    out, _ = run_hook(tmp_path, {"transcript_path": str(rollout)})

    assert context_of(out).startswith("ctx 100.0% (600,000 / 475,000) | ")


def test_quota_band_does_not_fire_on_drop_and_fires_again_on_recross(tmp_path):
    legacy_config(tmp_path)
    def prompt(secondary: float) -> dict:
        rollout = write_rollout(tmp_path / "r.jsonl", [event(tokens=1000, secondary=secondary)])
        return run_stop(tmp_path, rollout)

    first, same, dropped, recross = prompt(74.0), prompt(75.0), prompt(10.0), prompt(92.0)

    assert first["systemMessage"] == WK_MESSAGE
    assert same == {}
    assert dropped == {}
    assert recross["systemMessage"] == "quota high wk 92"


def run_raw(tmp_path: Path, stdin: bytes) -> subprocess.CompletedProcess:
    env = {**os.environ, "CODEX_HOME": str(tmp_path / "home")}
    return subprocess.run([sys.executable, str(HOOK)], input=stdin, capture_output=True, env=env, timeout=60)


def prompt_stdin(transcript_path: object) -> bytes:
    data = {"session_id": "s1", "hook_event_name": "UserPromptSubmit", "transcript_path": transcript_path}
    return json.dumps(data).encode()


@pytest.mark.parametrize(
    "stdin",
    [b"not json", b"[" * 200000, b"\xff\xfe", b"[]", b"null"],
    ids=["text", "deep-nesting", "bad-utf8", "array", "null"],
)
def test_unparseable_stdin_exits_zero_with_empty_object(tmp_path, stdin):
    proc = run_raw(tmp_path, stdin)

    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {}


def test_stdin_with_utf8_bom_is_parsed(tmp_path):
    rollout = write_rollout(tmp_path / "r.jsonl", [CAPTURED])
    payload = prompt_stdin(str(rollout))

    proc = run_raw(tmp_path, b"\xef\xbb\xbf" + payload)

    assert norm(context_of(json.loads(proc.stdout))) == CAPTURED_NORM


def test_session_id_cannot_escape_state_dir(tmp_path):
    rollout = write_rollout(tmp_path / "r.jsonl", [CAPTURED])

    run_stop(tmp_path, rollout, session="../../x")

    state_dir = tmp_path / "home" / "codex-usage-meter" / "state"
    assert len(list(state_dir.glob("*.json"))) == 1
    assert sorted(p.name for p in (tmp_path / "home").iterdir()) == ["codex-usage-meter"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["home", "r.jsonl"]


def test_token_count_with_other_event_type_is_ignored(tmp_path):
    foreign = json.dumps({**json.loads(CAPTURED), "type": "response_item"})
    rollout = write_rollout(tmp_path / "r.jsonl", [foreign])

    out, _ = run_hook(tmp_path, {"transcript_path": str(rollout)})

    assert context_of(out) == "ctx unknown"


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('"model_context_window": 475000', '"model_context_window": 0'),
        ('"model_context_window": 475000', '"model_context_window": true'),
        ('"total_tokens": 1000', '"total_tokens": NaN'),
        ('"total_tokens": 1000', '"total_tokens": -5'),
    ],
)
def test_invalid_newest_info_falls_back_to_older_valid_info(tmp_path, old, new):
    newest = event(tokens=1000)
    assert old in newest
    rollout = write_rollout(tmp_path / "r.jsonl", [event(tokens=237500), newest.replace(old, new)])

    out, _ = run_hook(tmp_path, {"transcript_path": str(rollout)})

    assert context_of(out).startswith("ctx 50.0% (237,500 / 475,000) | ")


def test_malformed_info_is_skipped_and_quota_still_shown(tmp_path):
    bad = json.loads(CAPTURED)
    del bad["payload"]["info"]["last_token_usage"]
    rollout = write_rollout(tmp_path / "r.jsonl", [json.dumps(bad)])

    out, _ = run_hook(tmp_path, {"transcript_path": str(rollout)})

    assert norm(context_of(out)) == "ctx unknown | 5h 12% (reset: X) wk 74% (reset: X)"


def test_malformed_quota_window_is_omitted_but_rest_is_shown(tmp_path):
    data = json.loads(CAPTURED)
    data["payload"]["rate_limits"]["primary"]["used_percent"] = None
    rollout = write_rollout(tmp_path / "r.jsonl", [json.dumps(data)])

    out, _ = run_hook(tmp_path, {"transcript_path": str(rollout)})

    assert norm(context_of(out)) == "ctx 19.5% (92,684 / 475,000) | wk 74% (reset: X)"


def test_type_corrupted_state_self_heals(tmp_path):
    legacy_config(tmp_path)
    state_dir = tmp_path / "home" / "codex-usage-meter" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "s1.json").write_text('{"ctx": null, "quota": {"wk": "70"}}', encoding="utf-8")
    rollout = write_rollout(tmp_path / "r.jsonl", [CAPTURED])

    out = run_stop(tmp_path, rollout)

    assert out == {"systemMessage": WK_MESSAGE}
    assert json.loads((state_dir / "s1.json").read_text(encoding="utf-8")) == {"ctx": 0, "quota": {"5h": 0, "wk": 70}}


def test_save_failure_keeps_message_and_leaves_no_temp_file(tmp_path):
    legacy_config(tmp_path)
    state_dir = tmp_path / "home" / "codex-usage-meter" / "state"
    (state_dir / "s1.json").mkdir(parents=True)
    rollout = write_rollout(tmp_path / "r.jsonl", [CAPTURED])

    out = run_stop(tmp_path, rollout)

    assert out == {"systemMessage": WK_MESSAGE}
    assert list(state_dir.glob("*.tmp")) == []


def test_scan_budget_limits_how_far_back_the_scan_reads(tmp_path):
    filler = json.dumps({"type": "response_item", "payload": {"text": "x" * 1000}}) + "\n"
    far, near = tmp_path / "far.jsonl", tmp_path / "near.jsonl"
    far.write_text(CAPTURED + "\n" + filler * 17000, encoding="utf-8")
    near.write_text(CAPTURED + "\n" + filler * 15000, encoding="utf-8")
    assert far.stat().st_size > 16 << 20 > near.stat().st_size

    far_out, _ = run_hook(tmp_path, {"transcript_path": str(far)})
    near_out, _ = run_hook(tmp_path, {"transcript_path": str(near)})

    assert context_of(far_out) == "ctx unknown"
    assert norm(context_of(near_out)) == CAPTURED_NORM


@pytest.mark.parametrize("path", [1, True, 7, ""], ids=["int1", "true", "int7", "empty"])
def test_non_string_transcript_path_counts_as_no_transcript(tmp_path, path):
    proc = run_raw(tmp_path, prompt_stdin(path))

    assert proc.returncode == 0
    assert context_of(json.loads(proc.stdout)) == "ctx unknown"


def test_scan_budget_edge_keeps_line_that_starts_exactly_at_the_cut(tmp_path):
    module = load_module()
    tail = json.dumps({"type": "response_item", "payload": {"text": "tail"}})
    inside = (CAPTURED + "\n" + tail).encode()
    rollout = tmp_path / "r.jsonl"
    rollout.write_bytes(b"junk\n" + inside)

    found, _ = module.scan_rollout(str(rollout), budget=len(inside))
    straddling, _ = module.scan_rollout(str(rollout), budget=len(inside) - 1)

    assert found["last_token_usage"]["total_tokens"] == 92684
    assert straddling is None


def test_pathologically_nested_token_count_line_is_skipped(tmp_path):
    nested = '{"type":"event_msg","payload":{"type":"token_count","x":' + "[" * 200000
    rollout = write_rollout(tmp_path / "r.jsonl", [CAPTURED, nested])

    out, _ = run_hook(tmp_path, {"transcript_path": str(rollout)})

    assert norm(context_of(out)) == CAPTURED_NORM


def quota_variant(tmp_path: Path, window: str, **changes) -> str:
    data = json.loads(CAPTURED)
    data["payload"]["rate_limits"][window].update(changes)
    rollout = write_rollout(tmp_path / "r.jsonl", [json.dumps(data)])
    out, _ = run_hook(tmp_path, {"transcript_path": str(rollout)})
    return norm(context_of(out))


def test_infinite_used_percent_omits_that_window(tmp_path):
    line = quota_variant(tmp_path, "primary", used_percent=float("inf"))

    assert line == "ctx 19.5% (92,684 / 475,000) | wk 74% (reset: X)"


def test_nan_resets_at_drops_only_the_suffix(tmp_path):
    line = quota_variant(tmp_path, "primary", resets_at=float("nan"))

    assert line == "ctx 19.5% (92,684 / 475,000) | 5h 12% wk 74% (reset: X)"


def test_zero_window_minutes_omits_that_window(tmp_path):
    line = quota_variant(tmp_path, "secondary", window_minutes=0)

    assert line == "ctx 19.5% (92,684 / 475,000) | 5h 12% (reset: X)"


ASSISTANT_LINE = json.dumps({"type": "response_item", "payload": {"type": "message", "role": "assistant"}})


def stop_payload(rollout: Path) -> dict:
    return {
        "session_id": "s1", "turn_id": "t1", "transcript_path": str(rollout), "cwd": "D:\\work",
        "hook_event_name": "Stop", "model": "gpt-6.1-sol", "permission_mode": "default",
        "stop_hook_active": False, "last_assistant_message": "OK",
    }


def test_stop_live_shaped_input_emits_only_system_message_once(tmp_path):
    legacy_config(tmp_path)
    rollout = write_rollout(tmp_path / "r.jsonl", [ASSISTANT_LINE, CAPTURED])
    payload = stop_payload(rollout)

    first, _ = run_hook(tmp_path, payload, session=None)
    second, _ = run_hook(tmp_path, payload, session=None)

    assert first == {"systemMessage": WK_MESSAGE}
    assert second == {}


def test_stop_without_crossing_prints_exactly_empty_object(tmp_path):
    rollout = write_rollout(tmp_path / "r.jsonl", [event(tokens=1000, primary=10.0, secondary=10.0)])

    out = run_stop(tmp_path, rollout)

    assert out == {}


def test_stop_with_missing_or_garbage_rollout_prints_empty_object(tmp_path):
    garbage = tmp_path / "garbage.jsonl"
    garbage.write_bytes(b'\xff{"token_count"\n[[[\n')

    missing = run_stop(tmp_path, tmp_path / "nope.jsonl")
    broken = run_stop(tmp_path, garbage)

    assert missing == {}
    assert broken == {}


def test_prompt_event_never_emits_message_or_state_even_when_bands_are_crossed(tmp_path):
    rollout = write_rollout(tmp_path / "r.jsonl", [event(tokens=400000, primary=95.0, secondary=70.0)])

    out, _ = run_hook(tmp_path, {"transcript_path": str(rollout)}, event="UserPromptSubmit")

    assert set(out) == {"hookSpecificOutput"}
    assert context_of(out).startswith("ctx 84.2% (400,000 / 475,000) | 5h 95%")
    assert not (tmp_path / "home").exists()


def test_unknown_event_prints_empty_object_and_writes_no_state(tmp_path):
    rollout = write_rollout(tmp_path / "r.jsonl", [CAPTURED])

    out, _ = run_hook(tmp_path, {"transcript_path": str(rollout)}, event="SessionStart")

    assert out == {}
    assert not (tmp_path / "home").exists()


def ctx_rollout(tmp_path: Path, tokens: int, secondary: float = 10.0) -> Path:
    return write_rollout(tmp_path / "r.jsonl", [event(tokens=tokens, primary=10.0, secondary=secondary)])


def stored_state(tmp_path: Path, session: str = "s1") -> dict:
    path = tmp_path / "home" / "codex-usage-meter" / "state" / (session + ".json")
    return json.loads(path.read_text(encoding="utf-8"))


def test_prompt_after_compaction_rearms_ctx_warning_for_next_stop(tmp_path):
    legacy_config(tmp_path)
    first = run_stop(tmp_path, ctx_rollout(tmp_path, 380000))  # 80.0%, band 75
    prompt, _ = run_hook(tmp_path, {"transcript_path": str(ctx_rollout(tmp_path, 261250))})  # 55.0%, band 50
    lowered = stored_state(tmp_path)["ctx"]
    second = run_stop(tmp_path, ctx_rollout(tmp_path, 361000))  # 76.0%, band 75 again

    assert first == {"systemMessage": "ctx high 80.0"}
    assert set(prompt) == {"hookSpecificOutput"}
    assert lowered == 50
    assert second == {"systemMessage": "ctx high 76.0"}


def test_prompt_after_quota_drop_rearms_quota_warning_for_next_stop(tmp_path):
    legacy_config(tmp_path)
    first = run_stop(tmp_path, ctx_rollout(tmp_path, 1000, secondary=92.0))
    run_hook(tmp_path, {"transcript_path": str(ctx_rollout(tmp_path, 1000, secondary=10.0))})
    second = run_stop(tmp_path, ctx_rollout(tmp_path, 1000, secondary=92.0))

    assert first == second == {"systemMessage": "quota high wk 92"}


def test_prompt_lowers_quota_to_the_current_band_not_below(tmp_path):
    legacy_config(tmp_path)
    run_stop(tmp_path, ctx_rollout(tmp_path, 1000, secondary=92.0))
    run_hook(tmp_path, {"transcript_path": str(ctx_rollout(tmp_path, 1000, secondary=75.0))})
    lowered = stored_state(tmp_path)["quota"]["wk"]
    same_band = run_stop(tmp_path, ctx_rollout(tmp_path, 1000, secondary=80.0))

    assert lowered == 70
    assert same_band == {}


def test_prompt_seeing_a_higher_band_leaves_stored_band_and_next_stop_still_fires(tmp_path):
    legacy_config(tmp_path)
    first = run_stop(tmp_path, ctx_rollout(tmp_path, 150000))  # 31.6%, band 25
    prompt, _ = run_hook(tmp_path, {"transcript_path": str(ctx_rollout(tmp_path, 285000))})  # 60.0%, band 50
    stored = stored_state(tmp_path)["ctx"]
    second = run_stop(tmp_path, ctx_rollout(tmp_path, 285000))

    assert first == {"systemMessage": "ctx low 31.6"}
    assert set(prompt) == {"hookSpecificOutput"}
    assert stored == 25
    assert second == {"systemMessage": "ctx mid 60.0"}


def test_prompt_with_agent_id_does_not_lower_bands(tmp_path):
    legacy_config(tmp_path)
    run_stop(tmp_path, ctx_rollout(tmp_path, 380000))
    run_hook(tmp_path, {"transcript_path": str(ctx_rollout(tmp_path, 1000)), "agent_id": "a1"})

    assert stored_state(tmp_path)["ctx"] == 75
    assert run_stop(tmp_path, ctx_rollout(tmp_path, 380000)) == {}


def test_event_name_missing_prints_empty_object(tmp_path):
    rollout = write_rollout(tmp_path / "r.jsonl", [CAPTURED])

    out, _ = run_hook(tmp_path, {"transcript_path": str(rollout)}, event=None)

    assert out == {}
    assert not (tmp_path / "home").exists()


def test_stop_without_any_figures_prints_empty_object_and_writes_no_state(tmp_path):
    rollout = write_rollout(tmp_path / "r.jsonl", [ASSISTANT_LINE])

    out = run_stop(tmp_path, rollout)

    assert out == {}
    assert not (tmp_path / "home").exists()


def test_stop_with_empty_session_id_prints_empty_object_and_writes_no_state(tmp_path):
    rollout = write_rollout(tmp_path / "r.jsonl", [CAPTURED])

    out = run_stop(tmp_path, rollout, session="")

    assert out == {}
    assert not (tmp_path / "home").exists()


def stop_in_process(tmp_path: Path, monkeypatch, rollout: Path, now: float = NOW) -> dict:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "home"))
    data = {"hook_event_name": "Stop", "session_id": "s1", "transcript_path": str(rollout)}
    return load_module().run(data, now)


@pytest.mark.parametrize(
    ("tokens", "expected"),
    [
        (237500, "⚠ Context 50.0% — consider /compact after this task"),
        (380000, "⚠ Context 80.0% — /compact or start a new session soon"),
        (437000, "⚠ Context 92.0% — near the limit, /compact now"),
    ],
)
def test_default_context_bands_and_messages(tmp_path, tokens, expected):
    out = run_stop(tmp_path, ctx_rollout(tmp_path, tokens))

    assert out == {"systemMessage": expected}


@pytest.mark.parametrize(
    ("used", "expected"),
    [
        (82.0, "⚠ Codex wk usage 82% — pace your usage (reset: X)"),
        (96.0, "⚠ Codex wk usage 96% — nearly used up (reset: X)"),
    ],
)
def test_default_quota_bands_and_messages(tmp_path, used, expected):
    out = run_stop(tmp_path, ctx_rollout(tmp_path, 1000, secondary=used))

    assert norm(out["systemMessage"]) == expected


@pytest.mark.parametrize(
    ("now", "drop_resets_at", "expected"),
    [(1794624083 + 10, False, "passed"), (NOW, True, "unknown"), (NOW, False, "1d 17h")],
    ids=["passed", "unknown", "days-hours"],
)
def test_quota_reset_placeholder_forms(tmp_path, monkeypatch, now, drop_resets_at, expected):
    data = json.loads(event(secondary=82.0))
    if drop_resets_at:
        del data["payload"]["rate_limits"]["secondary"]["resets_at"]
    rollout = write_rollout(tmp_path / "r.jsonl", [json.dumps(data)])

    out = stop_in_process(tmp_path, monkeypatch, rollout, now)

    assert out == {"systemMessage": f"⚠ Codex wk usage 82% — pace your usage (reset: {expected})"}


def test_custom_warnings_and_placeholders_from_file(tmp_path, monkeypatch):
    write_config(tmp_path, {
        "context_warnings": [
            {"at": 10, "message": "C {pct} b{band} {window} {foo}"},
            {"at": 20, "message": "D"},
        ],
        "quota_warnings": [{"at": 50.5, "message": "Q {window} {pct} {band} {reset} {unknown}"}],
    })
    rollout = write_rollout(tmp_path / "r.jsonl", [CAPTURED])

    out = stop_in_process(tmp_path, monkeypatch, rollout)

    assert out == {"systemMessage": "C 19.5 b10 {window} {foo}\nQ wk 74 50.5 1d 17h {unknown}"}


@pytest.mark.parametrize(
    ("at", "expected"),
    [(10.0, "b10"), (12.5, "b12.5"), (33.333333333333336, "b33.33333333")],
    ids=["integral-float", "fraction", "ten-significant-digits"],
)
def test_band_placeholder_formatting(tmp_path, monkeypatch, at, expected):
    write_config(tmp_path, {"context_warnings": [{"at": at, "message": "b{band}"}]})

    out = stop_in_process(tmp_path, monkeypatch, ctx_rollout(tmp_path, 190000))

    assert out == {"systemMessage": expected}


def notices_and_rest(message: str) -> tuple[list[str], list[str]]:
    lines = message.split("\n")
    notices = [line for line in lines if line.startswith("codex-usage-meter.json: ")]
    return notices, [line for line in lines if line not in notices]


def assert_single_notice(message: str, key: str) -> list[str]:
    notices, rest = notices_and_rest(message)
    assert len(notices) == 1
    assert notices[0].startswith(f"codex-usage-meter.json: {key} ignored (")
    assert notices[0].endswith(") — using defaults")
    return rest


GOOD_WARNING = [{"at": 10, "message": "a"}]
BAD_WARNINGS = {
    "non-list": 50,
    "empty-list": [],
    "entry-not-object": [5],
    "at-missing": [{"message": "a"}],
    "at-string": [{"at": "50", "message": "a"}],
    "at-zero": [{"at": 0, "message": "a"}],
    "at-below-one": [{"at": 0.5, "message": "a"}],
    "at-above-100": [{"at": 101, "message": "a"}],
    "at-bool": [{"at": True, "message": "a"}],
    "at-nan": [{"at": float("nan"), "message": "a"}],
    "at-huge-int": [{"at": 10**400, "message": "a"}],
    "message-empty": [{"at": 10, "message": " "}],
    "message-not-string": [{"at": 10, "message": 5}],
    "message-missing": [{"at": 10}],
    "not-ascending": [{"at": 75, "message": "a"}, {"at": 50, "message": "b"}],
    "equal-values": [{"at": 50, "message": "a"}, {"at": 50, "message": "b"}],
}


@pytest.mark.parametrize("bad", BAD_WARNINGS.values(), ids=BAD_WARNINGS.keys())
def test_bad_context_warnings_fall_back_but_quota_warnings_still_apply(tmp_path, bad):
    write_config(tmp_path, {"context_warnings": bad, "quota_warnings": [{"at": 50, "message": "Q {pct}"}]})
    rollout = write_rollout(tmp_path / "r.jsonl", [event(tokens=285000, secondary=74.0)])

    out = run_stop(tmp_path, rollout)

    rest = assert_single_notice(out["systemMessage"], "context_warnings")
    assert rest == ["⚠ Context 60.0% — consider /compact after this task", "Q 74"]


@pytest.mark.parametrize("bad", BAD_WARNINGS.values(), ids=BAD_WARNINGS.keys())
def test_bad_quota_warnings_fall_back_but_context_warnings_still_apply(tmp_path, bad):
    write_config(tmp_path, {"quota_warnings": bad, "context_warnings": [{"at": 10, "message": "C {pct}"}]})
    rollout = write_rollout(tmp_path / "r.jsonl", [event(secondary=82.0)])

    out = run_stop(tmp_path, rollout)

    rest = assert_single_notice(out["systemMessage"], "quota_warnings")
    assert [norm(line) for line in rest] == ["C 19.5", "⚠ Codex wk usage 82% — pace your usage (reset: X)"]


def test_huge_integer_in_config_keeps_the_usage_line_on_both_events(tmp_path):
    write_config(tmp_path, {"context_warnings": BAD_WARNINGS["at-huge-int"], "quota_warnings": GOOD_WARNING})
    rollout = write_rollout(tmp_path / "r.jsonl", [CAPTURED])

    prompt, _ = run_hook(tmp_path, {"transcript_path": str(rollout)})
    stop = run_stop(tmp_path, rollout)

    assert norm(context_of(prompt)) == CAPTURED_NORM
    assert set(stop) == {"systemMessage"}


def test_old_style_and_one_sided_keys_are_ignored_without_notice(tmp_path):
    write_config(tmp_path, {"context_bands": [10], "context_messages": ["x"], "quota_bands": [1]})

    out = run_stop(tmp_path, ctx_rollout(tmp_path, 237500))

    assert out == {"systemMessage": "⚠ Context 50.0% — consider /compact after this task"}


def test_config_with_utf8_bom_is_honoured(tmp_path):
    (tmp_path / "home").mkdir()
    body = json.dumps({"context_warnings": [{"at": 10, "message": "bom {pct}"}]}).encode()
    (tmp_path / "home" / "codex-usage-meter.json").write_bytes(b"\xef\xbb\xbf" + body)

    out = run_stop(tmp_path, write_rollout(tmp_path / "r.jsonl", [CAPTURED]))

    assert out == {"systemMessage": "bom 19.5"}


def padded_config(size: int) -> bytes:
    head, tail = '{"warnings": false, "pad": "', '"}'
    return (head + "a" * (size - len(head) - len(tail)) + tail).encode()


def test_config_of_exactly_64_kib_is_read(tmp_path):
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / "codex-usage-meter.json").write_bytes(padded_config(64 << 10))

    out = run_stop(tmp_path, ctx_rollout(tmp_path, 437000))

    assert out == {}


def test_oversized_config_means_defaults_and_is_announced_once(tmp_path):
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / "codex-usage-meter.json").write_bytes(padded_config((64 << 10) + 1))
    rollout = ctx_rollout(tmp_path, 437000)

    first = run_stop(tmp_path, rollout)
    second = run_stop(tmp_path, rollout)

    assert first == {"systemMessage": (
        "codex-usage-meter.json: config file ignored (larger than 64 KiB) — using defaults\n"
        "⚠ Context 92.0% — near the limit, /compact now"
    )}
    assert second == {}


def test_deeply_nested_config_means_defaults_with_notice(tmp_path):
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / "codex-usage-meter.json").write_text("[" * 50000, encoding="utf-8")

    out = run_stop(tmp_path, ctx_rollout(tmp_path, 237500))

    assert out == {"systemMessage": (
        "codex-usage-meter.json: config file ignored (not valid JSON) — using defaults\n"
        "⚠ Context 50.0% — consider /compact after this task"
    )}


@pytest.mark.parametrize(
    ("raw", "reason"),
    [("[1, 2]", "not a JSON object"), ('"text"', "not a JSON object"), ("null", "not a JSON object"),
     ("{not json", "not valid JSON"), ("", "not valid JSON")],
    ids=["array", "string", "null", "garbage", "empty"],
)
def test_non_object_or_unparseable_config_means_defaults_with_notice(tmp_path, raw, reason):
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / "codex-usage-meter.json").write_text(raw, encoding="utf-8")

    out = run_stop(tmp_path, ctx_rollout(tmp_path, 237500))

    assert out == {"systemMessage": (
        f"codex-usage-meter.json: config file ignored ({reason}) — using defaults\n"
        "⚠ Context 50.0% — consider /compact after this task"
    )}


def test_unreadable_config_means_defaults_without_notice(tmp_path):
    (tmp_path / "home" / "codex-usage-meter.json").mkdir(parents=True)

    out = run_stop(tmp_path, ctx_rollout(tmp_path, 237500))

    assert out == {"systemMessage": "⚠ Context 50.0% — consider /compact after this task"}


def test_config_notice_is_shown_once_and_again_when_the_problem_set_changes(tmp_path):
    rollout = ctx_rollout(tmp_path, 1000)
    bad = BAD_WARNINGS["non-list"]

    write_config(tmp_path, {"context_warnings": bad})
    first, second = run_stop(tmp_path, rollout), run_stop(tmp_path, rollout)
    write_config(tmp_path, {"context_warnings": bad, "quota_warnings": bad})
    widened = run_stop(tmp_path, rollout)
    write_config(tmp_path, {})
    cleared = run_stop(tmp_path, rollout)
    write_config(tmp_path, {"context_warnings": bad})
    again = run_stop(tmp_path, rollout)
    prompt, _ = run_hook(tmp_path, {"transcript_path": str(rollout)})

    assert set(first) == {"systemMessage"}
    assert first["systemMessage"].startswith("codex-usage-meter.json: context_warnings ignored (")
    assert second == {}
    notices, rest = notices_and_rest(widened["systemMessage"])
    assert [n.split(" ignored")[0] for n in notices] == [
        "codex-usage-meter.json: context_warnings", "codex-usage-meter.json: quota_warnings",
    ]
    assert rest == []
    assert cleared == {}
    assert again == first
    assert set(prompt) == {"hookSpecificOutput"}


def test_warnings_false_still_announces_config_problems(tmp_path):
    write_config(tmp_path, {"warnings": False, "context_warnings": BAD_WARNINGS["non-list"]})
    rollout = ctx_rollout(tmp_path, 437000, secondary=96.0)

    out = run_stop(tmp_path, rollout)

    assert set(out) == {"systemMessage"}
    assert assert_single_notice(out["systemMessage"], "context_warnings") == []


def test_warnings_false_silences_stop_and_leaves_state_alone_but_line_is_still_injected(tmp_path):
    write_config(tmp_path, {"warnings": False})
    state_dir = tmp_path / "home" / "codex-usage-meter" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "s1.json").write_text('{"ctx": 50, "quota": {}}', encoding="utf-8")
    rollout = ctx_rollout(tmp_path, 437000, secondary=96.0)

    stop = run_stop(tmp_path, rollout)
    prompt, _ = run_hook(tmp_path, {"transcript_path": str(rollout)})

    assert stop == {}
    assert json.loads((state_dir / "s1.json").read_text(encoding="utf-8")) == {"ctx": 50, "quota": {}}
    assert norm(context_of(prompt)).startswith("ctx 92.0% (437,000 / 475,000) | 5h 10% (reset: X) wk 96%")


def test_warnings_with_invalid_value_defaults_to_enabled_with_notice(tmp_path):
    write_config(tmp_path, {"warnings": 0})

    out = run_stop(tmp_path, ctx_rollout(tmp_path, 237500))

    assert assert_single_notice(out["systemMessage"], "warnings") == [
        "⚠ Context 50.0% — consider /compact after this task",
    ]


def write_state(tmp_path: Path, text: str) -> None:
    state_dir = tmp_path / "home" / "codex-usage-meter" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "s1.json").write_text(text, encoding="utf-8")


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ('{"ctx": 60, "quota": {"wk": 85}}', {}),
        ('{"ctx": 40, "quota": {"wk": 70}}', {"systemMessage": (
            "⚠ Context 55.0% — consider /compact after this task\n"
            "⚠ Codex wk usage 82% — pace your usage (reset: X)"
        )}),
    ],
    ids=["mapped-down-to-configured", "below-every-band-is-zero"],
)
def test_stored_band_maps_to_highest_configured_band_not_above_it(tmp_path, stored, expected):
    write_state(tmp_path, stored)
    rollout = write_rollout(tmp_path / "r.jsonl", [event(tokens=261250, secondary=82.0)])

    out = run_stop(tmp_path, rollout)

    if out:
        out = {"systemMessage": norm(out["systemMessage"])}
    assert out == expected


def test_prompt_lowering_uses_the_mapped_stored_band(tmp_path):
    write_state(tmp_path, '{"ctx": 60, "quota": {}}')
    state_file = tmp_path / "home" / "codex-usage-meter" / "state" / "s1.json"

    run_hook(tmp_path, {"transcript_path": str(ctx_rollout(tmp_path, 261250))})
    unchanged = json.loads(state_file.read_text(encoding="utf-8"))["ctx"]
    run_hook(tmp_path, {"transcript_path": str(ctx_rollout(tmp_path, 95000))})
    lowered = json.loads(state_file.read_text(encoding="utf-8"))["ctx"]

    assert unchanged == 60
    assert lowered == 0


def test_huge_integer_in_state_does_not_silence_stop(tmp_path):
    write_state(tmp_path, '{"ctx": ' + "9" * 400 + ', "quota": {"wk": ' + "9" * 400 + "}}")
    rollout = write_rollout(tmp_path / "r.jsonl", [event(secondary=82.0)])

    out = run_stop(tmp_path, rollout)

    assert norm(out["systemMessage"]) == "⚠ Codex wk usage 82% — pace your usage (reset: X)"


@pytest.mark.parametrize(
    ("config", "state", "rollout_event", "expected"),
    [
        ({"context_warnings": [{"at": 1, "message": "a"}, {"at": 50, "message": "b"}]},
         '{"ctx": true, "quota": {}}', event(tokens=4750), "a"),
        ({"quota_warnings": [{"at": 1, "message": "q"}]},
         '{"ctx": 0, "quota": {"wk": true}}', event(tokens=1000, primary=None, secondary=5.0), "q"),
    ],
    ids=["ctx-bool", "quota-bool"],
)
def test_stored_boolean_band_reads_as_zero(tmp_path, config, state, rollout_event, expected):
    write_config(tmp_path, config)
    write_state(tmp_path, state)

    out = run_stop(tmp_path, write_rollout(tmp_path / "r.jsonl", [rollout_event]))

    assert out == {"systemMessage": expected}


def test_unresolvable_home_keeps_the_usage_line_and_stop_prints_empty_object(tmp_path):
    rollout = write_rollout(tmp_path / "r.jsonl", [CAPTURED])
    drop = {"CODEX_HOME", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH"}
    env = {k: v for k, v in os.environ.items() if k.upper() not in drop}

    def run_event(name: str) -> dict:
        payload = {"hook_event_name": name, "session_id": "s1", "transcript_path": str(rollout)}
        proc = subprocess.run(
            [sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)

    assert norm(context_of(run_event("UserPromptSubmit"))) == CAPTURED_NORM
    assert run_event("Stop") == {}


def test_load_config_never_raises_when_home_cannot_be_resolved(monkeypatch):
    for name in ("CODEX_HOME", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH"):
        monkeypatch.delenv(name, raising=False)

    config = load_module().load_config()

    assert config["warnings"] is True
    assert config["problems"] == []


def test_problem_recurrence_is_announced_again_after_a_clean_stop_with_warnings_false(tmp_path):
    rollout = ctx_rollout(tmp_path, 1000)
    bad = {"warnings": False, "context_warnings": BAD_WARNINGS["non-list"]}

    write_config(tmp_path, bad)
    first = run_stop(tmp_path, rollout)
    write_config(tmp_path, {"warnings": False})
    clean = run_stop(tmp_path, rollout)
    write_config(tmp_path, bad)
    again = run_stop(tmp_path, rollout)

    assert first["systemMessage"].startswith("codex-usage-meter.json: context_warnings ignored (")
    assert clean == {}
    assert again == first


def test_problem_recurrence_is_announced_again_after_a_clean_stop_without_figures(tmp_path):
    rollout = write_rollout(tmp_path / "r.jsonl", [ASSISTANT_LINE])
    bad = {"context_warnings": BAD_WARNINGS["non-list"]}

    write_config(tmp_path, bad)
    first = run_stop(tmp_path, rollout)
    write_config(tmp_path, {})
    clean = run_stop(tmp_path, rollout)
    write_config(tmp_path, bad)
    again = run_stop(tmp_path, rollout)

    assert first["systemMessage"].startswith("codex-usage-meter.json: context_warnings ignored (")
    assert clean == {}
    assert again == first


def test_config_notice_survives_an_unreadable_rollout(tmp_path):
    write_config(tmp_path, {"context_warnings": BAD_WARNINGS["non-list"]})

    out = run_stop(tmp_path, tmp_path / "missing.jsonl")

    assert set(out) == {"systemMessage"}
    assert assert_single_notice(out["systemMessage"], "context_warnings") == []


def test_deeply_nested_state_file_is_treated_as_fresh(tmp_path):
    write_state(tmp_path, "[" * 50000)
    rollout = write_rollout(tmp_path / "r.jsonl", [event(secondary=82.0)])

    out = run_stop(tmp_path, rollout)

    assert norm(out["systemMessage"]) == "⚠ Codex wk usage 82% — pace your usage (reset: X)"


# Captured from a codex-tui 0.159.2 rollout; tests rewrite timestamp, turn_id and the usage figures.
USAGE_RECORD = (
    '{"timestamp":"2026-11-12T07:59:17.265Z","ordinal":25,"type":"token_usage_record","payload":{"thread_id":'
    '"01a1cec3-16a2-73c1-9875-8060de0e73c2","turn_id":"01a1cec4-7c69-7cd3-b447-101658d43ca0","session_id":'
    '"01a1cec3-16a2-73c1-9875-8060de0e73c2","root_turn_id":"01a1cec4-7c69-7cd3-b447-101658d43ca0","response_id":'
    '"resp_03a8a698f9485a76016af571d2136487d0aa4941b11fa82b35","usage":{"input_tokens":22392,"cached_input_tokens":'
    '22144,"cache_write_input_tokens":0,"output_tokens":6,"reasoning_output_tokens":0,"total_tokens":22398},'
    '"turn_token_usage":{"input_tokens":22392,"cached_input_tokens":22144,"cache_write_input_tokens":0,'
    '"output_tokens":6,"reasoning_output_tokens":0,"total_tokens":22398},"thread_token_usage":{"input_tokens":44715,'
    '"cached_input_tokens":22144,"cache_write_input_tokens":0,"output_tokens":11,"reasoning_output_tokens":0,'
    '"total_tokens":44726}}}'
)
TASK_STARTED = (
    '{"timestamp":"2026-11-12T07:59:05.904Z","ordinal":18,"type":"event_msg","payload":{"type":"task_started",'
    '"turn_id":"01a1cec4-7c69-7cd3-b447-101658d43ca0","root_turn_id":"01a1cec4-7c69-7cd3-b447-101658d43ca0",'
    '"started_at":1794470345,"model_context_window":475000,"collaboration_mode_kind":"default"}}'
)
CACHE_CONFIG = {"cache_warm_message": "warm {until} {ttl}", "cache_cold_message": "cold {idle} {ttl}"}
# Local noon, so a 30-minute expiry stays on the same local day in every time zone.
CACHE_NOW = datetime(2026, 11, 12, 12, 0).timestamp()


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def clock(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%H:%M")


def usage_record(at: float, turn: str, input_tokens: int, cached: int) -> str:
    data = json.loads(USAGE_RECORD)
    data["timestamp"] = iso(at)
    data["payload"]["turn_id"] = turn
    data["payload"]["usage"].update(input_tokens=input_tokens, cached_input_tokens=cached)
    return json.dumps(data)


def task_started(at: float, turn: str) -> str:
    data = json.loads(TASK_STARTED)
    data["timestamp"] = iso(at)
    data["payload"]["turn_id"] = turn
    return json.dumps(data)


def compacted(at: float) -> str:
    # Real payloads also carry replacement_history and ~100 KB of other fields; only the record type matters here.
    return json.dumps({"timestamp": iso(at), "type": "compacted", "payload": {"message": "", "replacement_history": []}})


def two_turns(
    tmp_path: Path, idle: float, cached: int, between: tuple = (), extra: tuple = (),
    previous_input: int = 22323, own_input: int = 22392,
) -> Path:
    """Turn a: one request at CACHE_NOW-idle-60. Turn b starts `idle` seconds later; its first request reuses `cached`."""
    a_done = CACHE_NOW - idle - 60
    lines = [
        task_started(a_done - 10, "a"), usage_record(a_done, "a", previous_input, 0), *between,
        task_started(a_done + idle, "b"), usage_record(a_done + idle + 5, "b", own_input, cached), *extra,
    ]
    return write_rollout(tmp_path / "r.jsonl", lines)


def stop_at(tmp_path: Path, monkeypatch, rollout: Path, now: float = CACHE_NOW) -> dict:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "home"))
    return load_module().run({"session_id": "s1", "hook_event_name": "Stop", "transcript_path": str(rollout)}, now)


def test_warm_line_counts_ttl_from_the_newest_request(tmp_path, monkeypatch):
    write_config(tmp_path, CACHE_CONFIG)
    rollout = two_turns(tmp_path, idle=120, cached=22144)

    out = stop_at(tmp_path, monkeypatch, rollout)

    assert out == {"systemMessage": f"warm {clock(CACHE_NOW - 55 + 1800)} 30"}


def test_turn_that_reused_under_half_the_previous_input_gets_a_cold_line_before_the_warm_line(tmp_path, monkeypatch):
    write_config(tmp_path, CACHE_CONFIG)
    rollout = two_turns(tmp_path, idle=42 * 60, cached=8320)

    out = stop_at(tmp_path, monkeypatch, rollout)

    assert out["systemMessage"].split("\n") == ["cold 42m 30", f"warm {clock(CACHE_NOW - 55 + 1800)} 30"]


def test_default_cache_lines_follow_band_warnings(tmp_path, monkeypatch):
    legacy_config(tmp_path)
    rollout = two_turns(tmp_path, idle=42 * 60, cached=8320, extra=(CAPTURED,))

    out = stop_at(tmp_path, monkeypatch, rollout)

    assert out["systemMessage"].split("\n") == [
        WK_MESSAGE,
        "🌊 This turn started on a cold prompt cache (idle 42m)",
        f"🧊 Prompt cache warm until {clock(CACHE_NOW - 55 + 1800)} (30m TTL)",
    ]


def test_low_reuse_after_a_compaction_is_not_reported_cold(tmp_path, monkeypatch):
    write_config(tmp_path, CACHE_CONFIG)
    rollout = two_turns(tmp_path, idle=600, cached=8320, between=(compacted(CACHE_NOW - 600),))

    out = stop_at(tmp_path, monkeypatch, rollout)

    assert out == {"systemMessage": f"warm {clock(CACHE_NOW - 55 + 1800)} 30"}


def test_only_the_turns_first_request_decides_cold(tmp_path, monkeypatch):
    write_config(tmp_path, CACHE_CONFIG)
    rollout = two_turns(tmp_path, idle=60, cached=22144, extra=(usage_record(CACHE_NOW - 30, "b", 90000, 4000),))

    out = stop_at(tmp_path, monkeypatch, rollout)

    assert out == {"systemMessage": f"warm {clock(CACHE_NOW - 30 + 1800)} 30"}


def test_first_turn_of_a_session_is_never_reported_cold(tmp_path, monkeypatch):
    write_config(tmp_path, CACHE_CONFIG)
    rollout = write_rollout(tmp_path / "r.jsonl", [task_started(CACHE_NOW - 20, "a"), usage_record(CACHE_NOW - 10, "a", 22323, 0)])

    out = stop_at(tmp_path, monkeypatch, rollout)

    assert out == {"systemMessage": f"warm {clock(CACHE_NOW - 10 + 1800)} 30"}


def test_expired_cache_gets_no_warm_line(tmp_path, monkeypatch):
    write_config(tmp_path, {**CACHE_CONFIG, "cache_ttl_minutes": 0.5})
    rollout = two_turns(tmp_path, idle=120, cached=22144)

    assert stop_at(tmp_path, monkeypatch, rollout) == {}


def test_configured_ttl_moves_the_expiry(tmp_path, monkeypatch):
    write_config(tmp_path, {**CACHE_CONFIG, "cache_ttl_minutes": 60})
    rollout = two_turns(tmp_path, idle=120, cached=22144)

    out = stop_at(tmp_path, monkeypatch, rollout, now=CACHE_NOW + 1800)

    assert out == {"systemMessage": f"warm {clock(CACHE_NOW - 55 + 3600)} 60"}


def test_expiry_on_another_day_carries_the_date(tmp_path, monkeypatch):
    write_config(tmp_path, {**CACHE_CONFIG, "cache_ttl_minutes": 1440})
    rollout = two_turns(tmp_path, idle=120, cached=22144)

    out = stop_at(tmp_path, monkeypatch, rollout)

    assert out == {"systemMessage": f"warm {datetime.fromtimestamp(CACHE_NOW - 55 + 86400).strftime('%m-%d %H:%M')} 1440"}


def test_cache_notices_false_keeps_band_warnings_only(tmp_path, monkeypatch):
    write_config(tmp_path, {**LEGACY_CONFIG, "cache_notices": False})
    rollout = two_turns(tmp_path, idle=42 * 60, cached=8320, extra=(CAPTURED,))

    assert stop_at(tmp_path, monkeypatch, rollout) == {"systemMessage": WK_MESSAGE}


@pytest.mark.parametrize("key, value", [
    ("cache_ttl_minutes", 0),
    ("cache_ttl_minutes", -5),
    ("cache_ttl_minutes", True),
    ("cache_ttl_minutes", "30"),
    ("cache_ttl_minutes", 1441),
    ("cache_ttl_minutes", 10 ** 400),
    ("cache_notices", "yes"),
    ("cache_warm_message", "   "),
    ("cache_cold_message", 5),
])
def test_invalid_cache_setting_falls_back_to_default_with_notice(tmp_path, monkeypatch, key, value):
    write_config(tmp_path, {key: value})
    rollout = two_turns(tmp_path, idle=42 * 60, cached=8320)

    out = stop_at(tmp_path, monkeypatch, rollout)

    assert assert_single_notice(out["systemMessage"], key) == [
        "🌊 This turn started on a cold prompt cache (idle 42m)",
        f"🧊 Prompt cache warm until {clock(CACHE_NOW - 55 + 1800)} (30m TTL)",
    ]


def test_malformed_usage_records_are_skipped(tmp_path, monkeypatch):
    write_config(tmp_path, CACHE_CONFIG)
    broken = json.loads(usage_record(CACHE_NOW - 5, "b", 1, 0))
    broken["payload"]["usage"] = None
    rollout = two_turns(tmp_path, idle=120, cached=22144, extra=(json.dumps(broken), '{"token_usage_record": [[['))

    out = stop_at(tmp_path, monkeypatch, rollout)

    assert out == {"systemMessage": f"warm {clock(CACHE_NOW - 55 + 1800)} 30"}


def test_newer_turn_without_a_request_does_not_repeat_the_previous_cold_line(tmp_path, monkeypatch):
    write_config(tmp_path, CACHE_CONFIG)
    rollout = two_turns(tmp_path, idle=42 * 60, cached=8320, extra=(task_started(CACHE_NOW - 20, "c"),))

    out = stop_at(tmp_path, monkeypatch, rollout)

    assert out == {"systemMessage": f"warm {clock(CACHE_NOW - 55 + 1800)} 30"}


def test_refired_stop_in_the_same_turn_drops_the_cold_line(tmp_path, monkeypatch):
    write_config(tmp_path, CACHE_CONFIG)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "home"))
    rollout = two_turns(tmp_path, idle=42 * 60, cached=8320)
    data = {"session_id": "s1", "hook_event_name": "Stop", "transcript_path": str(rollout), "stop_hook_active": True}

    out = load_module().run(data, CACHE_NOW)

    assert out == {"systemMessage": f"warm {clock(CACHE_NOW - 55 + 1800)} 30"}


def test_idle_runs_to_the_turn_start_not_to_its_first_response(tmp_path, monkeypatch):
    write_config(tmp_path, CACHE_CONFIG)
    rollout = write_rollout(tmp_path / "r.jsonl", [
        usage_record(CACHE_NOW - 3600, "a", 22323, 0),
        task_started(CACHE_NOW - 1200, "b"), usage_record(CACHE_NOW - 600, "b", 22392, 8320),
    ])

    out = stop_at(tmp_path, monkeypatch, rollout)

    assert out["systemMessage"].split("\n")[0] == "cold 40m 30"


def test_compaction_after_the_turns_first_request_keeps_the_cold_line(tmp_path, monkeypatch):
    write_config(tmp_path, CACHE_CONFIG)
    rollout = two_turns(
        tmp_path, idle=42 * 60, cached=8320, extra=(compacted(CACHE_NOW - 50), usage_record(CACHE_NOW - 40, "b", 9000, 8320)),
    )

    out = stop_at(tmp_path, monkeypatch, rollout)

    assert out == {"systemMessage": f"cold 42m 30\nwarm {clock(CACHE_NOW - 40 + 1800)} 30"}


def test_compaction_after_the_newest_request_drops_the_warm_line(tmp_path, monkeypatch):
    write_config(tmp_path, CACHE_CONFIG)
    rollout = two_turns(tmp_path, idle=120, cached=22144, extra=(compacted(CACHE_NOW - 50),))

    assert stop_at(tmp_path, monkeypatch, rollout) == {}


def test_prompt_shorter_than_the_previous_one_is_not_cold_when_fully_reused(tmp_path, monkeypatch):
    write_config(tmp_path, CACHE_CONFIG)
    rollout = two_turns(tmp_path, idle=120, cached=19840, previous_input=100000, own_input=20000)

    out = stop_at(tmp_path, monkeypatch, rollout)

    assert out == {"systemMessage": f"warm {clock(CACHE_NOW - 55 + 1800)} 30"}


@pytest.mark.parametrize("usage", [{}, {"input_tokens": 5, "cached_input_tokens": -1}, {"input_tokens": True,
                                                                                        "cached_input_tokens": 0}])
def test_usage_record_without_valid_token_counts_is_skipped(tmp_path, monkeypatch, usage):
    write_config(tmp_path, CACHE_CONFIG)
    broken = json.loads(usage_record(CACHE_NOW + 590, "b", 1, 0))
    broken["payload"]["usage"] = usage
    rollout = two_turns(tmp_path, idle=120, cached=22144, extra=(json.dumps(broken),))

    out = stop_at(tmp_path, monkeypatch, rollout, now=CACHE_NOW + 600)

    assert out == {"systemMessage": f"warm {clock(CACHE_NOW - 55 + 1800)} 30"}


def test_cold_after_under_half_a_minute_reads_less_than_one_minute(tmp_path, monkeypatch):
    write_config(tmp_path, CACHE_CONFIG)
    rollout = two_turns(tmp_path, idle=10, cached=8320)

    out = stop_at(tmp_path, monkeypatch, rollout)

    assert out["systemMessage"].split("\n")[0] == "cold <1m 30"


def test_stop_process_on_a_fresh_rollout_shows_the_default_warm_line(tmp_path):
    now = datetime.now(timezone.utc).timestamp()
    rollout = write_rollout(tmp_path / "r.jsonl", [task_started(now - 20, "a"), usage_record(now - 10, "a", 22323, 0)])

    out = run_stop(tmp_path, rollout)

    assert set(out) == {"systemMessage"}
    assert re.fullmatch(r"🧊 Prompt cache warm until \d\d:\d\d \(30m TTL\)", out["systemMessage"])
