"""gccfork 휴지통 — jsonl 영구 삭제 회피.

삭제 시 즉시 unlink 하지 않고 `~/.claude/gccfork-trash/<sid>/` 로 이동.
복원 가능. registry 백업도 meta.json 에 함께 저장.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

from gccfork_sessions import (
    CLAUDE_ROOT,
    invalidate_parse_cache,
    registry_get,
    registry_remove,
    registry_set,
)


# 삭제 시 jsonl 을 즉시 unlink 하지 않고 이 디렉터리로 이동.
#   <session_id>/
#     ├─ <basename>.jsonl   ← 원본 jsonl
#     └─ meta.json          ← original_path / deleted_at / registry 백업
TRASH_DIR = CLAUDE_ROOT / "gccfork-trash"


def move_session_to_trash(session_id: str, jsonl_path: Path) -> bool:
    """jsonl 을 휴지통으로 이동 + meta.json 기록 + registry 제거."""
    if not jsonl_path.exists():
        # 이미 사라진 파일 — registry만 정리하고 성공 처리
        registry_remove(session_id)
        invalidate_parse_cache(jsonl_path)
        return True

    entry_dir = TRASH_DIR / session_id
    try:
        entry_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False

    reg_data = registry_get(session_id)

    meta = {
        "session_id": session_id,
        "original_path": str(jsonl_path),
        "basename": jsonl_path.name,
        "deleted_at": datetime.now().isoformat(timespec="seconds"),
        "registry": reg_data,
    }

    try:
        dest = entry_dir / jsonl_path.name
        shutil.move(str(jsonl_path), str(dest))
        (entry_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        return False

    # slim_fork_session_with(in_place=True) 가 만든 자동 백업 .bak.<ts>.jsonl
    # 들도 같이 정리 — 본 폴더에 남으면 scan_sessions 가 잡아서 sid 부활 버그
    # 의 원인. B안 — 백업의 백업이라 의미 없으므로 그냥 삭제.
    parent_dir = jsonl_path.parent
    if parent_dir.exists():
        for bak in parent_dir.glob(f"{session_id}.bak.*.jsonl"):
            try:
                bak.unlink()
            except OSError:
                pass

    registry_remove(session_id)
    # 캐시에 남아있던 stale 항목 정리 (없으면 no-op)
    invalidate_parse_cache(jsonl_path)
    return True


def list_trash_entries() -> list[dict]:
    """휴지통의 모든 엔트리 목록 (최근 삭제순)."""
    if not TRASH_DIR.exists():
        return []
    entries: list[dict] = []
    for entry_dir in TRASH_DIR.iterdir():
        if not entry_dir.is_dir():
            continue
        meta_path = entry_dir / "meta.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        basename = meta.get("basename", "")
        jsonl_path = entry_dir / basename if basename else None
        size = 0
        if jsonl_path and jsonl_path.exists():
            try:
                size = jsonl_path.stat().st_size
            except OSError:
                size = 0
        reg = meta.get("registry") or {}
        entries.append({
            "session_id": meta.get("session_id", entry_dir.name),
            "deleted_at": meta.get("deleted_at", ""),
            "name": reg.get("name") or reg.get("custom_name") or "",
            "parent_id": reg.get("parent_id"),
            "basename": basename,
            "jsonl_path": jsonl_path,
            "meta_path": meta_path,
            "entry_dir": entry_dir,
            "size": size,
            "original_path": meta.get("original_path"),
            "registry": reg,
        })
    entries.sort(key=lambda e: e["deleted_at"], reverse=True)
    return entries


def restore_trash_entry(entry: dict) -> bool:
    """휴지통 엔트리를 원래 위치로 복원 + registry 재기록."""
    src = entry.get("jsonl_path")
    original = entry.get("original_path")
    if not src or not original:
        return False
    src = Path(src)
    dst = Path(original)
    if not src.exists():
        return False

    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
    except OSError:
        return False

    # registry 복원
    reg = entry.get("registry") or {}
    if isinstance(reg, dict):
        clean = {k: v for k, v in reg.items() if v is not None}
        if clean:
            registry_set(entry["session_id"], **clean)

    entry_dir = entry.get("entry_dir")
    if entry_dir:
        try:
            shutil.rmtree(str(entry_dir))
        except OSError:
            pass
    # 복원된 jsonl 캐시 무효화 (혹시 옛 항목 남아있었으면)
    invalidate_parse_cache(dst)
    return True


def purge_trash_entry(entry: dict) -> bool:
    """휴지통 엔트리를 영구 삭제."""
    entry_dir = entry.get("entry_dir")
    if not entry_dir:
        return False
    try:
        shutil.rmtree(str(entry_dir))
        return True
    except OSError:
        return False


def purge_all_trash() -> int:
    """휴지통의 모든 엔트리 영구 삭제. 삭제된 개수 반환."""
    if not TRASH_DIR.exists():
        return 0
    count = 0
    for entry_dir in list(TRASH_DIR.iterdir()):
        if entry_dir.is_dir():
            try:
                shutil.rmtree(str(entry_dir))
                count += 1
            except OSError:
                pass
    return count
