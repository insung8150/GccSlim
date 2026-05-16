#!/usr/bin/env python3
"""Import a Codex JSONL session into a new Claude Code session.

This creates a derived Claude session. It never edits the source Codex JSONL.
The generated session is a semantic replay: conversion summary + Codex
user/assistant message text. Codex runtime/tool internals are referenced, not
recreated as Claude tool_use/tool_result rows.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CODEX_SESSIONS = Path.home() / ".codex" / "sessions"
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)


@dataclass
class CodexMsg:
    line: int
    role: str
    timestamp: str
    text: str


@dataclass
class ImportResult:
    sid: str
    jsonl: Path
    lines: int
    source_sid: str
    source_jsonl: Path
    semantic_count: int


def _utc_now() -> tuple[datetime, str]:
    now = datetime.now(timezone.utc)
    return now, now.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def cwd_to_slug(cwd: str) -> str:
    out: list[str] = []
    for ch in cwd:
        if ch == "/" or ch == "_" or ord(ch) > 127:
            out.append("-")
        else:
            out.append(ch)
    return "".join(out)


def find_codex_jsonl(session_id_or_prefix: str) -> Path:
    matches: list[Path] = []
    for p in CODEX_SESSIONS.glob("**/*.jsonl"):
        if ".bak." in p.name:
            continue
        sid = _codex_sid_from_path(p)
        if sid and sid.startswith(session_id_or_prefix):
            matches.append(p)
    if not matches:
        raise FileNotFoundError(f"Codex JSONL not found for sid/prefix: {session_id_or_prefix}")
    matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0]


def _codex_sid_from_path(path: Path) -> str | None:
    m = UUID_RE.search(path.stem)
    return m.group(0) if m else None


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, dict):
        text = content.get("text")
        return text.strip() if isinstance(text, str) else ""
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
            continue
        if not isinstance(item, dict):
            continue
        typ = item.get("type")
        if typ in {"input_text", "output_text", "text"}:
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    return "\n".join(parts).strip()


def _is_internal_user(text: str) -> bool:
    stripped = text.strip()
    return (
        not stripped
        or stripped.startswith("# AGENTS.md")
        or stripped.startswith("<environment_context>")
        or stripped.startswith("<turn_aborted>")
    )


def _parse_codex(source: Path, now_ts: str) -> tuple[
    str,
    str,
    str,
    dict[str, int],
    int,
    list[CodexMsg],
    list[CodexMsg],
    int,
    int,
]:
    source_sid = ""
    cwd = os.getcwd()
    originator = "codex"
    counts: dict[str, int] = {}
    bad = 0
    response_messages: list[CodexMsg] = []
    event_messages: list[CodexMsg] = []
    function_calls = 0
    function_outputs = 0

    with source.open("r", encoding="utf-8", errors="replace") as fh:
        for line_no, raw in enumerate(fh):
            try:
                obj = json.loads(raw)
            except Exception:
                bad += 1
                continue
            typ = obj.get("type")
            counts[typ] = counts.get(typ, 0) + 1
            payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
            timestamp = str(obj.get("timestamp") or now_ts)

            if typ == "session_meta":
                source_sid = str(payload.get("id") or source_sid)
                cwd = str(payload.get("cwd") or cwd)
                originator = str(payload.get("originator") or originator)
                continue

            if typ == "turn_context":
                cwd = str(payload.get("cwd") or cwd)
                continue

            if typ == "event_msg":
                ev_type = payload.get("type")
                msg = payload.get("message")
                if isinstance(msg, str) and msg.strip() and ev_type in {"user_message", "agent_message"}:
                    role = "user" if ev_type == "user_message" else "assistant"
                    event_messages.append(CodexMsg(line_no, role, timestamp, msg.strip()))
                continue

            if typ != "response_item":
                continue
            ptype = payload.get("type")
            if ptype == "function_call":
                function_calls += 1
                continue
            if ptype == "function_call_output":
                function_outputs += 1
                continue
            if ptype != "message":
                continue
            role = payload.get("role")
            if role not in {"user", "assistant"}:
                continue
            text = _content_text(payload.get("content"))
            if role == "user" and _is_internal_user(text):
                continue
            if not text:
                continue
            response_messages.append(CodexMsg(line_no, str(role), timestamp, text))

    if not source_sid:
        source_sid = _codex_sid_from_path(source) or ""
    semantic = response_messages if response_messages else event_messages
    return source_sid, cwd, originator, counts, bad, semantic, event_messages, function_calls, function_outputs


def _git_branch(cwd: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=cwd if Path(cwd).exists() else None,
            text=True,
            capture_output=True,
            timeout=2,
        )
        branch = proc.stdout.strip()
        return branch or "main"
    except Exception:
        return "main"


def _base_obj(
    *,
    typ: str,
    sid: str,
    cwd: str,
    ts: str,
    parent: str | None,
    git_branch: str,
) -> dict[str, Any]:
    return {
        "type": typ,
        "uuid": str(uuid.uuid4()),
        "parentUuid": parent,
        "isSidechain": False,
        "userType": "external",
        "entrypoint": "cli",
        "cwd": cwd,
        "sessionId": sid,
        "version": "2.1.140",
        "gitBranch": git_branch,
        "timestamp": ts,
    }


def _claude_user(sid: str, cwd: str, ts: str, parent: str | None, text: str, git_branch: str) -> dict[str, Any]:
    obj = _base_obj(typ="user", sid=sid, cwd=cwd, ts=ts, parent=parent, git_branch=git_branch)
    obj["promptId"] = str(uuid.uuid4())
    obj["message"] = {"role": "user", "content": text}
    return obj


def _claude_assistant(sid: str, cwd: str, ts: str, parent: str | None, text: str, git_branch: str) -> dict[str, Any]:
    obj = _base_obj(typ="assistant", sid=sid, cwd=cwd, ts=ts, parent=parent, git_branch=git_branch)
    obj["message"] = {
        "id": str(uuid.uuid4()),
        "type": "message",
        "role": "assistant",
        "model": "<synthetic>",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }
    return obj


def _dump_jsonl(obj: dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"


def build_claude_session(source: Path, *, max_semantic: int = 2000) -> ImportResult:
    now, now_ts = _utc_now()
    sid = str(uuid.uuid4())
    (
        source_sid,
        cwd,
        originator,
        counts,
        bad,
        semantic,
        event_messages,
        function_calls,
        function_outputs,
    ) = _parse_codex(source, now_ts)

    if max_semantic > 0 and len(semantic) > max_semantic:
        semantic = semantic[-max_semantic:]

    out_dir = CLAUDE_PROJECTS / cwd_to_slug(cwd)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{sid}.jsonl"
    git_branch = _git_branch(cwd)

    summary = f"""# Codex 세션 → Claude 파생 세션

