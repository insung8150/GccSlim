#!/usr/bin/env python3
"""Import a Claude Code JSONL session into a new Codex session.

This intentionally creates a derived Codex session. It never edits the
source Claude JSONL. The generated Codex session is a context-distilled
session: verified structure summary + Claude compact-summary excerpts +
recent semantic raw messages.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
CODEX_SESSIONS = Path.home() / ".codex" / "sessions"


@dataclass
class ClaudeMsg:
    line: int
    role: str
    timestamp: str
    text: str
    is_compact: bool
    saw_tool: bool
    saw_tool_result: bool


@dataclass
class ImportResult:
    sid: str
    jsonl: Path
    lines: int
    source_sid: str
    source_jsonl: Path
    recent_count: int
    recent_span: tuple[int, int] | None
    keep_raw_turns: int
    kept_raw_turns: int


@dataclass
class ClaudeTurn:
    user: ClaudeMsg
    replies: list[ClaudeMsg]


def _utc_now() -> tuple[datetime, str]:
    now = datetime.now(timezone.utc)
    return now, now.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _seoul_date() -> str:
    return datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d")


def find_claude_jsonl(session_id_or_prefix: str) -> Path:
    matches = []
    for p in CLAUDE_PROJECTS.glob(f"**/{session_id_or_prefix}*.jsonl"):
        if ".bak." in p.name:
            continue
        matches.append(p)
    if not matches:
        raise FileNotFoundError(f"Claude JSONL not found for sid/prefix: {session_id_or_prefix}")
    matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0]


def _extract_text_and_flags(obj: dict[str, Any]) -> tuple[str, bool, bool]:
    msg = obj.get("message") or {}
    content = msg.get("content", "")
    texts: list[str] = []
    saw_tool = False
    saw_tool_result = False
    if isinstance(content, str):
        return content.strip(), saw_tool, saw_tool_result
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            typ = block.get("type")
            if typ == "text":
                text = str(block.get("text", "")).strip()
                if text:
                    texts.append(text)
            elif typ == "tool_use":
                saw_tool = True
            elif typ == "tool_result":
                saw_tool_result = True
    return "\n".join(texts).strip(), saw_tool, saw_tool_result


def _row(row_type: str, payload: dict[str, Any], timestamp: str) -> dict[str, Any]:
    return {"timestamp": timestamp, "type": row_type, "payload": payload}


def _msg_content(role: str, text: str) -> list[dict[str, str]]:
    content_type = "output_text" if role == "assistant" else "input_text"
    return [{"type": content_type, "text": text}]


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n...[truncated]"


def _trim_to_user_start(messages: list[ClaudeMsg]) -> list[ClaudeMsg]:
    """Codex TUI replay stability: historical replay must start with a user turn."""
    for idx, msg in enumerate(messages):
        if msg.role == "user":
            return messages[idx:]
    return []


def _is_noisy_local_command(text: str) -> bool:
    """Remove rows harmful to import context, such as Claude local command output."""
    stripped = text.strip()
    if not stripped:
        return True
    if stripped.startswith("<local-command-caveat>"):
        return True
    if stripped.startswith("<local-command-stdout>"):
        return True
    if stripped.startswith("<command-name>"):
        return True
    if "<command-name>/context</command-name>" in stripped:
        return True
    if "Context Usage" in stripped and "Tokens" in stripped:
        return True
    return False


def _build_turns(messages: list[ClaudeMsg]) -> list[ClaudeTurn]:
    """Group Claude semantic messages into turns starting with a user message."""
    turns: list[ClaudeTurn] = []
    current: ClaudeTurn | None = None
    for msg in messages:
        if msg.role == "user":
            current = ClaudeTurn(user=msg, replies=[])
            turns.append(current)
        elif msg.role == "assistant" and current is not None:
            current.replies.append(msg)
    return turns


def _flatten_turns(turns: list[ClaudeTurn]) -> list[ClaudeMsg]:
    out: list[ClaudeMsg] = []
    for turn in turns:
        out.append(turn.user)
        out.extend(turn.replies)
    return out


def _dedupe_messages(messages: list[ClaudeMsg]) -> list[ClaudeMsg]:
    seen: set[int] = set()
    out: list[ClaudeMsg] = []
    for msg in messages:
        if msg.line in seen:
            continue
        seen.add(msg.line)
        out.append(msg)
    return out


def _turn_context_payload(turn_id: str, cwd: str, *, user_instructions: str) -> dict[str, Any]:
    """Build a Codex TurnContextItem that matches the strict Rust schema."""
    developer_instructions = (
        "This is a GccSlim Claude-to-Codex imported session. "
        "Use the developer summary and preserved recent raw turns as the verified context. "
        "Do not claim to know exact details that are not present in this imported history."
    )
    return {
        "turn_id": turn_id,
        "cwd": cwd or os.getcwd(),
        "current_date": _seoul_date(),
        "timezone": "Asia/Seoul",
        "approval_policy": "never",
        "sandbox_policy": {"type": "danger-full-access"},
        "model": "gpt-5.5",
        "personality": "pragmatic",
        "collaboration_mode": {
            "mode": "default",
            "settings": {
                "model": "gpt-5.5",
                "reasoning_effort": "medium",
                "developer_instructions": developer_instructions,
            },
        },
        "realtime_active": False,
        "effort": "medium",
        "summary": "none",
        "user_instructions": user_instructions,
        "truncation_policy": {"mode": "tokens", "limit": 10000},
    }


def _parse_claude(source: Path, now_ts: str) -> tuple[
    list[ClaudeMsg],
    list[int],
    dict[str, int],
    int,
    str,
    str,
    int,
    int,
    int,
]:
    messages: list[ClaudeMsg] = []
    markers: list[int] = []
    counts: dict[str, int] = {}
    bad = 0
    cwd = ""
    source_sid = ""
    raw_text_chars = 0
    tool_use_count = 0
    tool_result_count = 0

    total_lines = 0
    with source.open("r", encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh):
            total_lines = i + 1
            try:
                obj = json.loads(line)
            except Exception:
                bad += 1
                continue
            typ = obj.get("type")
            counts[typ] = counts.get(typ, 0) + 1
            if obj.get("cwd"):
                cwd = str(obj.get("cwd"))
            if obj.get("sessionId"):
                source_sid = str(obj.get("sessionId"))
            if obj.get("isCompactSummary"):
                markers.append(i)
            if typ not in ("user", "assistant"):
                continue
            text, saw_tool, saw_tool_result = _extract_text_and_flags(obj)
            if saw_tool:
                tool_use_count += 1
            if saw_tool_result:
                tool_result_count += 1
            raw_text_chars += len(text)
            role = (obj.get("message") or {}).get("role") or typ
            messages.append(
                ClaudeMsg(
                    line=i,
                    role=str(role),
                    timestamp=str(obj.get("timestamp") or now_ts),
                    text=text,
                    is_compact=bool(obj.get("isCompactSummary")),
                    saw_tool=saw_tool,
                    saw_tool_result=saw_tool_result,
                )
            )
    return (
        messages,
        markers,
        counts,
        bad,
        cwd,
        source_sid,
        raw_text_chars,
        tool_use_count,
        tool_result_count,
        total_lines,
    )


def build_codex_session(
    source: Path,
    *,
    recent_semantic: int = 120,
    keep_raw_turns: int = 5,
    compact_excerpt_chars: int = 1200,
    include_local_commands: bool = False,
) -> ImportResult:
    now, now_ts = _utc_now()
    sid = str(uuid.uuid4())
    first_turn = str(uuid.uuid4())
    out_dir = CODEX_SESSIONS / now.strftime("%Y") / now.strftime("%m") / now.strftime("%d")
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"rollout-{now.strftime('%Y-%m-%dT%H-%M-%S')}-{sid}.jsonl"

    (
        messages,
        markers,
        counts,
        bad,
        cwd,
        source_sid,
        raw_text_chars,
        tool_use_count,
        tool_result_count,
        total_lines,
    ) = _parse_claude(source, now_ts)

    active_start = markers[-1] + 1 if markers else 0
    active_messages = [m for m in messages if m.line >= active_start]

    semantic: list[ClaudeMsg] = []
    compact_summaries: list[ClaudeMsg] = []
    for msg in messages:
        if msg.is_compact:
            if msg.text:
                compact_summaries.append(msg)
            continue
        if not msg.text:
            continue
        if not include_local_commands and _is_noisy_local_command(msg.text):
            continue
        if msg.role == "user" and msg.saw_tool_result:
            continue
        if msg.role == "assistant" and msg.saw_tool and not msg.text:
            continue
        semantic.append(msg)

    semantic_active = [m for m in semantic if m.line >= active_start]
    turns = _build_turns(semantic)
    raw_turns = turns[-keep_raw_turns:] if keep_raw_turns > 0 else []
    raw_messages = _flatten_turns(raw_turns)
    raw_start_line = raw_messages[0].line if raw_messages else None
    context_pool = [m for m in semantic if raw_start_line is None or m.line < raw_start_line]
    context_recent = context_pool[-recent_semantic:] if recent_semantic > 0 else []
    context_recent = _trim_to_user_start(context_recent)
    recent = _dedupe_messages(context_recent + raw_messages)
    recent_blob = "\n".join(m.text for m in recent)
    observed: list[str] = []
    if "fold" in recent_blob.lower() or "\ubcd1\ud569" in recent_blob:
        observed.append("Recent raw includes discussion of merge/split and fold-style merging.")
    if "pristine" in recent_blob or "dirty" in recent_blob:
        observed.append("Recent raw includes discussion of unmerge feasibility by pristine/dirty state.")
    if "linear" in recent_blob and "interleave" in recent_blob:
        observed.append("Recent raw mentions linear/interleave/parallel/common-only/as-sections modes.")
    if not observed:
        observed.append("Specific conclusions from recent raw must be judged only from this session's response_item raw messages.")

    summary = f"""# Claude Session → Codex Verified Import Session

