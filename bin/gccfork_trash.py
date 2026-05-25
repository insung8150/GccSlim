"""gccfork trash — avoid permanent JSONL deletion.

Deleting a session moves it to `~/.claude/gccfork-trash/<sid>/` instead of
unlinking it immediately. The session can be restored, and a registry backup is
stored in meta.json.
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


# Move JSONL files here instead of unlinking them immediately.
#   <session_id>/
#     ├─ <basename>.jsonl   ← original JSONL
#     └─ meta.json          ← original_path / deleted_at / registry backup
TRASH_DIR = CLAUDE_ROOT / "gccfork-trash"


def move_session_to_trash(session_id: str, jsonl_path: Path) -> bool:
    """Move a JSONL to trash, write meta.json, and remove the registry entry."""
    if not jsonl_path.exists():
        # File already gone; clean only the registry and treat it as success.
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

    # Also remove automatic .bak.<ts>.jsonl backups created by
    # slim_fork_session_with(in_place=True). If they remain in the source
    # folder, scan_sessions may rediscover the sid. These are backup backups, so
    # simple deletion is acceptable.
    parent_dir = jsonl_path.parent
    if parent_dir.exists():
        for bak in parent_dir.glob(f"{session_id}.bak.*.jsonl"):
            try:
                bak.unlink()
            except OSError:
                pass

    registry_remove(session_id)
    # Clear any stale parse-cache entry; no-op when absent.
    invalidate_parse_cache(jsonl_path)
    return True


def list_trash_entries() -> list[dict]:
    """List all trash entries, newest first."""
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
    """Restore a trash entry to its original path and rewrite the registry."""
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

    # Restore registry.
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
    # Invalidate the restored JSONL cache in case an old entry remains.
    invalidate_parse_cache(dst)
    return True


def purge_trash_entry(entry: dict) -> bool:
    """Permanently delete one trash entry."""
    entry_dir = entry.get("entry_dir")
    if not entry_dir:
        return False
    try:
        shutil.rmtree(str(entry_dir))
        return True
    except OSError:
        return False


def purge_all_trash() -> int:
    """Permanently delete every trash entry and return the count."""
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
