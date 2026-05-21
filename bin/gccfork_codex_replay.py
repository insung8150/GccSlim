#!/usr/bin/env python3
"""Replay a Codex JSONL transcript into a fresh terminal scrollback.

This is display-only. It does not modify the JSONL and does not affect what
Codex receives during `codex resume`; Codex still reads the session file itself.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text") or item.get("input_text") or item.get("output_text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(p for p in parts if p)
    return ""


def _print_block(prefix: str, text: str) -> None:
    text = (text or "").rstrip()
    if not text:
        return
    print(prefix)
    print(text)
    print()


def _format_cmd(command: Any) -> str:
    if isinstance(command, list):
        return " ".join(shlex.quote(str(x)) for x in command)
    if isinstance(command, str):
        return command
    return ""


def replay(path: Path, *, include_tools: bool = True) -> int:
    try:
        fh = path.open("r", encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"[GccSlim replay] 세션 파일을 열 수 없습니다: {exc}", file=sys.stderr)
        return 1

    brand = os.environ.get("GCCFORK_REPLAY_BRAND") or os.environ.get("GCCSLIM_REPLAY_BRAND") or "GccSlim"
    print()
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"[{brand}] 하드 복제 세션 과거 대화 재생")
    print(f"[{brand}] source: {path}")
    print(f"[{brand}] 아래 내용은 터미널 스크롤백 표시용입니다.")
    print(f"[{brand}] 실제 LLM 컨텍스트는 이어서 실행되는 `codex resume` 이 JSONL에서 읽습니다.")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print()

    seen = 0
    with fh:
        for raw in fh:
            raw = raw.rstrip("\n")
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue

            typ = obj.get("type")
            payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}

            if typ == "session_meta":
                meta = payload
                sid = meta.get("id") or ""
                parent = meta.get("cloned_from_session_id") or meta.get("source_session_id") or ""
                cwd = meta.get("cwd") or ""
                if sid or parent or cwd:
                    lines = []
                    if sid:
                        lines.append(f"session: {sid}")
                    if parent:
                        lines.append(f"cloned from: {parent}")
                    if cwd:
                        lines.append(f"cwd: {cwd}")
                    _print_block("[session]", "\n".join(lines))
                continue

            if typ == "event_msg":
                ptype = payload.get("type")
                if ptype == "user_message":
                    _print_block("› user", str(payload.get("message") or ""))
                    seen += 1
                elif ptype == "agent_message":
                    phase = payload.get("phase") or "assistant"
                    _print_block(f"• {phase}", str(payload.get("message") or ""))
                    seen += 1
                elif include_tools and ptype in {
                    "exec_command_end",
                    "function_call_output",
                    "tool_output",
                }:
                    command = _format_cmd(payload.get("command"))
                    output = (
                        payload.get("aggregated_output")
                        or payload.get("output")
                        or payload.get("stdout")
                        or ""
                    )
                    if command:
                        _print_block("▶ tool", command)
                    if output:
                        _print_block("  output", str(output))
                continue

            if typ == "response_item":
                ptype = payload.get("type")
                if include_tools and ptype == "function_call":
                    name = payload.get("name") or "tool"
                    args = payload.get("arguments") or ""
                    _print_block(f"▶ {name}", str(args))
                continue

    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"[{brand}] 과거 대화 재생 완료: {seen}개 대화 블록")
    print(f"[{brand}] 이제 Codex 세션을 재개합니다.")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("jsonl", type=Path)
    parser.add_argument("--no-tools", action="store_true", help="do not replay tool output blocks")
    args = parser.parse_args(argv)
    return replay(args.jsonl, include_tools=not args.no_tools)


if __name__ == "__main__":
    raise SystemExit(main())
