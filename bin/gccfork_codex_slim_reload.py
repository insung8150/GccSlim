"""Slim and reload Codex CLI session JSONL files.

This module is intentionally independent from the Textual fork picker so a
future Codex version of gccfork can import it directly.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any


CODEX_ROOT = Path.home() / ".codex"
SESSIONS_DIR = CODEX_ROOT / "sessions"
BRIDGE_INJECT_DIR = Path.home() / ".claude" / "gccfork-inject-requests"
BRIDGE_STATUS_DIR = Path.home() / ".claude" / "gccfork-inject-status"
SESSION_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)

INTERNAL_USER_PREFIXES = (
    "# AGENTS.md",
    "<environment_context>",
    "<turn_aborted>",
)

SLIM_MODE_ALIASES = {
    "weak": "safe",
    "medium": "safe",
    "safe": "safe",
    "strong": "strong",
    "heavy-strong": "strong",
}

SLIM_MODE_DEFAULT_KEEP_RECENT = {
    "safe": 10,
    "strong": 3,
}

DEFAULT_COMPACT_TOP_LEVEL_TYPES = ("compacted",)
DEFAULT_COMPACT_EVENT_TYPES = (
    "context_compacted",
    "compact_summary",
    "conversation_compacted",
    "auto_compacted",
)
DEFAULT_COMPACT_TEXT_KEYS = (
    "message",
    "summary",
    "text",
    "content",
    "compact_summary",
)


@dataclass(frozen=True)
class JsonlRow:
    raw: bytes
    obj: dict[str, Any] | None
    line_no: int


@dataclass(frozen=True)
class SlimStats:
    kept: int
    stubbed: int
    dropped: int


@dataclass(frozen=True)
class RecentTurnReport:
    turn_no: int
    original_bytes: int
    slim_bytes: int


@dataclass(frozen=True)
class SlimPlan:
    session_id: str
    session_file: Path
    cwd: str | None
    original_bytes: int
    original_lines: int
    slim_bytes: int
    slim_lines: int
    dropped_lines: int
    mode: str
    keep_recent: int
    trim_recent_tools: bool
    total_user_turns: int
    stats: SlimStats
    backup_path: Path
    slim_rows: list[bytes]
    compact_summary_count: int = 0
    compact_detected_types: tuple[str, ...] = ()
    compact_unknown_types: tuple[str, ...] = ()
    recent_turn_reports: tuple[RecentTurnReport, ...] = ()

    @property
    def saved_bytes(self) -> int:
        return self.original_bytes - self.slim_bytes

    @property
    def saved_percent(self) -> float:
        if self.original_bytes <= 0:
            return 0.0
        return self.saved_bytes * 100.0 / self.original_bytes


@dataclass(frozen=True)
class CloneSlimResult:
    source_session_id: str
    cloned_session_id: str
    source_file: Path
    cloned_file: Path
    cloned_bytes: int
    cloned_lines: int


@dataclass(frozen=True)
class CodexProcess:
    pid: int
    ppid: int
    session_id: str | None
    jsonl_path: Path | None
    jsonl_deleted: bool
    tty: str | None
    cmdline: list[str]

    @property
    def command_text(self) -> str:
        return " ".join(self.cmdline)

    @property
    def is_resume_command(self) -> bool:
        return len(self.cmdline) >= 2 and self.cmdline[1] == "resume"


def fmt_size(n: int) -> str:
    if n >= 1_048_576:
        return f"{n / 1_048_576:.1f}MB"
    if n >= 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n}B"


def _load_jsonl_rows(path: Path) -> list[JsonlRow]:
    rows: list[JsonlRow] = []
    with path.open("rb") as handle:
        for index, raw in enumerate(handle, start=1):
            stripped = raw.strip()
            if not stripped:
                rows.append(JsonlRow(raw=raw, obj=None, line_no=index))
                continue
            try:
                obj = json.loads(stripped.decode("utf-8"))
            except json.JSONDecodeError:
                obj = None
            rows.append(JsonlRow(raw=raw, obj=obj, line_no=index))
    return rows


def _dump_jsonl(obj: dict[str, Any]) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n".join(parts).strip()


def _is_internal_user_text(text: str) -> bool:
    stripped = text.strip()
    return not stripped or stripped.startswith(INTERNAL_USER_PREFIXES)


def _session_id_from_rows(rows: list[JsonlRow]) -> str:
    for row in rows:
        obj = row.obj
        if not obj or obj.get("type") != "session_meta":
            continue
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            continue
        session_id = payload.get("id")
        if isinstance(session_id, str) and session_id:
            return session_id
    raise ValueError("session_meta.payload.id was not found.")


def _cwd_from_rows(rows: list[JsonlRow]) -> str | None:
    for row in rows:
        obj = row.obj
        if not obj or obj.get("type") != "session_meta":
            continue
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            continue
        cwd = payload.get("cwd")
        if isinstance(cwd, str) and cwd:
            return cwd
    return None


def _is_real_user_message(obj: dict[str, Any]) -> bool:
    if obj.get("type") != "response_item":
        return False
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        return False
    if payload.get("type") != "message" or payload.get("role") != "user":
        return False
    return not _is_internal_user_text(_extract_text(payload.get("content")))


def _turn_numbers(rows: list[JsonlRow]) -> dict[int, int]:
    """Return line_no -> current real user turn number."""
    current = 0
    mapping: dict[int, int] = {}
    for row in rows:
        obj = row.obj
        if obj and _is_real_user_message(obj):
            current += 1
        mapping[row.line_no] = current
    return mapping


def _truncate_text(text: str, limit: int) -> str:
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[:limit].rstrip() + "\n\n[... codex-slim: older content truncated ...]"


def _content_with_replaced_text(content: Any, text: str) -> list[dict[str, Any]]:
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                replacement = dict(item)
                replacement["text"] = text
                return [replacement]
    return [{"type": "input_text", "text": text}]


def _load_project_prefs(cwd: str | None) -> dict[str, Any]:
    if not cwd:
        return {}
    path = Path(cwd).expanduser() / ".gccfork" / "ccfork-prefs.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _list_pref(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        parts = re.split(r"[\s,]+", value)
    elif isinstance(value, list):
        parts = [str(item) for item in value]
    else:
        parts = [str(value)]
    seen: set[str] = set()
    out: list[str] = []
    for part in parts:
        item = part.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return tuple(out)


def _merge_key_lists(*lists: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for values in lists:
        for value in values:
            item = str(value).strip()
            if not item or item in seen:
                continue
            seen.add(item)
            out.append(item)
    return tuple(out)


def _extract_compact_text_from_value(value: Any, text_keys: tuple[str, ...]) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        text = _extract_text(value)
        if text:
            return text
        for item in value:
            text = _extract_compact_text_from_value(item, text_keys)
            if text:
                return text
        return ""
    if not isinstance(value, dict):
        return ""
    for key in text_keys:
        if key in value:
            text = _extract_compact_text_from_value(value.get(key), text_keys)
            if text:
                return text
    return ""


def normalize_slim_mode(mode: str) -> str:
    normalized = SLIM_MODE_ALIASES.get(str(mode or "").strip(), "")
    if not normalized:
        raise ValueError(f"mode must be one of safe/strong: {mode!r}")
    return normalized


def _extract_compacted_message(
    obj: dict[str, Any],
    *,
    compact_top_level_types: tuple[str, ...],
    compact_event_types: tuple[str, ...],
    compact_text_keys: tuple[str, ...],
) -> tuple[str, str, bool]:
    row_type = str(obj.get("type") or "")
    payload = obj.get("payload")
    if row_type in compact_top_level_types:
        text = _extract_compact_text_from_value(payload, compact_text_keys)
        return text, row_type, True
    if row_type == "event_msg" and isinstance(payload, dict):
        event_type = str(payload.get("type") or "")
        if event_type in compact_event_types:
            text = _extract_compact_text_from_value(payload, compact_text_keys)
            return text, event_type, True
        if "compact" in event_type or "summary" in event_type:
            return "", event_type, False
    return "", "", False


def _compacted_summary_rows(
    rows: list[JsonlRow],
    *,
    cwd: str | None,
) -> tuple[list[bytes], int, tuple[str, ...], tuple[str, ...]]:
    prefs = _load_project_prefs(cwd)
    compact_top_level_types = DEFAULT_COMPACT_TOP_LEVEL_TYPES
    compact_event_types = _merge_key_lists(
        DEFAULT_COMPACT_EVENT_TYPES,
        _list_pref(prefs.get("codex_slim_compact_event_types")),
    )
    compact_text_keys = _merge_key_lists(
        DEFAULT_COMPACT_TEXT_KEYS,
        _list_pref(prefs.get("codex_slim_compact_text_keys")),
    )

    summaries: list[tuple[str, str, str]] = []
    detected_types: set[str] = set()
    unknown_types: set[str] = set()
    for row in rows:
        obj = row.obj
        if not isinstance(obj, dict):
            continue
        message, source_type, recognized = _extract_compacted_message(
            obj,
            compact_top_level_types=compact_top_level_types,
            compact_event_types=compact_event_types,
            compact_text_keys=compact_text_keys,
        )
        if source_type and recognized:
            detected_types.add(source_type)
        elif source_type:
            unknown_types.add(source_type)
        if not message:
            continue
        timestamp = str(obj.get("timestamp") or f"line {row.line_no}")
        summaries.append((timestamp, source_type, message))
    if not summaries:
        return [], 0, tuple(sorted(detected_types)), tuple(sorted(unknown_types))

    parts = [
        "# Accumulated Codex compact summaries",
        "",
        "GccSlim Codex slim collected JSONL compact summary text in chronological order and placed it at the front of the new context.",
        "These summaries preserve, as recoverable plaintext context, content that Codex previously injected dynamically during automatic compaction.",
    ]
    for index, (timestamp, source_type, message) in enumerate(summaries, start=1):
        parts.extend([
            "",
            f"## Compact summary #{index} ({timestamp}, source={source_type})",
            "",
            message,
        ])

    payload = {
        "type": "message",
        "role": "user",
        "content": [
            {
                "type": "input_text",
                "text": "\n".join(parts).strip(),
            }
        ],
        "codex_slim_context": {
            "kind": "accumulated_compact_summaries",
            "count": len(summaries),
        },
    }
    return (
        [_dump_jsonl({"type": "response_item", "payload": payload})],
        len(summaries),
        tuple(sorted(detected_types)),
        tuple(sorted(unknown_types)),
    )


def _stub_message_payload(payload: dict[str, Any], *, mode: str) -> dict[str, Any] | None:
    role = payload.get("role")
    text = _extract_text(payload.get("content"))
    if not text:
        return None

    if mode == "safe":
        if role == "assistant":
            limit = 2200
        elif role == "user":
            limit = 3000
        else:
            limit = 3600
    else:
        if role == "assistant":
            limit = 700
        elif role == "user":
            limit = 900
        else:
            limit = 1200

    shortened = _truncate_text(text, limit)
    if shortened == text:
        return payload

    replacement = dict(payload)
    replacement["content"] = _content_with_replaced_text(payload.get("content"), shortened)
    replacement["codex_slim_stub"] = {
        "mode": mode,
        "reason": "older semantic message shortened",
        "original_chars": len(text),
    }
    return replacement


def _stub_event_msg_payload(payload: dict[str, Any], *, mode: str) -> dict[str, Any] | None:
    event_type = payload.get("type")
    text_key = None
    if event_type == "user_message":
        text_key = "message"
    elif event_type == "agent_message":
        text_key = "message"
    elif event_type == "task_complete":
        text_key = "last_agent_message"
    else:
        return None

    text = payload.get(text_key)
    if not isinstance(text, str) or not text.strip():
        return None

    if mode == "safe":
        limit = 4000
    else:
        limit = 900

    shortened = _truncate_text(text, limit)
    if shortened == text:
        return payload

    replacement = dict(payload)
    replacement[text_key] = shortened
    replacement["codex_slim_stub"] = {
        "mode": mode,
        "reason": "older transcript event shortened",
        "original_chars": len(text),
    }
    return replacement


def _old_row_verdict(row: JsonlRow, mode: str) -> tuple[str, bytes | None]:
    obj = row.obj
    if obj is None:
        return ("DROP", None)

    typ = obj.get("type")
    if typ == "session_meta":
        return ("KEEP", row.raw)

    if typ == "event_msg":
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            return ("DROP", None)
        stubbed_payload = _stub_event_msg_payload(payload, mode=mode)
        if stubbed_payload is None:
            return ("DROP", None)
        if stubbed_payload is payload:
            return ("KEEP", row.raw)
        new_obj = dict(obj)
        new_obj["payload"] = stubbed_payload
        return ("STUB", _dump_jsonl(new_obj))

    if typ != "response_item":
        return ("DROP", None)

    payload = obj.get("payload")
    if not isinstance(payload, dict):
        return ("DROP", None)

    # Keep semantic conversation messages. Drop older tool plumbing,
    # encrypted reasoning blobs, token counts, duplicated event messages, etc.
    if payload.get("type") != "message":
        return ("DROP", None)

    role = payload.get("role")
    if role not in {"system", "developer", "user", "assistant"}:
        return ("DROP", None)

    if role == "user" and _is_internal_user_text(_extract_text(payload.get("content"))):
        # AGENTS.md/environment context can be huge; current run will inject it again.
        return ("DROP", None)

    stubbed_payload = _stub_message_payload(payload, mode=mode)
    if stubbed_payload is None:
        return ("DROP", None)

    # Safe preserves more semantic text; strong stubs old semantic messages
    # outside the recent window to fit Codex's smaller context.
    if stubbed_payload is payload:
        return ("KEEP", row.raw)

    new_obj = dict(obj)
    new_obj["payload"] = stubbed_payload
    return ("STUB", _dump_jsonl(new_obj))


def _recent_row_verdict(
    row: JsonlRow,
    *,
    mode: str,
    turn_no: int,
    latest_turn: int,
    trim_recent_tools: bool,
) -> tuple[str, bytes | None]:
    obj = row.obj
    if obj is None:
        return ("DROP", None)

    if trim_recent_tools:
        typ = obj.get("type")
        payload = obj.get("payload")
        if typ == "event_msg" and isinstance(payload, dict):
            event_type = payload.get("type")
            if event_type in {"user_message", "agent_message", "task_complete"}:
                stubbed_payload = _stub_event_msg_payload(payload, mode=mode)
                if stubbed_payload is payload:
                    return ("KEEP", row.raw)
                if stubbed_payload is not None:
                    new_obj = dict(obj)
                    new_obj["payload"] = stubbed_payload
                    return ("STUB", _dump_jsonl(new_obj))
            return ("DROP", None)
        if typ == "response_item":
            if not isinstance(payload, dict) or payload.get("type") != "message":
                return ("DROP", None)
            role = payload.get("role")
            if role not in {"system", "developer", "user", "assistant"}:
                return ("DROP", None)
            stubbed_payload = _stub_message_payload(payload, mode=mode)
            if stubbed_payload is payload:
                return ("KEEP", row.raw)
            if stubbed_payload is not None:
                new_obj = dict(obj)
                new_obj["payload"] = stubbed_payload
                return ("STUB", _dump_jsonl(new_obj))
        if typ == "session_meta":
            return ("KEEP", row.raw)
        return ("DROP", None)

    # Recent turns are the TUI replay surface. Keep their event/tool/turn_context
    # rows intact only when explicitly requested; otherwise tool output in recent
    # turns can dominate the context and make `/slim` ineffective.
    return ("KEEP", row.raw)


def _validate_jsonl_bytes(lines: list[bytes]) -> None:
    for index, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not stripped:
            continue
        json.loads(stripped.decode("utf-8"))


def find_latest_session_file(codex_root: Path | None = None) -> Path:
    root = (codex_root or CODEX_ROOT) / "sessions"
    candidates = list(root.glob("*/*/*/*.jsonl"))
    if not candidates:
        raise FileNotFoundError(f"No Codex session files found: {root}")
    return max(candidates, key=lambda path: path.stat().st_mtime_ns)


def find_session_file_by_id(session_id: str, codex_root: Path | None = None) -> Path:
    root = (codex_root or CODEX_ROOT) / "sessions"
    matches = [path for path in root.glob("*/*/*/*.jsonl") if session_id in path.name]
    if not matches:
        raise FileNotFoundError(f"No session file found for session_id={session_id!r}.")
    if len(matches) > 1:
        joined = "\n".join(str(path) for path in matches)
        raise FileExistsError(f"Multiple matches for session_id={session_id!r}.\n{joined}")
    return matches[0]


def build_slim_plan(
    session_file: Path,
    *,
    mode: str = "strong",
    keep_recent: int | None = None,
    codex_root: Path | None = None,
    include_compact_summaries: bool = True,
    trim_recent_tools: bool = True,
) -> SlimPlan:
    mode = normalize_slim_mode(mode)
    if keep_recent is None:
        keep_recent = SLIM_MODE_DEFAULT_KEEP_RECENT[mode]
    if keep_recent < 1:
        raise ValueError("keep_recent must be at least 1.")

    session_file = session_file.expanduser().resolve()
    rows = _load_jsonl_rows(session_file)
    session_id = _session_id_from_rows(rows)
    cwd = _cwd_from_rows(rows)
    turn_by_line = _turn_numbers(rows)
    total_turns = max(turn_by_line.values(), default=0)
    recent_start = max(1, total_turns - keep_recent + 1)
    latest_turn = total_turns

    slim_rows: list[bytes] = []
    compact_rows, compact_summary_count, compact_detected_types, compact_unknown_types = (
        _compacted_summary_rows(rows, cwd=cwd) if include_compact_summaries else ([], 0, (), ())
    )
    recent_original_bytes: dict[int, int] = {}
    recent_slim_bytes: dict[int, int] = {}
    for row in rows:
        turn_no = turn_by_line.get(row.line_no, 0)
        if turn_no >= recent_start:
            recent_original_bytes[turn_no] = recent_original_bytes.get(turn_no, 0) + len(row.raw)

    compact_inserted = False
    kept = 0
    stubbed = 0
    dropped = 0
    for row in rows:
        if not compact_inserted and row.obj and row.obj.get("type") != "session_meta":
            slim_rows.extend(compact_rows)
            kept += len(compact_rows)
            compact_inserted = True

        turn_no = turn_by_line.get(row.line_no, 0)
        payload = row.obj.get("payload") if isinstance(row.obj, dict) else None
        if isinstance(payload, dict) and isinstance(payload.get("codex_slim_context"), dict):
            dropped += 1
            continue
        if row.obj and row.obj.get("type") == "compacted":
            dropped += 1
            continue
        if turn_no >= recent_start:
            verdict, replacement = _recent_row_verdict(
                row,
                mode=mode,
                turn_no=turn_no,
                latest_turn=latest_turn,
                trim_recent_tools=trim_recent_tools,
            )
            if verdict == "KEEP" and replacement is not None:
                slim_rows.append(replacement)
                recent_slim_bytes[turn_no] = recent_slim_bytes.get(turn_no, 0) + len(replacement)
                kept += 1
                continue
            if verdict == "STUB" and replacement is not None:
                slim_rows.append(replacement)
                recent_slim_bytes[turn_no] = recent_slim_bytes.get(turn_no, 0) + len(replacement)
                stubbed += 1
                continue
            dropped += 1
            continue

        verdict, replacement = _old_row_verdict(row, mode)
        if verdict == "KEEP" and replacement is not None:
            slim_rows.append(replacement)
            kept += 1
        elif verdict == "STUB" and replacement is not None:
            slim_rows.append(replacement)
            stubbed += 1
        else:
            dropped += 1

    if not compact_inserted:
        slim_rows.extend(compact_rows)
        kept += len(compact_rows)

    _validate_jsonl_bytes(slim_rows)

    root = codex_root or CODEX_ROOT
    backup_dir = root / "slim-backups"
    backup_name = f"{session_file.name}.bak-slim-{int(time.time())}"
    backup_path = backup_dir / backup_name
    original_bytes = session_file.stat().st_size
    slim_bytes = sum(len(raw) for raw in slim_rows)
    recent_turn_reports = tuple(
        RecentTurnReport(
            turn_no=turn_no,
            original_bytes=recent_original_bytes.get(turn_no, 0),
            slim_bytes=recent_slim_bytes.get(turn_no, 0),
        )
        for turn_no in sorted(recent_original_bytes)
    )

    return SlimPlan(
        session_id=session_id,
        session_file=session_file,
        cwd=cwd,
        original_bytes=original_bytes,
        original_lines=len(rows),
        slim_bytes=slim_bytes,
        slim_lines=len(slim_rows),
        dropped_lines=len(rows) - len(slim_rows),
        mode=mode,
        keep_recent=keep_recent,
        trim_recent_tools=trim_recent_tools,
        total_user_turns=total_turns,
        stats=SlimStats(kept=kept, stubbed=stubbed, dropped=dropped),
        backup_path=backup_path,
        slim_rows=slim_rows,
        compact_summary_count=compact_summary_count,
        compact_detected_types=compact_detected_types,
        compact_unknown_types=compact_unknown_types,
        recent_turn_reports=recent_turn_reports,
    )


def _write_atomic(path: Path, data: bytes) -> None:
    with NamedTemporaryFile("wb", delete=False, dir=path.parent) as handle:
        handle.write(data)
        tmp_path = Path(handle.name)
    try:
        if path.exists():
            os.chmod(tmp_path, path.stat().st_mode)
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def apply_slim_plan(plan: SlimPlan) -> None:
    plan.backup_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(plan.session_file, plan.backup_path)
    _write_atomic(plan.session_file, b"".join(plan.slim_rows))


def new_session_id() -> str:
    # Codex accepts UUID-shaped session ids in rollout filenames.  UUIDv4 is
    # enough for cloned sessions because chronological order comes from the
    # rollout timestamp prefix and file mtime, not from the UUID itself.
    return str(uuid.uuid4())


def _session_file_for_clone(source_file: Path, source_sid: str, cloned_sid: str) -> Path:
    if source_sid in source_file.name:
        name = source_file.name.replace(source_sid, cloned_sid, 1)
    else:
        stem = source_file.stem
        name = f"{stem}-{cloned_sid}.jsonl"
    return source_file.with_name(name)


def _rewrite_session_meta_id(raw: bytes, source_sid: str, cloned_sid: str) -> bytes:
    try:
        obj = json.loads(raw.strip().decode("utf-8"))
    except Exception:
        return raw
    if obj.get("type") != "session_meta":
        return raw
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        return raw
    if payload.get("id") != source_sid:
        return raw
    new_obj = dict(obj)
    new_payload = dict(payload)
    new_payload["id"] = cloned_sid
    new_payload["cloned_from_session_id"] = source_sid
    new_payload["cloned_by"] = "codex-slim"
    new_obj["payload"] = new_payload
    return _dump_jsonl(new_obj)


def apply_slim_plan_to_new_session(
    plan: SlimPlan,
    *,
    cloned_session_id: str | None = None,
) -> CloneSlimResult:
    cloned_sid = cloned_session_id or new_session_id()
    cloned_file = _session_file_for_clone(plan.session_file, plan.session_id, cloned_sid)
    if cloned_file.exists():
        raise FileExistsError(f"clone target already exists: {cloned_file}")

    cloned_rows = [
        _rewrite_session_meta_id(raw, plan.session_id, cloned_sid)
        for raw in plan.slim_rows
    ]
    _validate_jsonl_bytes(cloned_rows)
    _write_atomic(cloned_file, b"".join(cloned_rows))
    return CloneSlimResult(
        source_session_id=plan.session_id,
        cloned_session_id=cloned_sid,
        source_file=plan.session_file,
        cloned_file=cloned_file,
        cloned_bytes=cloned_file.stat().st_size,
        cloned_lines=len(cloned_rows),
    )


def _read_proc_cmdline(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def _read_proc_stat_ppid(pid: int) -> int:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text("utf-8", "replace")
    except OSError:
        return 0
    end = stat.rfind(")")
    if end < 0:
        return 0
    parts = stat[end + 2 :].split()
    if len(parts) < 2:
        return 0
    try:
        return int(parts[1])
    except ValueError:
        return 0


def _proc_tty(pid: int) -> str | None:
    for fd_name in ("0", "1", "2"):
        try:
            target = os.readlink(f"/proc/{pid}/fd/{fd_name}")
        except OSError:
            continue
        if target.startswith("/dev/pts/") or target.startswith("/dev/tty"):
            return target
    return None


def _strip_deleted_suffix(path_text: str) -> tuple[str, bool]:
    suffix = " (deleted)"
    if path_text.endswith(suffix):
        return path_text[: -len(suffix)], True
    return path_text, False


def _session_id_from_text(text: str) -> str | None:
    match = SESSION_ID_RE.search(text)
    return match.group(0) if match else None


def _proc_codex_jsonl(pid: int) -> tuple[Path | None, bool, str | None]:
    fd_dir = Path(f"/proc/{pid}/fd")
    try:
        fd_names = list(fd_dir.iterdir())
    except OSError:
        return None, False, None

    for fd_path in fd_names:
        try:
            target = os.readlink(fd_path)
        except OSError:
            continue
        clean, deleted = _strip_deleted_suffix(target)
        if "/.codex/sessions/" not in clean or not clean.endswith(".jsonl"):
            continue
        session_id = _session_id_from_text(Path(clean).name)
        return Path(clean), deleted, session_id
    return None, False, None


def _is_codex_process(cmdline: list[str]) -> bool:
    if not cmdline:
        return False
    name = Path(cmdline[0]).name
    text = " ".join(cmdline)
    if "codex_slim_reload.py" in text:
        return False
    return name == "codex" or cmdline[0].endswith("/codex")


def iter_codex_processes() -> list[CodexProcess]:
    processes: list[CodexProcess] = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        pid = int(proc.name)
        cmdline = _read_proc_cmdline(pid)
        if not _is_codex_process(cmdline):
            continue
        jsonl_path, jsonl_deleted, sid_from_fd = _proc_codex_jsonl(pid)
        sid_from_cmd = _session_id_from_text(" ".join(cmdline))
        processes.append(
            CodexProcess(
                pid=pid,
                ppid=_read_proc_stat_ppid(pid),
                session_id=sid_from_fd or sid_from_cmd,
                jsonl_path=jsonl_path,
                jsonl_deleted=jsonl_deleted,
                tty=_proc_tty(pid),
                cmdline=cmdline,
            )
        )
    return processes


def find_codex_process_for_session(
    session_id: str,
    *,
    target_pid: int | None = None,
) -> CodexProcess:
    processes = iter_codex_processes()
    if target_pid is not None:
        for proc in processes:
            if proc.pid == target_pid:
                if proc.session_id and proc.session_id != session_id:
                    raise RuntimeError(
                        f"target PID {target_pid} is attached to another Codex session: "
                        f"{proc.session_id} != {session_id}"
                    )
                return proc
        raise ProcessLookupError(f"target Codex PID was not found: {target_pid}")

    matches = [proc for proc in processes if proc.session_id == session_id]
    if not matches:
        raise ProcessLookupError(f"No running Codex process found for session_id={session_id}.")

    # If an accidental `codex resume <sid>` child exists, prefer the original
    # interactive process. This avoids writing the same session from two TUIs.
    non_resume = [proc for proc in matches if not proc.is_resume_command]
    candidates = non_resume or matches
    if len(candidates) == 1:
        return candidates[0]

    detail = "\n".join(
        f"  pid={proc.pid} ppid={proc.ppid} tty={proc.tty or '?'} "
        f"deleted={proc.jsonl_deleted} cmd={proc.command_text}"
        for proc in candidates
    )
    raise RuntimeError(
        "Multiple target Codex processes were found. Specify one with --target-pid.\n" + detail
    )


def _wait_process_on_session(pid: int, session_id: str, timeout_s: float) -> CodexProcess:
    deadline = time.monotonic() + timeout_s
    last: CodexProcess | None = None
    while time.monotonic() < deadline:
        for proc in iter_codex_processes():
            if proc.pid != pid:
                continue
            last = proc
            if proc.session_id == session_id and proc.jsonl_path and not proc.jsonl_deleted:
                return proc
        time.sleep(0.05)
    if last is None:
        raise TimeoutError(f"Codex PID {pid} disappeared.")
    raise TimeoutError(
        f"Codex PID {pid} did not open the target session: "
        f"current={last.session_id or '?'} target={session_id}"
    )


def _write_bridge_request(payload: dict[str, Any], request_id: str) -> Path:
    BRIDGE_INJECT_DIR.mkdir(parents=True, exist_ok=True)
    path = BRIDGE_INJECT_DIR / f"{request_id}.json"
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp_path, path)
    return path


def _wait_bridge_status(request_id: str, timeout_s: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    status_path = BRIDGE_STATUS_DIR / f"{request_id}.json"
    last_status: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        if status_path.exists():
            try:
                last_status = json.loads(status_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                last_status = None
            state = last_status.get("state") if last_status else None
            if state in {"done", "failed"}:
                return last_status
        time.sleep(0.05)
    if last_status:
        raise TimeoutError(f"bridge inject timeout: last_state={last_status.get('state')}")
    raise TimeoutError("bridge inject timeout: no status")


def reload_codex_session_in_place(
    session_id: str,
    *,
    target_pid: int | None = None,
    wait: bool = True,
    timeout_s: float = 5.0,
    resume_only: bool = False,
) -> dict[str, Any]:
    # Codex TUI currently does not expose Claude-style in-TUI `/resume`.
    # This path is retained only for experiments and must verify the process'
    # open JSONL after injection; bridge ack alone only means "text was sent".
    proc = find_codex_process_for_session(session_id, target_pid=target_pid)
    if proc.ppid <= 1:
        raise RuntimeError(f"Could not determine shell PID for Codex PID {proc.pid}.")

    request_id = f"codex-resume-{session_id[:8]}-{os.getpid()}-{int(time.time() * 1000)}"
    payload = {
        "targetShellPid": proc.ppid,
        "requestId": request_id,
        "transactionTimeoutMs": int(timeout_s * 1000),
    }
    if resume_only:
        payload["steps"] = [
            {"text": "\u001b", "addNewLine": False},
            {"text": "\u0015", "addNewLine": False},
            {"text": f"/resume {session_id}", "addNewLine": True},
        ]
    else:
        payload["steps"] = [
            {"text": "\u001b", "addNewLine": False},
            {"text": "\u0015", "addNewLine": False},
            {"text": "/clear", "addNewLine": True},
            {"text": "\u001b", "addNewLine": False, "delayMs": 250},
            {"text": "\u0015", "addNewLine": False},
            {"text": f"/resume {session_id}", "addNewLine": True},
        ]
    _write_bridge_request(payload, request_id)
    if not wait:
        return {
            "state": "published",
            "requestId": request_id,
            "targetPid": proc.pid,
            "targetShellPid": proc.ppid,
        }

    status = _wait_bridge_status(request_id, timeout_s + 0.5)
    state = status.get("state")
    if state != "done":
        raise RuntimeError(f"bridge inject failed: {status}")
    verified = _wait_process_on_session(proc.pid, session_id, timeout_s)
    return {
        **status,
        "requestId": request_id,
        "targetPid": proc.pid,
        "targetShellPid": proc.ppid,
        "verifiedSessionId": verified.session_id,
        "verifiedJsonlPath": str(verified.jsonl_path) if verified.jsonl_path else None,
    }


def _sanitize_env_for_codex() -> dict[str, str]:
    env = os.environ.copy()
    venv = env.pop("VIRTUAL_ENV", None)
    env.pop("UV", None)
    env.pop("UV_RUN_RECURSION_DEPTH", None)
    env.pop("UV_PROJECT_ENVIRONMENT", None)
    env.pop("PYTHONHOME", None)
    if venv:
        venv_bin = f"{venv}/bin"
        env["PATH"] = ":".join(
            part for part in env.get("PATH", "").split(":") if part and part != venv_bin
        )
    return env


def launch_codex_resume_process(
    session_id: str,
    *,
    cwd: str | None = None,
    wait: bool = True,
) -> int:
    args = ["codex", "resume", session_id]
    env = _sanitize_env_for_codex()
    run_cwd = cwd if cwd and Path(cwd).exists() else None
    if wait:
        return subprocess.call(args, cwd=run_cwd, env=env)
    subprocess.Popen(args, cwd=run_cwd, env=env, start_new_session=True)
    return 0


def _resolve_session_file(args: argparse.Namespace) -> Path:
    root = args.codex_root
    if args.session_file:
        return args.session_file
    if args.session_id:
        return find_session_file_by_id(args.session_id, root)
    return find_latest_session_file(root)


def _print_plan(plan: SlimPlan) -> None:
    print(f"session_id: {plan.session_id}")
    print(f"session_file: {plan.session_file}")
    print(f"cwd: {plan.cwd or '(none)'}")
    print(f"user_turns: {plan.total_user_turns}")
    print(f"mode: {plan.mode}")
    print(f"keep_recent: {plan.keep_recent}")
    print(f"trim_recent_tools: {plan.trim_recent_tools}")
    print(f"compact_summaries: {plan.compact_summary_count}")
    print(
        "compact_detected_types: "
        + (", ".join(plan.compact_detected_types) if plan.compact_detected_types else "(none)")
    )
    if plan.compact_unknown_types:
        print("compact_unknown_types: " + ", ".join(plan.compact_unknown_types))
        print("hint: add unknown types to codex_slim_compact_event_types if they contain summary text")
    print(f"lines: {plan.original_lines} -> {plan.slim_lines}  (drop {plan.dropped_lines})")
    print(
        f"verdict: KEEP={plan.stats.kept}  "
        f"STUB={plan.stats.stubbed}  DROP={plan.stats.dropped}"
    )
    print(
        f"size: {fmt_size(plan.original_bytes)} -> {fmt_size(plan.slim_bytes)}  "
        f"(save {fmt_size(plan.saved_bytes)}, {plan.saved_percent:.1f}%)"
    )
    if plan.recent_turn_reports:
        print("recent_raw_turns:")
        for report in plan.recent_turn_reports:
            saved = report.original_bytes - report.slim_bytes
            print(
                f"  turn {report.turn_no}: "
                f"{fmt_size(report.original_bytes)} -> {fmt_size(report.slim_bytes)} "
                f"(save {fmt_size(saved)})"
            )
    print(f"backup: {plan.backup_path}")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Slim a Codex session JSONL and optionally inject /resume into the current Codex TUI.",
    )
    parser.add_argument("--session-id", help="Target Codex session id. Defaults to the latest session.")
    parser.add_argument("--session-file", type=Path, help="Target Codex JSONL file")
    parser.add_argument(
        "--codex-root",
        type=Path,
        default=CODEX_ROOT,
        help="Codex root directory (default: ~/.codex)",
    )
    parser.add_argument(
        "--mode",
        default="strong",
        help="Slim strength: safe keeps 10 turns, strong keeps 3 turns. Legacy weak/medium map to safe; heavy-strong maps to strong.",
    )
    parser.add_argument(
        "--keep-recent",
        type=int,
        default=None,
        help="Number of recent user turns to preserve as raw context. Defaults to the mode setting.",
    )
    parser.add_argument(
        "--include-compact-summaries",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Insert previous compacted payload.message summaries at the front of the new context.",
    )
    parser.add_argument(
        "--trim-recent-tools",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Also remove tool calls, tool outputs, token counts, and turn plumbing inside recent raw-protected turns.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print the plan only; do not modify files")
    parser.add_argument("--yes", action="store_true", help="Apply without confirmation")
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="Overwrite the original session JSONL. This is the current default and kept for compatibility.",
    )
    parser.add_argument(
        "--clone",
        action="store_true",
        help="Preserve the original and create a slimmed clone with a new sid. Intended for recovery experiments.",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Reload after slim. Default path is experimental in-place injection and fails on verification errors.",
    )
    parser.add_argument(
        "--new-process",
        action="store_true",
        help="With --reload, launch a new codex resume process instead of in-place injection.",
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="With --reload, do not wait for bridge ack or the new process to exit.",
    )
    parser.add_argument(
        "--target-pid",
        type=int,
        help="Target Codex PID for in-place reload when multiple Codex processes share a session.",
    )
    parser.add_argument(
        "--inject-timeout",
        type=float,
        default=5.0,
        help="Overall in-place bridge injection timeout in seconds (default: 5.0)",
    )
    parser.add_argument(
        "--resume-only",
        action="store_true",
        help="For experimental in-place reload, inject only /resume without /clear.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        session_file = _resolve_session_file(args)
        plan = build_slim_plan(
            session_file,
            mode=args.mode,
            keep_recent=args.keep_recent,
            codex_root=args.codex_root,
            include_compact_summaries=args.include_compact_summaries,
            trim_recent_tools=args.trim_recent_tools,
        )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    _print_plan(plan)
    if args.dry_run:
        print("\n--dry-run: no files modified")
        return 0

    if not args.yes:
        answer = input("\nApply this slim plan? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("Cancelled.")
            return 0

    reload_session_id = plan.session_id
    reload_cwd = plan.cwd
    try:
        if args.clone:
            clone = apply_slim_plan_to_new_session(plan)
            reload_session_id = clone.cloned_session_id
            print("\nslim clone complete")
            print(f"source_session_id: {clone.source_session_id}")
            print(f"cloned_session_id: {clone.cloned_session_id}")
            print(f"cloned_file: {clone.cloned_file}")
            print("Original session file was not modified.")
        else:
            apply_slim_plan(plan)
            print("\nslim complete (in-place)")
            print(f"session_id: {plan.session_id}")
            print(f"backup: {plan.backup_path}")
    except Exception as exc:
        print(f"slim failed: {exc}", file=sys.stderr)
        return 1

    if args.reload:
        if args.new_process:
            print(f"\n→ Launching new process: codex resume {reload_session_id}")
            return launch_codex_resume_process(reload_session_id, cwd=reload_cwd, wait=not args.no_wait)
        try:
            print(f"\n→ In-place reload: /resume {reload_session_id}")
            status = reload_codex_session_in_place(
                reload_session_id,
                target_pid=args.target_pid,
                wait=not args.no_wait,
                timeout_s=args.inject_timeout,
                resume_only=args.resume_only,
            )
        except Exception as exc:
            print(f"in-place reload failed: {exc}", file=sys.stderr)
            print("Use --target-pid <codex-pid> or --new-process if needed.", file=sys.stderr)
            return 1
        print(
            "bridge inject complete: "
            f"state={status.get('state')} targetPid={status.get('targetPid')} "
            f"targetShellPid={status.get('targetShellPid')}"
        )
        return 0

    print(f"\nReload command: codex resume {reload_session_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