## Identity
This is a derived session created so the original Claude Code session can be
opened in Codex. The original Claude JSONL was not modified.

Original Claude SID: {source_sid}
Original JSONL: {source}
Working cwd: {cwd}
Created at: {now_ts}

## Counts Verified by Direct Parsing
Total JSONL lines: {total_lines}
Parse-failed lines: {bad}
row type counts: {counts}
compact marker lines: {markers}
Active start line after the last compact marker: {active_start}
user/assistant row count: {len(messages)}
active user/assistant row count: {len(active_messages)}
semantic active message count (excluding tool plumbing): {len(semantic_active)}
text volume (excluding tool bodies, text blocks focused): {raw_text_chars} chars
tool call row count: {tool_use_count}
tool result row count: {tool_result_count}
semantic messages imported into Codex: {len(recent)}, Claude lines {recent[0].line if recent else 'n/a'}..{recent[-1].line if recent else 'n/a'}
protected final raw turns: requested {keep_raw_turns} / actual {len(raw_turns)}
protected raw range: Claude lines {raw_messages[0].line if raw_messages else 'n/a'}..{raw_messages[-1].line if raw_messages else 'n/a'}
protected raw composition: user {len(raw_turns)} / assistant {sum(len(t.replies) for t in raw_turns)}
local command output included: {'yes' if include_local_commands else 'no'}
compact summary excerpt chars/marker: {compact_excerpt_chars}

