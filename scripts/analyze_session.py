#!/usr/bin/env python3
"""Read-only, staged Codex rollout JSONL inspector (Python 3.10+)."""
import argparse
from collections import Counter
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys

from session_report import build_report, render_report


def compact(value, limit=180, preserve_lines=False):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if not preserve_lines:
        text = text.replace("\r", " ").replace("\n", " ")
    return text[:limit] + ("…" if len(text) > limit else "")


def to_text(value):
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def timestamp_seconds(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def read_rollout(path):
    session = {"file": str(path), "session_id": None, "cwd": None, "model": None}
    tools, by_call_id, messages = [], {}, []
    token_records, fallback_usage, seen_responses = [], None, set()
    started, ended, turn_duration_ms = None, None, 0
    errors = 0
    with path.open(encoding="utf-8-sig", errors="replace") as stream:
        for lineno, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                errors += 1
                continue
            kind = rec.get("type")
            data = rec.get("payload") or {}
            at = rec.get("timestamp")
            if at:
                started = started or at
                ended = at
            if kind == "session_meta":
                session["session_id"] = data.get("id") or data.get("session_id")
                session["cwd"] = data.get("cwd")
                session["parent_thread_id"] = data.get("parent_thread_id")
            elif kind == "turn_context":
                session["model"] = data.get("model") or session["model"]
            elif kind == "token_usage_record":
                rid = data.get("response_id")
                usage = data.get("usage")
                if isinstance(usage, dict) and (not rid or rid not in seen_responses):
                    token_records.append(usage)
                    if rid:
                        seen_responses.add(rid)
            elif kind == "event_msg":
                subtype = data.get("type")
                if subtype == "token_count":
                    info = data.get("info") or {}
                    if isinstance(info.get("total_token_usage"), dict):
                        fallback_usage = info["total_token_usage"]
                elif subtype == "task_complete":
                    turn_duration_ms += data.get("duration_ms") or 0
                elif subtype == "user_message":
                    messages.append({"line": lineno, "type": "user", "text": to_text(data.get("message", ""))})
                elif subtype == "agent_message":
                    messages.append({"line": lineno, "type": "assistant", "text": to_text(data.get("message", ""))})
            elif kind == "response_item":
                subtype = data.get("type")
                if subtype in ("function_call", "custom_tool_call"):
                    arguments = data.get("arguments", data.get("input", ""))
                    try:
                        parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
                    except (ValueError, TypeError):
                        parsed = arguments
                    description = parsed.get("cmd") or parsed.get("command") or parsed.get("query") if isinstance(parsed, dict) else parsed
                    item = {"id": len(tools) + 1, "line": lineno, "time": at,
                            "name": data.get("name"), "call_id": data.get("call_id"),
                            "arguments": arguments, "description": to_text(description or arguments),
                            "output_line": None, "output_chars": None, "elapsed_s": None,
                            "exit_code": None, "error_signal": None}
                    tools.append(item)
                    if item["call_id"]:
                        by_call_id[item["call_id"]] = item
                elif subtype in ("function_call_output", "custom_tool_call_output"):
                    item = by_call_id.get(data.get("call_id"))
                    if item:
                        result = to_text(data.get("output", ""))
                        item["output_line"] = lineno
                        item["output_chars"] = len(result)
                        code = re.search(r"(?:Process exited with code|Exit code:)\s*(-?\d+)", result, re.I)
                        if code:
                            item["exit_code"] = int(code.group(1))
                        if re.search(r"(?m)^\s*Traceback \(most recent call last\):", result):
                            item["error_signal"] = "python_traceback"
                        a, b = timestamp_seconds(item["time"]), timestamp_seconds(at)
                        if a is not None and b is not None:
                            item["elapsed_s"] = round(max(0, b - a), 3)
                elif subtype == "message" and data.get("role") == "assistant":
                    parts = data.get("content") or []
                    value = " ".join(x.get("text", "") for x in parts if isinstance(x, dict))
                    if value:
                        messages.append({"line": lineno, "type": "assistant", "text": value})
    if token_records:
        usage = {k: sum(int(x.get(k) or 0) for x in token_records) for k in
                 ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")}
        usage_source = "token_usage_record"
    else:
        usage = fallback_usage
        usage_source = "token_count.total_token_usage" if usage else "unavailable"
    session.update(started=started, ended=ended, turn_duration_s=round(turn_duration_ms / 1000, 2) if turn_duration_ms else None,
                   token_usage=usage, usage_source=usage_source, malformed_lines=errors)
    return session, tools, messages


def short_tool(t):
    return {"id": t["id"], "line": t["line"], "name": t["name"],
            "command": compact(t["description"], 160), "output_chars": t["output_chars"],
            "elapsed_s_approx": t["elapsed_s"], "exit_code": t["exit_code"],
            "error_signal": t["error_signal"]}


def latest_files(n, project=None):
    base = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    files = sorted((base / "sessions").rglob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if project is None:
        return files[:n]
    desired = os.path.normcase(os.path.abspath(project))
    selected = []
    for f in files:
        try:
            with f.open(encoding="utf-8-sig") as stream:
                first = json.loads(stream.readline())
            cwd = (first.get("payload") or {}).get("cwd")
            if cwd and os.path.normcase(os.path.abspath(cwd)) == desired:
                selected.append(f)
        except (OSError, ValueError, TypeError):
            continue
        if len(selected) == n:
            break
    return selected


def overview(path):
    session, tools, _ = read_rollout(path)
    repeats = Counter((t["name"], t["description"]) for t in tools)
    return {**session, "tool_calls": len(tools), "tool_by_name": dict(Counter(t["name"] for t in tools)),
            "nonzero_exits": sum(t["exit_code"] != 0 for t in tools if t["exit_code"] is not None),
            "suspected_failures": sum(t["error_signal"] is not None for t in tools),
            "largest_outputs": [short_tool(t) for t in sorted(tools, key=lambda t: t["output_chars"] or 0, reverse=True)[:5]],
            "repeated_calls": [{"name": name, "command": compact(command, 120), "count": count}
                               for (name, command), count in repeats.most_common(8) if count > 1]}


def fetch_lines(path, wanted):
    found = {}
    with path.open(encoding="utf-8-sig", errors="replace") as stream:
        for i, line in enumerate(stream, 1):
            if i in wanted:
                found[i] = json.loads(line)
            if len(found) == len(wanted):
                break
    return found


def detail(path, call_id, around, max_chars):
    _, tools, messages = read_rollout(path)
    tool = next((t for t in tools if t["id"] == call_id), None)
    if tool is None:
        raise SystemExit(f"Tool ID {call_id} not found (1-{len(tools)})")
    surrounding = [m for m in messages if m["line"] < tool["line"]][-around:] if around else []
    later = [m for m in messages if m["line"] > tool["line"]][:around]
    rows = fetch_lines(path, {tool["line"], tool["output_line"]} - {None})
    result = rows.get(tool["output_line"], {}).get("payload", {}).get("output", "")
    return {"tool": short_tool(tool), "arguments": compact(tool["arguments"], max_chars, True),
            "output": compact(result, max_chars, True),
            "nearby_calls": [short_tool(t) for t in tools[max(0, call_id - around - 1):call_id + around] if t["id"] != call_id],
            "context_before": [{**m, "text": compact(m["text"], 350)} for m in surrounding],
            "context_after": [{**m, "text": compact(m["text"], 350)} for m in later],
            "truncated": len(to_text(result)) > max_chars}




def message_at_line(path, line, max_chars):
    """Retrieve only text from a message at an exact JSONL line."""
    try:
        rec = fetch_lines(path, {line}).get(line)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON at line {line}") from exc
    if rec is None:
        raise SystemExit(f"Line {line} not found")
    data = rec.get("payload") or {}
    role = None
    text = None
    if rec.get("type") == "event_msg":
        if data.get("type") == "agent_message":
            role = "assistant"
        elif data.get("type") == "user_message":
            role = "user"
        if role:
            text = data.get("message")
    elif rec.get("type") == "response_item" and data.get("type") == "message":
        if data.get("role") in ("assistant", "user"):
            role = data["role"]
            content = data.get("content") or []
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                texts = [part["text"] for part in content if isinstance(part, dict)
                         and part.get("type") in ("output_text", "input_text", "text")
                         and isinstance(part.get("text"), str)]
                text = "\n".join(texts)
    if role is None or not isinstance(text, str) or not text:
        raise SystemExit(f"Line {line} is not a text message")
    return {"line": line, "timestamp": rec.get("timestamp"), "role": role,
            "text": compact(text, max_chars, True),
            "original_chars": len(text), "truncated": len(text) > max_chars}


def positive_int(text):
    value = nonnegative_int(text)
    if value == 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def call_ids(text):
    parts = [part.strip() for part in text.split(",")]
    if not parts or any(not part.isdecimal() or int(part) < 1 for part in parts):
        raise argparse.ArgumentTypeError("use comma-separated positive IDs, e.g. 1,3,7")
    return set(map(int, parts))


def select_calls(tools, min_output_chars=0, min_elapsed_s=0, sort_by="sequence",
                 ids=None, from_id=None, to_id=None):
    selected = [t for t in tools
                if (ids is None or t["id"] in ids)
                and (from_id is None or t["id"] >= from_id)
                and (to_id is None or t["id"] <= to_id)
                and (t["output_chars"] or 0) >= min_output_chars
                and (t["elapsed_s"] or 0) >= min_elapsed_s]
    if sort_by == "output":
        selected.sort(key=lambda t: t["output_chars"] or 0, reverse=True)
    elif sort_by == "duration":
        selected.sort(key=lambda t: t["elapsed_s"] or 0, reverse=True)
    return selected


def nonnegative_int(text):
    try:
        value = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if value < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return value


def nonnegative_float(text):
    try:
        value = float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not (0 <= value < float("inf")):
        raise argparse.ArgumentTypeError("must be finite and nonnegative")
    return value


def min_total_chars(text):
    value = nonnegative_int(text)
    if value < 800:
        raise argparse.ArgumentTypeError("must be at least 800")
    return value


def render_detail(data, max_total_chars):
    """Return valid JSON within the requested character budget, showing omissions."""
    def encode():
        return json.dumps(data, ensure_ascii=False, indent=2)

    if len(encode()) <= max_total_chars:
        return encode()
    data["truncated"] = True
    data["omitted_fields"] = []
    # Preserve call identity and command/output evidence before surrounding context.
    for field in ("context_after", "context_before", "nearby_calls"):
        if len(encode()) <= max_total_chars:
            break
        if data[field]:
            data[field] = []
            data["omitted_fields"].append(field)
    for field in ("output", "arguments"):
        while len(encode()) > max_total_chars and len(data[field]) > 128:
            data[field] = compact(data[field], max(128, len(data[field]) // 2), True)
            if field not in data["omitted_fields"]:
                data["omitted_fields"].append(field)
    if len(encode()) > max_total_chars:
        # Even with no context, a very small budget may not fit all metadata.
        data = {"tool": {"id": data["tool"]["id"], "line": data["tool"]["line"],
                         "name": data["tool"]["name"]},
                "truncated": True, "omitted_fields": ["all_except_tool"]}
    return encode()

def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("overview", help="Compact overview of the latest sessions or one file")
    p.add_argument("--last", type=int, default=1)
    p.add_argument("--project", help="Limit to sessions whose recorded cwd matches this path")
    p.add_argument("--file", type=Path)
    p = sub.add_parser("report", help="Bounded one-pass session statistics and investigation candidates")
    p.add_argument("--file", required=True, type=Path)
    p.add_argument("--top", type=int, default=5, help="Maximum candidates in each category (1-20)")
    p.add_argument("--max-total-chars", type=min_total_chars, default=6000)
    p = sub.add_parser("calls", help="List tool calls without tool output bodies")
    p.add_argument("--file", required=True, type=Path)
    p.add_argument("--offset", type=nonnegative_int, default=0)
    p.add_argument("--limit", type=nonnegative_int, default=50)
    p.add_argument("--sort-by", choices=("sequence", "output", "duration"), default="sequence")
    p.add_argument("--min-output-chars", type=nonnegative_int, default=0)
    p.add_argument("--min-elapsed-s", type=nonnegative_float, default=0)
    p.add_argument("--ids", type=call_ids, help="Comma-separated original call IDs, e.g. 1,3,7")
    p.add_argument("--from-id", type=positive_int, help="First original call ID (inclusive)")
    p.add_argument("--to-id", type=positive_int, help="Last original call ID (inclusive)")
    p = sub.add_parser("message", help="Retrieve text of a message at an exact JSONL line")
    p.add_argument("--file", required=True, type=Path)
    p.add_argument("--line", type=positive_int, required=True)
    p.add_argument("--max-chars", type=positive_int, default=1500)
    p = sub.add_parser("detail", help="Fetch one call and its surrounding conversation")
    p.add_argument("--file", required=True, type=Path)
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--around", type=nonnegative_int, default=3)
    p.add_argument("--max-chars", type=nonnegative_int, default=2000)
    p.add_argument("--max-total-chars", type=min_total_chars, default=6000,
                   help="Maximum total characters of detail JSON (default: 6000)")
    args = parser.parse_args()
    if args.action == "overview":
        paths = [args.file] if args.file else latest_files(args.last, args.project)
        if not paths:
            raise SystemExit("No matching rollout JSONL files found")
        for path in paths:
            print(json.dumps(overview(path), ensure_ascii=False))
    elif args.action == "report":
        if not 1 <= args.top <= 20:
            parser.error("--top must be between 1 and 20")
        print(render_report(build_report(args.file, args.top), args.max_total_chars))
    elif args.action == "calls":
        if args.from_id is not None and args.to_id is not None and args.from_id > args.to_id:
            parser.error("--from-id must not exceed --to-id")
        _, tools, _ = read_rollout(args.file)
        selected = select_calls(tools, args.min_output_chars, args.min_elapsed_s,
                                args.sort_by, args.ids, args.from_id, args.to_id)
        page = selected[args.offset: args.offset + args.limit]
        for t in page:
            print(json.dumps(short_tool(t), ensure_ascii=False))
        print(json.dumps({"total_calls": len(tools), "matched_calls": len(selected),
                          "shown": len(page),
                          "next_offset": args.offset + args.limit if args.offset + args.limit < len(selected) else None}))
    elif args.action == "message":
        print(json.dumps(message_at_line(args.file, args.line, args.max_chars), ensure_ascii=False))
    else:
        print(render_detail(detail(args.file, args.id, args.around, args.max_chars),
                            args.max_total_chars))


if __name__ == "__main__":
    main()