이 세션은 Codex 원본 세션을 Claude Code에서 열기 위해 새로 만든 파생 세션이다. 원본 Codex JSONL은 수정하지 않았다.

원본 Codex SID: {source_sid}
원본 Codex JSONL: {source}
원본 originator: {originator}
작업 cwd: {cwd}
생성 시각: {now_ts}

## 직접 파싱 수치
row type counts: {counts}
파싱 실패 라인: {bad}
semantic message 수(response_item 우선): {len(semantic)}
event user/assistant message 수: {len(event_messages)}
function_call 수: {function_calls}
function_call_output 수: {function_outputs}

## 이식 정책
- Codex response_item user/assistant 본문을 Claude user/assistant row로 재구성했다.
- response_item이 있으면 event_msg 중복 메시지는 모델 컨텍스트에 넣지 않았다.
- Codex tool/runtime/apply_patch/exec 내부 상태는 Claude tool_use/tool_result로 억지 변환하지 않았다.
- 정확한 도구 출력이나 런타임 이벤트가 필요하면 원본 Codex JSONL을 직접 검색해야 한다.
"""

    prev: str | None = None
    first = _claude_user(sid, cwd, now_ts, prev, summary, git_branch)
    rows: list[dict[str, Any]] = [first]
    prev = first["uuid"]
    ack = _claude_assistant(
        sid,
        cwd,
        now_ts,
        prev,
        "Codex 세션을 Claude 파생 세션으로 이식했습니다. 원본 Codex JSONL은 보존되어 있으며, 도구 런타임 세부사항은 원본에서 확인해야 합니다.",
        git_branch,
    )
    rows.append(ack)
    prev = ack["uuid"]

    for msg in semantic:
        if msg.role == "assistant":
            row = _claude_assistant(sid, cwd, msg.timestamp, prev, msg.text, git_branch)
        else:
            row = _claude_user(sid, cwd, msg.timestamp, prev, msg.text, git_branch)
        rows.append(row)
        prev = row["uuid"]

    title = f"Codex import <= {source_sid[:8] or source.stem[:8]}"
    rows.extend(
        [
            {"type": "last-prompt", "lastPrompt": "Codex 세션을 Claude로 이식", "leafUuid": prev, "sessionId": sid},
            {"type": "custom-title", "customTitle": title, "sessionId": sid},
            {"type": "agent-name", "agentName": title, "sessionId": sid},
            {"type": "permission-mode", "permissionMode": "bypassPermissions", "sessionId": sid},
        ]
    )

    with out.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(_dump_jsonl(row))

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
        lines=len(rows),
        source_sid=source_sid,
        source_jsonl=source,
        semantic_count=len(semantic),
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Create a derived Claude Code session from a Codex JSONL.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--codex-sid", help="Codex session id or prefix, e.g. 019e2012")
    src.add_argument("--codex-jsonl", type=Path, help="Path to source Codex JSONL")
    ap.add_argument("--max-semantic", type=int, default=2000, help="Max semantic user/assistant messages to replay")
    ap.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    args = ap.parse_args(argv)

    source = args.codex_jsonl if args.codex_jsonl else find_codex_jsonl(args.codex_sid)
    result = build_claude_session(source, max_semantic=args.max_semantic)
    if args.json:
        print(
            json.dumps(
                {
                    "sid": result.sid,
                    "jsonl": str(result.jsonl),
                    "lines": result.lines,
                    "source_sid": result.source_sid,
                    "source_jsonl": str(result.source_jsonl),
                    "semantic_count": result.semantic_count,
                    "resume": f"claude --resume {result.sid}",
                },
                ensure_ascii=False,
            )
        )
    else:
        print(f"Claude SID: {result.sid}")
        print(f"Claude JSONL: {result.jsonl}")
        print(f"Source Codex SID: {result.source_sid}")
        print(f"Source Codex JSONL: {result.source_jsonl}")
        print(f"Generated lines: {result.lines}")
        print(f"Semantic replay messages: {result.semantic_count}")
        print()
        print("Open:")
        print(f"  cd {Path.cwd()}")
        print(f"  claude --resume {result.sid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
