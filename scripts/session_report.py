"""Compact, read-only report for Codex rollout JSONL (Python 3.10+)."""
from collections import Counter, defaultdict
from datetime import datetime
import json
from pathlib import Path
import re
from statistics import median

_USAGE_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")
_EXIT_CODE = re.compile(r"(?:Process exited with code|Exit code:)\s*(-?\d+)", re.I)
_TRACEBACK = re.compile(r"(?m)^\s*Traceback \(most recent call last\):")
_TRUNCATION = re.compile(r"(?i)(?:output truncated|truncated output|output was truncated|tokens truncated)")
_CMD_STRING = re.compile(r'\b(?:cmd|command)\s*:\s*("(?:\\.|[^"\\])*")')
_SECRET = re.compile(r'(?i)(?:\b(?:sk-[a-z0-9_-]+|ghp_[a-z0-9_]+|github_pat_[a-z0-9_]+)\b)')
_WAIT = re.compile(r"(?:write_stdin|functions\.wait|tools\.wait|\.wait_agent|\bsleep\b)", re.I)


def _seconds(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def _as_text(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _empty_usage():
    return dict.fromkeys(_USAGE_KEYS, 0)


def _add_usage(target, source):
    for k in _USAGE_KEYS:
        target[k] += int(source.get(k) or 0)


def _shorten(value, limit=100):
    text = re.sub(r"\s+", " ", str(value).strip())
    text = _SECRET.sub("[非表示]", text)
    return text[:limit] + ("…" if len(text) > limit else "")


def _command_summary(name, arguments):
    """Describe the operation without sending command bodies or tool outputs."""
    if isinstance(arguments, dict):
        parsed = arguments
        raw = json.dumps(arguments, ensure_ascii=False)
    else:
        raw = _as_text(arguments)
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            parsed = None
    if isinstance(parsed, dict):
        for key in ("cmd", "command", "query"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return _shorten(value)
            if isinstance(value, list) and value:
                return _shorten(" ".join(str(x) for x in value))
    embedded = []
    for match in _CMD_STRING.finditer(raw):
        try:
            embedded.append(json.loads(match.group(1)))
        except ValueError:
            continue
    if embedded:
        result = " / ".join(_shorten(x, 62) for x in embedded[:2])
        if len(embedded) > 2:
            result += f"（他{len(embedded) - 2}件）"
        return _shorten(result, 120)
    if "write_stdin" in raw:
        return "実行中プロセスの待機・出力取得"
    if name == "sleep" or re.search(r"\bsleep\b", raw, re.I):
        return "待機処理"
    return _shorten(raw, 100)


def _brief_tool(tool):
    return {k: tool.get(k) for k in ("id", "line", "name", "summary", "output_chars", "elapsed_s_approx", "exit_code", "error_signal")}


def _output_timeline(tools, total_output_chars, window_size=8):
    windows = []
    for start in range(0, len(tools), window_size):
        group = tools[start:start + window_size]
        output_chars = sum(t["output_chars"] or 0 for t in group)
        windows.append({"from_id": group[0]["id"], "to_id": group[-1]["id"],
                        "output_chars": output_chars,
                        "missing_results": sum(t["output_chars"] is None for t in group)})
    initial = windows[0] if windows else None
    if initial:
        initial = {**initial, "share_percent": round(100 * initial["output_chars"] / total_output_chars, 1)
                   if total_output_chars else 0.0}
    return {"window_size_calls": window_size, "window_count": len(windows),
            "initial_window": initial,
            "largest_windows": sorted(windows, key=lambda w: w["output_chars"], reverse=True)[:3]}


def build_report(path, top=5):
    """Read one rollout once. Candidates are evidence leads, not waste judgments."""
    path = Path(path)
    session = {"id": None, "model": None, "started": None, "ended": None}
    tool_by_call = {}
    tools = []
    turns = {}
    responses = []
    usage = _empty_usage()
    token_source = "unavailable"
    seen_responses = set()
    fallback_usage = None
    malformed = 0
    active_turn = None
    def get_turn(tid):
        if tid not in turns:
            turns[tid] = {"turn_id": tid, "started": None, "ended": None,
                          "duration_s": None, "tool_calls": 0, "tool_output_chars": 0, "responses": 0,
                          "tokens": _empty_usage()}
        return turns[tid]
    with path.open(encoding="utf-8-sig", errors="replace") as stream:
        for lineno, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            at = row.get("timestamp")
            if at:
                session["started"] = session["started"] or at
                session["ended"] = at
            kind = row.get("type")
            data = row.get("payload") or {}
            if kind == "session_meta":
                session["id"] = data.get("id") or data.get("session_id")
            elif kind == "turn_context":
                session["model"] = data.get("model") or session["model"]
            elif kind == "event_msg":
                event = data.get("type")
                if event == "task_started":
                    active_turn = data.get("turn_id") or f"turn-line-{lineno}"
                    get_turn(active_turn)["started"] = at
                elif event == "task_complete":
                    tid = data.get("turn_id") or active_turn
                    if tid is not None:
                        t = get_turn(tid)
                        t["ended"] = at
                        if data.get("duration_ms") is not None:
                            t["duration_s"] = round(data["duration_ms"] / 1000, 2)
                    active_turn = None
                elif event == "token_count":
                    totals = (data.get("info") or {}).get("total_token_usage")
                    if isinstance(totals, dict):
                        fallback_usage = totals
            elif kind == "token_usage_record":
                rid = data.get("response_id")
                record = data.get("usage")
                if not isinstance(record, dict) or (rid and rid in seen_responses):
                    continue
                if rid:
                    seen_responses.add(rid)
                _add_usage(usage, record)
                token_source = "token_usage_record"
                tid = data.get("turn_id") or active_turn
                if tid is not None:
                    t = get_turn(tid)
                    _add_usage(t["tokens"], record)
                    t["responses"] += 1
                responses.append({"line": lineno, "input_tokens": int(record.get("input_tokens") or 0),
                                  "cached_input_tokens": int(record.get("cached_input_tokens") or 0),
                                  "output_tokens": int(record.get("output_tokens") or 0)})
            elif kind == "response_item":
                subtype = data.get("type")
                if subtype in ("function_call", "custom_tool_call"):
                    t = {"id": len(tools) + 1, "line": lineno, "name": data.get("name"),
                         "started": at, "output_chars": None, "elapsed_s_approx": None,
                         "exit_code": None, "error_signal": None, "possible_truncation": False,
                         "possible_wait": False, "signature": None,
                         "summary": _command_summary(data.get("name"), data.get("arguments", data.get("input", ""))),
                         "turn_id": active_turn}
                    arguments = _as_text(data.get("arguments", data.get("input", "")))
                    t["signature"] = (data.get("name"), arguments)
                    t["possible_wait"] = bool(_WAIT.search(arguments))
                    tools.append(t)
                    call_id = data.get("call_id")
                    if call_id:
                        tool_by_call[call_id] = t
                    if active_turn is not None:
                        get_turn(active_turn)["tool_calls"] += 1
                elif subtype in ("function_call_output", "custom_tool_call_output"):
                    t = tool_by_call.get(data.get("call_id"))
                    if t:
                        output = _as_text(data.get("output", ""))
                        t["output_chars"] = len(output)
                        if t["turn_id"] is not None:
                            get_turn(t["turn_id"])["tool_output_chars"] += len(output)
                        match = _EXIT_CODE.search(output)
                        if match:
                            t["exit_code"] = int(match.group(1))
                        if _TRACEBACK.search(output):
                            t["error_signal"] = "python_traceback"
                        t["possible_truncation"] = bool(_TRUNCATION.search(output))
                        start_s, end_s = _seconds(t["started"]), _seconds(at)
                        if start_s is not None and end_s is not None:
                            t["elapsed_s_approx"] = round(max(end_s - start_s, 0), 3)
    if token_source == "unavailable" and fallback_usage is not None:
        usage = {k: int(fallback_usage.get(k) or 0) for k in _USAGE_KEYS}
        token_source = "token_count.total_token_usage"
    input_values = [r["input_tokens"] for r in responses]
    cached_values = [r["cached_input_tokens"] for r in responses]
    total_input = usage["input_tokens"]
    token_summary = {"source": token_source, "usage": usage,
                     "noncached_input_tokens": max(0, total_input - usage["cached_input_tokens"]),
                     "responses": len(responses),
                     "input_per_response": ({"median": median(input_values),
                                              "min": min(input_values),
                                              "max": max(input_values),
                                              "first": input_values[0],
                                              "last": input_values[-1]}
                                             if input_values else None),
                     "cached_input_per_response": ({"median": median(cached_values),
                                                     "first": cached_values[0],
                                                     "last": cached_values[-1]}
                                                    if cached_values else None)}
    sorted_turns = sorted(turns.values(), key=lambda t: t["started"] or t["ended"] or "")
    for t in sorted_turns:
        t["noncached_input_tokens"] = max(0, t["tokens"]["input_tokens"] - t["tokens"]["cached_input_tokens"])
    tool_counts = Counter(t["name"] for t in tools)
    total_output_chars = sum(t["output_chars"] or 0 for t in tools)
    attributed_output_chars = sum(t["tool_output_chars"] for t in sorted_turns)
    failed = [t for t in tools if (t["exit_code"] is not None and t["exit_code"] != 0) or t["error_signal"]]
    truncated = [t for t in tools if t["possible_truncation"]]
    duplicate_groups = defaultdict(list)
    for t in tools:
        duplicate_groups[t["signature"]].append(t["id"])
    repeated = [{"ids": ids[:10], "count": len(ids), "summary": tools[ids[0] - 1]["summary"]}
                for ids in duplicate_groups.values() if len(ids) > 1]
    repeated.sort(key=lambda x: x["count"], reverse=True)
    largest = sorted([t for t in tools if t["output_chars"] is not None],
                     key=lambda t: t["output_chars"], reverse=True)
    slowest = sorted([t for t in tools if t["elapsed_s_approx"] is not None],
                     key=lambda t: t["elapsed_s_approx"], reverse=True)
    return {"session": session, "tokens": token_summary,
            "turns": sorted_turns,
            "tools": {"total": len(tools), "by_name": dict(tool_counts),
                      "output_chars_total": total_output_chars,
                      "explicit_nonzero": sum(t["exit_code"] not in (0, None) for t in tools),
                      "suspected_failures": sum(bool(t["error_signal"]) for t in tools),
                      "possible_truncations": len(truncated),
                      "possible_waits": sum(t["possible_wait"] for t in tools)},
            "output_timeline": {**_output_timeline(tools, total_output_chars),
                                "attributed_to_turns_chars": attributed_output_chars,
                                "unattributed_chars": total_output_chars - attributed_output_chars},
            "candidates": {"largest_outputs": [_brief_tool(t) for t in largest[:top]],
                           "longest_calls": [_brief_tool(t) for t in slowest[:top]],
                           "failures": [_brief_tool(t) for t in failed[:top]],
                           "possible_truncation": [_brief_tool(t) for t in truncated[:top]],
                           "repeated": repeated[:top]},
            "counts": {"largest_outputs": len(largest), "longest_calls": len(slowest),
                       "failures": len(failed), "possible_truncation": len(truncated),
                       "repeated": len(repeated)},
            "malformed_lines": malformed,
            "notes": ["出力文字数とトークン数は別の指標です。",
                      "ツール所要時間は呼び出しから結果までの概算です。",
                      "失敗・重複・待機・切り詰めは調査候補であり、不要な処理の確定ではありません。"]}


def render_report(data, max_total_chars=6000):
    """Limit overall JSON length without silently dropping evidence groups."""
    data = json.loads(json.dumps(data, ensure_ascii=False))
    def encode():
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    if len(encode()) <= max_total_chars:
        data["truncated"] = False
        result = encode()
        if len(result) <= max_total_chars:
            return result
    data["truncated"] = True
    data["omitted"] = {}
    # Keep core measurements; reduce candidate lists and long turn timelines first.
    while len(encode()) > max_total_chars:
        groups = [(len(data["turns"]), "turns")]
        groups += [(len(v), k) for k, v in data["candidates"].items()]
        size, key = max(groups)
        if size == 0:
            break
        if key == "turns":
            # Preserve both ends of the timeline whenever possible.
            data["turns"].pop(len(data["turns"]) // 2 if len(data["turns"]) > 2 else 0)
        else:
            data["candidates"][key].pop()
        data["omitted"][key] = data["omitted"].get(key, 0) + 1
    if len(encode()) > max_total_chars:
        data["notes"] = []
        data["omitted"]["notes"] = "all"
    if len(encode()) > max_total_chars:
        data = {"session": {"id": data["session"]["id"]},
                "tokens": data["tokens"], "tools": data["tools"],
                "truncated": True, "omitted": {"all_detail": True}}
    if len(encode()) > max_total_chars:
        data = {"session_id": data["session"]["id"], "truncated": True,
                "omitted": {"all_except_session": True}}
    return encode()
