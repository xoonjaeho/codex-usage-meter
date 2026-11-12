from __future__ import annotations

import copy
import importlib.util
import json
import os
import re
import subprocess
import sys
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
WK_MESSAGE = "⚠ Codex wk 74% — prefer engines on another pool"


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
    def prompt(tokens: int) -> dict:
        rollout = write_rollout(tmp_path / "r.jsonl", [event(tokens=tokens, secondary=10.0)])
        return run_stop(tmp_path, rollout)

    first = prompt(130000)  # 27.4%
    again = prompt(140000)  # still band 25
    higher = prompt(250000)  # 52.6%
    dropped = prompt(10000)
    recross = prompt(130000)

    assert first["systemMessage"] == "⚠ ctx 27.4% crossed — protect: delegate reads, tighten output caps"
    assert again == {}
    assert higher["systemMessage"] == "⚠ ctx 52.6% crossed — /compact after the current task"
    assert dropped == {}
    assert recross["systemMessage"] == first["systemMessage"]


def test_ctx_and_quota_messages_are_joined_by_newline(tmp_path):
    rollout = write_rollout(tmp_path / "r.jsonl", [event(tokens=400000, primary=95.0, secondary=70.0)])

    out = run_stop(tmp_path, rollout)

    assert out["systemMessage"] == (
        "⚠ ctx 84.2% crossed — stop at a clean point, then /compact or /new\n"
        "⚠ Codex 5h 95% — orchestrator only, delegate execution\n"
        "⚠ Codex wk 70% — prefer engines on another pool"
    )


def test_unknown_info_leaves_context_band_untouched(tmp_path):
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
    rollout = write_rollout(tmp_path / "r.jsonl", [CAPTURED])

    run_stop(tmp_path, rollout, session="abc")

    state = json.loads((tmp_path / "home" / "codex-usage-meter" / "state" / "abc.json").read_text(encoding="utf-8"))
    assert state == {"ctx": 0, "quota": {"5h": 0, "wk": 70}}


def test_corrupt_state_is_treated_as_fresh(tmp_path):
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
    assert quota == {"60m": 12.0, "wk": 74.0}


def test_context_percent_is_capped_at_window(tmp_path):
    rollout = write_rollout(tmp_path / "r.jsonl", [event(tokens=600000, secondary=10.0)])

    out, _ = run_hook(tmp_path, {"transcript_path": str(rollout)})

    assert context_of(out).startswith("ctx 100.0% (600,000 / 475,000) | ")


def test_quota_band_does_not_fire_on_drop_and_fires_again_on_recross(tmp_path):
    def prompt(secondary: float) -> dict:
        rollout = write_rollout(tmp_path / "r.jsonl", [event(tokens=1000, secondary=secondary)])
        return run_stop(tmp_path, rollout)

    first, same, dropped, recross = prompt(74.0), prompt(75.0), prompt(10.0), prompt(92.0)

    assert first["systemMessage"] == WK_MESSAGE
    assert same == {}
    assert dropped == {}
    assert recross["systemMessage"] == "⚠ Codex wk 92% — orchestrator only, delegate execution"


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
    state_dir = tmp_path / "home" / "codex-usage-meter" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "s1.json").write_text('{"ctx": null, "quota": {"wk": "70"}}', encoding="utf-8")
    rollout = write_rollout(tmp_path / "r.jsonl", [CAPTURED])

    out = run_stop(tmp_path, rollout)

    assert out == {"systemMessage": WK_MESSAGE}
    assert json.loads((state_dir / "s1.json").read_text(encoding="utf-8")) == {"ctx": 0, "quota": {"5h": 0, "wk": 70}}


def test_save_failure_keeps_message_and_leaves_no_temp_file(tmp_path):
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
    first = run_stop(tmp_path, ctx_rollout(tmp_path, 380000))  # 80.0%, band 75
    prompt, _ = run_hook(tmp_path, {"transcript_path": str(ctx_rollout(tmp_path, 261250))})  # 55.0%, band 50
    lowered = stored_state(tmp_path)["ctx"]
    second = run_stop(tmp_path, ctx_rollout(tmp_path, 361000))  # 76.0%, band 75 again

    assert first == {"systemMessage": "⚠ ctx 80.0% crossed — stop at a clean point, then /compact or /new"}
    assert set(prompt) == {"hookSpecificOutput"}
    assert lowered == 50
    assert second == {"systemMessage": "⚠ ctx 76.0% crossed — stop at a clean point, then /compact or /new"}


def test_prompt_after_quota_drop_rearms_quota_warning_for_next_stop(tmp_path):
    first = run_stop(tmp_path, ctx_rollout(tmp_path, 1000, secondary=92.0))
    run_hook(tmp_path, {"transcript_path": str(ctx_rollout(tmp_path, 1000, secondary=10.0))})
    second = run_stop(tmp_path, ctx_rollout(tmp_path, 1000, secondary=92.0))

    assert first == second == {"systemMessage": "⚠ Codex wk 92% — orchestrator only, delegate execution"}


def test_prompt_lowers_quota_to_the_current_band_not_below(tmp_path):
    run_stop(tmp_path, ctx_rollout(tmp_path, 1000, secondary=92.0))
    run_hook(tmp_path, {"transcript_path": str(ctx_rollout(tmp_path, 1000, secondary=75.0))})
    lowered = stored_state(tmp_path)["quota"]["wk"]
    same_band = run_stop(tmp_path, ctx_rollout(tmp_path, 1000, secondary=80.0))

    assert lowered == 70
    assert same_band == {}


def test_prompt_seeing_a_higher_band_leaves_stored_band_and_next_stop_still_fires(tmp_path):
    first = run_stop(tmp_path, ctx_rollout(tmp_path, 150000))  # 31.6%, band 25
    prompt, _ = run_hook(tmp_path, {"transcript_path": str(ctx_rollout(tmp_path, 285000))})  # 60.0%, band 50
    stored = stored_state(tmp_path)["ctx"]
    second = run_stop(tmp_path, ctx_rollout(tmp_path, 285000))

    assert first == {"systemMessage": "⚠ ctx 31.6% crossed — protect: delegate reads, tighten output caps"}
    assert set(prompt) == {"hookSpecificOutput"}
    assert stored == 25
    assert second == {"systemMessage": "⚠ ctx 60.0% crossed — /compact after the current task"}


def test_prompt_with_agent_id_does_not_lower_bands(tmp_path):
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