## Directly Verified Structural Conclusions
- Copying the full original raw transcript into Codex 256k is inappropriate. There are {len(messages)} total user/assistant rows and {len(active_messages)} active rows.
- This Codex session is not a full clone; it is a work-index session built from a developer summary, compact summary excerpts, recent semantic context, and protected final raw turns.
- Protected final raw turns preserve user prompts and following assistant replies by turn.
- tool_use/tool_result originals were not inserted into Codex context. Search the original JSONL for exact tool output.
- Local command output such as `/context` is excluded by default.
- Content before the existing compact marker is treated as the older compacted/history area of the original Claude session.

## Recent Raw Observations
"""
    for item in observed:
        summary += f"- {item}\n"

    if compact_summaries:
        summary += "\n## Claude Native Compact Summary Excerpts\n"
        summary += "The following text is excerpted directly from isCompactSummary messages in the original Claude JSONL. Treat it as original compact-summary text, not independently verified test results.\n"
        for msg in compact_summaries:
            summary += f"\n### compact marker line {msg.line}\n"
            summary += _truncate(msg.text, compact_excerpt_chars) + "\n"

    summary += f"""

## Answering Rules
- Do not say this session is a complete clone that remembers the entire original.
- State only directly parsed counts and facts visible in recent raw with certainty.
- If commit hashes, test-pass counts, file-size changes, or similar details are not explicitly in this summary, say they must be rechecked in the original JSONL or repo.
- When original logs, command output, or tool results are needed, tell the user to open and search this file: {source}
"""

    base_instructions = (
        "You are Codex. This session is a verified derived import from a Claude Code JSONL. "
        "Treat the developer summary as the verified map. Do not invent exact counts, commits, "
        "test results, or file sizes unless present in the summary or recent raw messages."
    )

    out_rows: list[dict[str, Any]] = [
        _row(
            "session_meta",
            {
                "id": sid,
                "timestamp": now_ts,
                "cwd": cwd or os.getcwd(),
                "originator": "gccfork-claude-import-verified",
                "cli_version": "0.0.0",
                "source": "gccfork",
                "model_provider": "openai",
                "base_instructions": {"text": base_instructions},
            },
            now_ts,
        ),
        _row(
            "event_msg",
            {
                "type": "task_started",
                "turn_id": first_turn,
                "model_context_window": 256000,
                "collaboration_mode_kind": "default",
            },
            now_ts,
        ),
        _row(
            "response_item",
            {"type": "message", "role": "developer", "content": _msg_content("developer", summary)},
            now_ts,
        ),
        _row(
            "response_item",
            {
                "type": "message",
                "role": "user",
                "content": _msg_content(
                    "user",
                    "This is a derived session imported from Claude into Codex. Continue from the developer summary above and the following recent raw context.",
                ),
            },
            now_ts,
        ),
        _row(
            "turn_context",
            _turn_context_payload(
                first_turn,
                cwd,
                user_instructions="Answer concisely. Say that unverified details must be checked against the original.",
            ),
            now_ts,
        ),
    ]

    current_turn: str | None = first_turn
    current_turn_has_user = False
    current_turn_last_agent: str | None = None
    for msg in recent:
        role = "assistant" if msg.role == "assistant" else "user"
        if role == "user":
            if current_turn and current_turn_has_user:
                out_rows.append(
                    _row(
                        "event_msg",
                        {
                            "type": "task_complete",
                            "turn_id": current_turn,
                            "last_agent_message": current_turn_last_agent,
                        },
                        msg.timestamp,
                    )
                )
                current_turn_last_agent = None
            current_turn = str(uuid.uuid4())
            if current_turn_has_user:
                out_rows.append(
                    _row(
                        "event_msg",
                        {
                            "type": "task_started",
                            "turn_id": current_turn,
                            "model_context_window": 256000,
                            "collaboration_mode_kind": "default",
                        },
                        msg.timestamp,
                    )
                )
                out_rows.append(
                    _row(
                        "turn_context",
                        _turn_context_payload(
                            current_turn,
                            cwd,
                            user_instructions="Answer concisely. Say that unverified details must be checked against the original.",
                        ),
                        msg.timestamp,
                    )
                )
            else:
                current_turn = first_turn
            out_rows.append(
                _row(
                    "response_item",
                    {"type": "message", "role": "user", "content": _msg_content("user", msg.text)},
                    msg.timestamp,
                )
            )
            out_rows.append(
                _row(
                    "event_msg",
                    {
                        "type": "user_message",
                        "message": msg.text,
                        "turn_id": current_turn,
                        "images": [],
                        "local_images": [],
                        "text_elements": [],
                    },
                    msg.timestamp,
                )
            )
            current_turn_has_user = True
        else:
            if not current_turn:
                current_turn = str(uuid.uuid4())
                out_rows.append(
                    _row(
                        "event_msg",
                        {
                            "type": "task_started",
                            "turn_id": current_turn,
                            "model_context_window": 256000,
                            "collaboration_mode_kind": "default",
                        },
                        msg.timestamp,
                    )
                )
            # Codex MessagePhase is a strict enum: "commentary" or
            # "final_answer".  "final" silently fails RolloutLine
            # deserialization, which makes Codex drop assistant rows during
            # resume replay.
            phase = "final_answer"
            out_rows.append(
                _row(
                    "event_msg",
                    {
                        "type": "agent_message",
                        "message": msg.text,
                        "turn_id": current_turn,
                        "phase": phase,
                        "memory_citation": None,
                    },
                    msg.timestamp,
                )
            )
            current_turn_last_agent = msg.text
            out_rows.append(
                _row(
                    "response_item",
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": _msg_content("assistant", msg.text),
                        "phase": phase,
                    },
                    msg.timestamp,
                )
            )

    if current_turn:
        out_rows.append(
            _row(
                "event_msg",
                {
                    "type": "task_complete",
                    "turn_id": current_turn,
                    "last_agent_message": current_turn_last_agent,
                },
                now_ts,
            )
        )

    with out.open("w", encoding="utf-8") as fh:
        for obj in out_rows:
            fh.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")

    bad_out = 0
    with out.open("r", encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            try:
                json.loads(line)
            except Exception as exc:
                print(f"BAD {n}: {exc}", file=sys.stderr)
                bad_out += 1
    if bad_out:
        raise RuntimeError(f"generated invalid JSONL: {bad_out} bad lines")

    return ImportResult(
        sid=sid,
        jsonl=out,
        lines=len(out_rows),
        source_sid=source_sid,
        source_jsonl=source,
        recent_count=len(recent),
        recent_span=(recent[0].line, recent[-1].line) if recent else None,
        keep_raw_turns=keep_raw_turns,
        kept_raw_turns=len(raw_turns),
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Create a derived Codex session from a Claude Code JSONL.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--claude-sid", help="Claude session id or prefix, e.g. ca09")
    src.add_argument("--claude-jsonl", type=Path, help="Path to source Claude JSONL")
    ap.add_argument("--recent-semantic", type=int, default=240, help="Recent semantic user/assistant messages before protected raw turns")
    ap.add_argument("--keep-raw-turns", type=int, default=5, help="Protect the last N user-started turns as raw user+assistant pairs")
    ap.add_argument("--compact-excerpt-chars", type=int, default=4000, help="Chars kept from each Claude compact summary")
    ap.add_argument(
        "--include-local-commands",
        action="store_true",
        help="Include Claude local command wrappers/stdout such as /context output (default: exclude)",
    )
    ap.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    args = ap.parse_args(argv)

    source = args.claude_jsonl if args.claude_jsonl else find_claude_jsonl(args.claude_sid)
    result = build_codex_session(
        source,
        recent_semantic=args.recent_semantic,
        keep_raw_turns=args.keep_raw_turns,
        compact_excerpt_chars=args.compact_excerpt_chars,
        include_local_commands=args.include_local_commands,
    )
    if args.json:
        print(
            json.dumps(
                {
                    "sid": result.sid,
                    "jsonl": str(result.jsonl),
                    "lines": result.lines,
                    "source_sid": result.source_sid,
                    "source_jsonl": str(result.source_jsonl),
                    "recent_count": result.recent_count,
                    "recent_span": result.recent_span,
                    "keep_raw_turns": result.keep_raw_turns,
                    "kept_raw_turns": result.kept_raw_turns,
                    "resume": f"codex resume {result.sid}",
                },
                ensure_ascii=False,
            )
        )
    else:
        print(f"Codex SID: {result.sid}")
        print(f"Codex JSONL: {result.jsonl}")
        print(f"Source Claude SID: {result.source_sid}")
        print(f"Source Claude JSONL: {result.source_jsonl}")
        print(f"Generated lines: {result.lines}")
        print(f"Recent semantic raw: {result.recent_count} {result.recent_span}")
        print()
        print("Open:")
        print(f"  cd {Path.cwd()}")
        print(f"  codex resume {result.sid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
