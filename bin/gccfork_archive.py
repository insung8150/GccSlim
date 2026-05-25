"""gccfork_archive.py — archive child sessions under a parent session.

Selected design D: move jsonl files into an archive folder, update the
registry, and redirect sid lookup. The jsonl itself is preserved so child sid
references embedded in external .md files do not become dead links.

This is a sidecar module, split out under the main `gccfork` mono
non-interactive policy.

## New registry fields

Each child session entry receives these four fields:

```json
{
  "sessions": {
    "<child_sid>": {
      "name": "...",
      "archived": true,
      "archived_into": "<parent_sid>",
      "archive_path": "/home/.../<P>/archive/<child_sid>.jsonl",
      "archived_at": "2026-05-01T03:36:00.000Z"
    }
  }
}
```

`archived = false` or a missing key means a normal active session. `archived = true` means archived.
`archived_into` stores the direct parent sid. In recursive archive, grandchildren point to their own direct parent.

## Options (prefs `archive.*`)

Ten options are listed in `ARCHIVE_DEFAULTS`.
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from gccfork_sessions import (
    PROJECTS_DIR,
    Session,
    all_active_sid_pid_map,
    load_registry,
    pref_get,
    registry_get,
    registry_set,
)


class ActiveSessionArchiveError(ValueError):
    """Raised when trying to archive an active Claude session.

    Double safety guard in archive_session; merge passes through this path too.
    """
    def __init__(self, sid: str):
        self.sid = sid
        super().__init__(
            f"Active Claude session {sid[:8]} cannot be archived. "
            f"Run /quit first, then try again."
        )

# Textual UI imports for sidecar-local Screen and Mixin classes.
# gccfork runs inside the PEP 723 venv, so textual is expected to be available.
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static


# ── option defaults ──────────────────────────────────────────────────────────
# Ten options. Pref keys use the `archive_` prefix and underscores for Textual widget id compatibility.
# Read them with `pref_get(key, default)`.
ARCHIVE_DEFAULTS: dict[str, str | bool] = {
    "archive_preview_mode": "tail_sections",          # interleave / tail_sections / headers_only / split
    "archive_search_includes_children": True,
    "archive_important_handling": "confirm",          # auto_include / confirm / reject
    "archive_restore_enabled": "trash_pattern",       # trash_pattern / permanent
    "archive_trigger_mode": "both",                   # keybinding / button / both
    "archive_lazy_load": True,
    "archive_child_color_distinction": True,
    "archive_section_header_format": "simple",        # simple / verbose
    "archive_child_sort_order": "mtime",              # mtime / branch_order / alphabetic
    "archive_folder_layout": "per_project",           # per_project / central
}

# Archive root used by the central layout.
CENTRAL_ARCHIVE_ROOT = Path.home() / ".claude" / "gccfork-archive"


def get_archive_pref(key: str):
    """Read archive.* prefs and fall back to ARCHIVE_DEFAULTS when missing.

    Use this helper instead of direct calls such as
    `pref_get("archive_preview_mode")`, so missing defaults stay safe.
    """
    if key not in ARCHIVE_DEFAULTS:
        # Unregistered key: return None instead of KeyError to avoid caller crashes.
        return pref_get(key, None)
    return pref_get(key, ARCHIVE_DEFAULTS[key])


# ── archive folder location ────────────────────────────────────────────────────
def _archive_dir(jsonl_path: Path, layout: Optional[str] = None) -> Path:
    """Return the archive folder where a jsonl file should be moved, based on layout option.

    - per_project (default): an `archive/` subfolder under the project folder containing the jsonl, e.g. `~/.claude/projects/<P>/archive/`
    - central: one archive root with per-project subfolders using sanitized project names, e.g. `~/.claude/gccfork-archive/<P>/`
    """
    if layout is None:
        layout = str(get_archive_pref("archive_folder_layout"))
    project_dir = jsonl_path.parent
    if layout == "central":
        # Use the project folder name as-is, for example -home-yooha-...-MindVault.
        return CENTRAL_ARCHIVE_ROOT / project_dir.name
    # default: per_project
    return project_dir / "archive"


# ── archive metadata ────────────────────────────────────────────────────────
@dataclass
class ArchivedChildMeta:
    """Archived child metadata used by preview rendering.

    Returned by `archived_children_for(parent_sid)`. The jsonl body is lazy-loaded; only `path` is carried here and body text is read by separate helpers when needed.
    """
    sid: str
    short_id: str
    path: Path                # absolute jsonl path inside archive
    name: Optional[str]       # custom_name or None
    auto_summary: Optional[str]
    archived_at: str          # iso8601
    parent_sid: str
    size_bytes: int = 0
    turn_count: int = 0       # from registry when available, otherwise -1
    fork_type: Optional[str] = None  # hard / slim / soft / auto


# ── recursive descendant collection ──────────────────────────────────────────────────────
def collect_subtree(
    root_sids: Iterable[str],
    all_sessions: list[Session],
) -> list[Session]:
    """Return all descendants of root_sids, deduplicated.

    BFS does not include root_sids in the result, matching the pattern where the selected node remains listed while only descendants are archived.

    Callers that need to archive roots too should add them explicitly or start from children with root_sids as parents.

    Cycle guard: visited set prevents infinite loops.
    """
    result: list[Session] = []
    seen: set[str] = set(root_sids)  # exclude root from result and avoid revisits

    queue: list[str] = list(root_sids)
    while queue:
        current = queue.pop(0)
        for s in all_sessions:
            if s.id in seen:
                continue
            if s.parent_id == current:
                seen.add(s.id)
                result.append(s)
                queue.append(s.id)
    return result


# ── jsonl move and registry update (atomic) ──────────────────────────────────
def archive_session(session: Session, parent_sid: str) -> bool:
    """Move session jsonl into the archive folder and add the four registry fields.

    Atomicity: move first, then write registry. The move is an OS rename when possible; registry write failure attempts to roll the jsonl back.

    Idempotent: already archived sessions return True as a no-op.

    Safety guard §2: reject active Claude sessions with ActiveSessionArchiveError. This prevents the 2026-05-04 failure where Claude kept writing to a moved jsonl, created a stub, and corrupted registry metadata. Already archived sessions pass.
    """
    src = session.jsonl_path
    if not src.exists():
        return False

    reg_entry = registry_get(session.id)
    if reg_entry.get("archived"):
        # Already archived session: no-op.
        return True

    # Safety guard §2: block active sid, covering direct archive calls in addition to merge guard §1.
    if session.id in all_active_sid_pid_map():
        raise ActiveSessionArchiveError(session.id)

    archive_dir = _archive_dir(src)
    archive_dir.mkdir(parents=True, exist_ok=True)
    dst = archive_dir / src.name

    # Add a timestamp suffix when the same archive filename already exists.
    if dst.exists():
        ts = int(time.time())
        dst = archive_dir / f"{src.stem}.archived-{ts}{src.suffix}"

    try:
        # OS rename is atomic on the same filesystem; otherwise shutil.move copies and deletes.
        shutil.move(str(src), str(dst))
    except OSError:
        return False

    # Update registry. Roll jsonl back on failure.
    try:
        registry_set(
            session.id,
            archived=True,
            archived_into=parent_sid,
            archive_path=str(dst),
            archived_at=datetime.now(timezone.utc).isoformat(),
        )
    except Exception:
        try:
            shutil.move(str(dst), str(src))
        except OSError:
            pass
        return False

    return True


# ── restore (trash pattern) ──────────────────────────────────────────────────
def restore_session(sid: str) -> bool:
    """Restore an archived session to its original location and remove the four registry fields.

    Restore follows the trash pattern: remove keys instead of leaving archived=false in registry.
    If `pref_get("archive_restore_enabled") == "permanent"`, callers must reject before calling; this function always works.

    Safety guard §3 fallback: even if registry entry lost its archived flag, scan archive folders directly and try to find the sid jsonl.
    Safety guards §4+5: if dst collides with a stub (lines < 100), back it up as .stub.bak and prefer the archive body. Only real collisions use .restored-<ts>.
    """
    entry = registry_get(sid)
    archive_path: Optional[Path] = None
    archived_flag = entry.get("archived")

    if archived_flag:
        # Normal path: trust registry.
        archive_path = Path(entry.get("archive_path", ""))
        if not archive_path.exists():
            archive_path = None

    if archive_path is None:
        # Safety guard §3: folder scan fallback.
        candidates: list[Path] = []
        for proj in PROJECTS_DIR.iterdir() if PROJECTS_DIR.exists() else []:
            p = proj / "archive" / f"{sid}.jsonl"
            if p.exists():
                candidates.append(p)
        if CENTRAL_ARCHIVE_ROOT.exists():
            for cd in CENTRAL_ARCHIVE_ROOT.iterdir():
                if cd.is_dir():
                    p = cd / f"{sid}.jsonl"
                    if p.exists():
                        candidates.append(p)
        if candidates:
            # Choose the largest candidate, usually the real one.
            archive_path = max(candidates, key=lambda p: p.stat().st_size)

    if archive_path is None or not archive_path.exists():
        return False

    # Original location is the project folder with the same jsonl filename.
    # For per_project, archive folder parent is the project folder.
    # For central, use the same-named project folder under PROJECTS_DIR.
    layout = str(get_archive_pref("archive_folder_layout"))
    if layout == "central":
        project_dir = PROJECTS_DIR / archive_path.parent.name
    else:
        project_dir = archive_path.parent.parent  # parent of archive/

    project_dir.mkdir(parents=True, exist_ok=True)
    dst = project_dir / archive_path.name

    if dst.exists():
        # Safety guards §4+5: automatically split stub versus real collision.
        # If an active Claude process created a stub after archive, back it up and restore the archive body. Only real large-file collisions use .restored.
        try:
            line_count = sum(1 for _ in dst.open(encoding="utf-8", errors="ignore"))
        except OSError:
            line_count = 999_999
        if line_count < 100:
            # Classified as stub: keep a safe backup for diagnosis/identification only.
            ts = int(time.time())
            stub_backup = project_dir / f"{archive_path.stem}.stub-{ts}{archive_path.suffix}"
            try:
                shutil.move(str(dst), str(stub_backup))
            except OSError:
                # Backup failed; fall back to collision avoidance.
                ts = int(time.time())
                dst = project_dir / f"{archive_path.stem}.restored-{ts}{archive_path.suffix}"
        else:
            # Real content collision: keep previous behavior by restoring archive as .restored.
            ts = int(time.time())
            dst = project_dir / f"{archive_path.stem}.restored-{ts}{archive_path.suffix}"

    try:
        shutil.move(str(archive_path), str(dst))
    except OSError:
        return False

    # Remove the four registry fields. Passing None pops keys in registry_set.
    try:
        registry_set(
            sid,
            archived=None,
            archived_into=None,
            archive_path=None,
            archived_at=None,
        )
    except Exception:
        # Even if registry update fails, the file has already moved; leave it in place.
        pass

    return True


# ── unmerge: reverse archive by restoring all children under a parent ─────────────────────
def unmerge_parent(parent_sid: str) -> tuple[int, int]:
    """Unmerge all archived children merged under a parent by restoring them in place.

    Exact inverse of merge/archive_session. Child jsonl files move back to their original project_dir and the four archive registry fields are removed. The parent entry is untouched because the 📦N marker is computed dynamically by archived_children_count(), so it disappears when children are restored.

    If `pref_get("archive_restore_enabled") == "permanent"`, callers must reject before calling; this function always works.

    Returns:
        (success, fail), where attempted children = success + fail
    """
    children = archived_children_for(parent_sid)
    ok, fail = 0, 0
    for c in children:
        if restore_session(c.sid):
            ok += 1
        else:
            fail += 1
    return ok, fail


# ── stub jsonl sweeper ──────────────────────────────────────────────────
# Fixes stale tree entries caused when Claude SessionStart hooks or last-prompt trackers create metadata-only stub jsonl files for archived child sids in the main project_dir.
#
# Safety criteria: sweep only when all three are true:
#   1. file size < 5KB
#   2. registry entry for sid has archived=true
#   3. jsonl has zero user/assistant role messages
#
# Action: move to archive/.stale-stubs/<ts>-<sid>.jsonl. Do not delete, so rollback remains possible.

STUB_SWEEP_SIZE_LIMIT = 5 * 1024  # 5KB
STUB_SWEEP_QUARANTINE = ".stale-stubs"


def _is_stale_stub(jsonl_path: Path, sid: str) -> bool:
    """Return True only when all three guards pass and the file should be swept."""
    try:
        size = jsonl_path.stat().st_size
    except OSError:
        return False
    if size >= STUB_SWEEP_SIZE_LIMIT:
        return False
    entry = registry_get(sid)
    if not entry.get("archived"):
        return False
    # count user/assistant messages
    try:
        text = jsonl_path.read_text(encoding="utf-8")
    except OSError:
        return False
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        msg = d.get("message") or {}
        if isinstance(msg, dict) and msg.get("role") in ("user", "assistant"):
            return False  # real conversation message found, so this is not a stub
    return True


def sweep_stale_stubs(project_dir: Path) -> list[str]:
    """Scan every jsonl in project_dir and move stubs into the quarantine folder.

    Returns:
        List of swept sid prefixes (8 chars). Empty list means nothing was swept.
    """
    if not project_dir.exists():
        return []
    quarantine_dir = project_dir / "archive" / STUB_SWEEP_QUARANTINE
    swept: list[str] = []
    ts = int(time.time())
    for jsonl in project_dir.glob("*.jsonl"):
        sid = jsonl.stem
        # Basic sid shape validation (36 chars + 4 dashes) protects unrelated filenames.
        if len(sid) != 36 or sid.count("-") != 4:
            continue
        if not _is_stale_stub(jsonl, sid):
            continue
        try:
            quarantine_dir.mkdir(parents=True, exist_ok=True)
            dst = quarantine_dir / f"{ts}-{jsonl.name}"
            shutil.move(str(jsonl), str(dst))
            swept.append(sid[:8])
        except OSError:
            continue
    return swept


def sweep_all_known_projects() -> dict[str, list[str]]:
    """Run sweep_stale_stubs across all project directories under PROJECTS_DIR.

    Returns:
        {project_name: [swept_sid_prefixes]}, including only projects with swept files.
    """
    out: dict[str, list[str]] = {}
    if not PROJECTS_DIR.exists():
        return out
    for proj in PROJECTS_DIR.iterdir():
        if not proj.is_dir():
            continue
        swept = sweep_stale_stubs(proj)
        if swept:
            out[proj.name] = swept
    return out


# ── sid -> archive jsonl path lookup ─────────────────────────────────────
def find_archived_session(sid: str) -> Optional[Path]:
    """Return the archived jsonl path for sid if archived; otherwise None.

    `gccfork_sessions.find_session_by_id` calls this after normal search fails (one-line patch in late Phase 1).
    """
    entry = registry_get(sid)
    if not entry.get("archived"):
        return None
    archive_path = entry.get("archive_path")
    if not archive_path:
        return None
    p = Path(archive_path)
    if not p.exists():
        return None
    return p


# ── archived children for a parent ─────────────────────────────────────────────
def archived_children_for(parent_sid: str) -> list[ArchivedChildMeta]:
    """Return all children whose registry archived_into equals parent_sid.

    Used by preview rendering. The jsonl body is lazy-loaded, so this returns metadata only. Sort order follows prefs `archive.child_sort_order`.

    Safety guard §3: fallback to parent.merged_from plus archive folder scan so children with damaged archived flags are still detected. This double-check prevents missing children during unmerge, based on the 2026-05-04 ca09 incident.
    """
    reg = load_registry()
    sessions = reg.get("sessions", {}) or {}
    out: list[ArchivedChildMeta] = []
    seen_sids: set[str] = set()
    for sid, entry in sessions.items():
        if not entry.get("archived"):
            continue
        if entry.get("archived_into") != parent_sid:
            continue
        archive_path = Path(entry.get("archive_path", ""))
        if not archive_path.exists():
            # Stale entry with missing file: skip it. A cleanup UI can be added separately.
            continue
        seen_sids.add(sid)
        try:
            size = archive_path.stat().st_size
        except OSError:
            size = 0
        out.append(
            ArchivedChildMeta(
                sid=sid,
                short_id=sid[:8],
                path=archive_path,
                name=entry.get("name"),
                auto_summary=entry.get("auto_summary"),
                archived_at=entry.get("archived_at", ""),
                parent_sid=parent_sid,
                size_bytes=size,
                turn_count=int(entry.get("turn_count") or -1),
                fork_type=entry.get("fork_type"),
            )
        )

    # Safety guard §3 fallback: even if parent.merged_from sids lost archived flags due to registry damage, detect them when jsonl exists in archive folders.
    # If found, create ArchivedChildMeta even without archive_path so unmerge can
    # restore_session(sid) can find it again by folder scan.
    parent_entry = sessions.get(parent_sid) or {}
    merged_from = parent_entry.get("merged_from") or []
    if merged_from:
        # archive folder candidates: try both per_project and central
        archive_dirs: list[Path] = []
        # per_project: since active_path is hard to infer, try archive/ under every project folder in PROJECTS_DIR
        for proj in PROJECTS_DIR.iterdir() if PROJECTS_DIR.exists() else []:
            ad = proj / "archive"
            if ad.is_dir():
                archive_dirs.append(ad)
        # central
        if CENTRAL_ARCHIVE_ROOT.exists():
            for cd in CENTRAL_ARCHIVE_ROOT.iterdir():
                if cd.is_dir():
                    archive_dirs.append(cd)

        for sid in merged_from:
            if sid in seen_sids:
                continue
            for ad in archive_dirs:
                p = ad / f"{sid}.jsonl"
                if p.exists():
                    try:
                        size = p.stat().st_size
                    except OSError:
                        size = 0
                    entry_data = sessions.get(sid) or {}
                    out.append(
                        ArchivedChildMeta(
                            sid=sid,
                            short_id=sid[:8],
                            path=p,
                            name=entry_data.get("name"),
                            auto_summary=entry_data.get("auto_summary"),
                            archived_at=entry_data.get("archived_at", ""),
                            parent_sid=parent_sid,
                            size_bytes=size,
                            turn_count=int(entry_data.get("turn_count") or -1),
                            fork_type=entry_data.get("fork_type"),
                        )
                    )
                    seen_sids.add(sid)
                    break

    # sort
    sort_order = str(get_archive_pref("archive_child_sort_order"))
    if sort_order == "alphabetic":
        out.sort(key=lambda m: (m.name or m.short_id).lower())
    elif sort_order == "branch_order":
        # branch order = archived_at ascending
        out.sort(key=lambda m: m.archived_at)
    else:
        # mtime — newest first
        def _mtime(m: ArchivedChildMeta) -> float:
            try:
                return m.path.stat().st_mtime
            except OSError:
                return 0.0
        out.sort(key=_mtime, reverse=True)
    return out


# ── archived child count for parent node labels ─────────────────────────────
def archived_children_count(parent_sid: str) -> int:
    """Fast count for parent node label `📦 N archived`."""
    reg = load_registry()
    sessions = reg.get("sessions", {}) or {}
    return sum(
        1
        for entry in sessions.values()
        if entry.get("archived") and entry.get("archived_into") == parent_sid
    )


# ── all archived children for full archive view ────────────────────────
def all_archived_sessions() -> list[ArchivedChildMeta]:
    """For showing all archived children in one archive view, similar to trash.

    Caller is responsible for grouping by parent_sid.
    """
    reg = load_registry()
    sessions = reg.get("sessions", {}) or {}
    out: list[ArchivedChildMeta] = []
    for sid, entry in sessions.items():
        if not entry.get("archived"):
            continue
        archive_path = Path(entry.get("archive_path", ""))
        if not archive_path.exists():
            continue
        try:
            size = archive_path.stat().st_size
        except OSError:
            size = 0
        out.append(
            ArchivedChildMeta(
                sid=sid,
                short_id=sid[:8],
                path=archive_path,
                name=entry.get("name"),
                auto_summary=entry.get("auto_summary"),
                archived_at=entry.get("archived_at", ""),
                parent_sid=entry.get("archived_into", ""),
                size_bytes=size,
                turn_count=int(entry.get("turn_count") or -1),
                fork_type=entry.get("fork_type"),
            )
        )
    out.sort(key=lambda m: m.archived_at, reverse=True)
    return out


# ── Integrated preview rendering — four modes ──────────────────────────────────────
def _read_archive_jsonl_preview(
    path: Path,
    max_bytes: Optional[int] = None,
) -> str:
    """Read an archived jsonl and convert it to user-readable text.

    Extract user/assistant messages from each line and summarize them briefly. Skip system/tool messages.
    When `max_bytes` is provided, read only that much and mark the preview as truncated.
    """
    import json as _json
    out_lines: list[str] = []
    truncated = False
    try:
        with path.open("rb") as fh:
            data = fh.read(max_bytes) if max_bytes is not None else fh.read()
            if max_bytes is not None and len(data) >= max_bytes:
                truncated = True
        text = data.decode("utf-8", errors="ignore")
    except OSError:
        return "  (file read failed)"

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        typ = d.get("type")
        if typ not in {"user", "assistant"}:
            continue
        if d.get("isSidechain") or d.get("isMeta"):
            continue
        msg = d.get("message", {}) or {}
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", typ)
        content = msg.get("content", "")
        body = ""
        if isinstance(content, str):
            body = content
        elif isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        parts.append(str(item.get("text", "")))
                    elif item.get("type") == "tool_use":
                        parts.append(f"[tool: {item.get('name', '?')}]")
            body = "\n".join(parts)
        body = body.strip()
        if not body:
            continue
        # Skip system-injected tag-like content starting with `<`.
        if body.startswith("<system-reminder>") or body.startswith("<command-name>"):
            continue
        prefix = "👤" if role == "user" else "🤖"
        out_lines.append(f"  {prefix} {body[:600]}")
        out_lines.append("")  # blank line between messages
    if truncated:
        out_lines.append("  …(truncated, lazy-load mode)")
    return "\n".join(out_lines) if out_lines else "  (no conversation content)"


def _format_child_header(
    meta: ArchivedChildMeta,
    fmt: Optional[str] = None,
) -> str:
    """Child section header, controlled by option 8 (simple/verbose)."""
    if fmt is None:
        fmt = str(get_archive_pref("archive_section_header_format"))
    name = meta.name or meta.auto_summary or "(unnamed)"
    name = name.replace("\n", " ")[:50]

    fork_emoji = ""
    if meta.fork_type == "hard":
        fork_emoji = "🪓 "
    elif meta.fork_type == "slim":
        fork_emoji = "🔻 "
    elif meta.fork_type in {"soft", "auto"}:
        fork_emoji = "🔱 "

    if fmt == "verbose":
        size_kb = max(1, meta.size_bytes // 1024)
        turn = f"{meta.turn_count} turns" if meta.turn_count >= 0 else "? turns"
        archived_at = meta.archived_at[:19] if meta.archived_at else "?"
        return f"▶ {fork_emoji}{meta.short_id}  {name}  ·  {turn}  ·  {size_kb}KB  ·  {archived_at}"
    # simple (default)
    return f"▶ {fork_emoji}{meta.short_id}  {name}"


def _render_tail_sections(
    parent: Session,
    children: list[ArchivedChildMeta],
    width: int = 80,
) -> str:
    """Design B (default): append child sections at the end, with header and body for each child.

    lazy_load (option 6): when ON, show only the first 5KB per child and mark truncation.
    When OFF, show all content.
    """
    lazy = bool(get_archive_pref("archive_lazy_load"))
    max_bytes = 5 * 1024 if lazy else None

    sep = "━" * min(width, 78)
    out: list[str] = []
    out.append("")
    out.append(sep)
    out.append(f"📦 ARCHIVED CHILDREN ({len(children)})")
    out.append(sep)
    out.append("")

    for meta in children:
        out.append(_format_child_header(meta))
        out.append("─" * min(width, 78))
        out.append(_read_archive_jsonl_preview(meta.path, max_bytes=max_bytes))
        out.append("")

    return "\n".join(out)


def _read_archive_jsonl_messages(
    path: Path,
    max_bytes: Optional[int] = None,
) -> list[tuple[str, str, str]]:
    """Return archive jsonl messages as a list of (ts, role, body) tuples.

    Used by interleave rendering. Skip failures and empty messages. Lines without ts are included with an empty ts, sorting first. Uses the same filters as _read_archive_jsonl_preview, excluding system/tool/meta.
    """
    import json as _json
    out: list[tuple[str, str, str]] = []
    try:
        with path.open("rb") as fh:
            data = fh.read(max_bytes) if max_bytes is not None else fh.read()
        text = data.decode("utf-8", errors="ignore")
    except OSError:
        return out

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        typ = d.get("type")
        if typ not in {"user", "assistant"}:
            continue
        if d.get("isSidechain") or d.get("isMeta"):
            continue
        msg = d.get("message", {}) or {}
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", typ)
        content = msg.get("content", "")
        body = ""
        if isinstance(content, str):
            body = content
        elif isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        parts.append(str(item.get("text", "")))
                    elif item.get("type") == "tool_use":
                        parts.append(f"[tool: {item.get('name', '?')}]")
            body = "\n".join(parts)
        body = body.strip()
        if not body:
            continue
        if body.startswith("<system-reminder>") or body.startswith("<command-name>"):
            continue
        ts = str(d.get("timestamp", "") or "")
        out.append((ts, role, body))
    return out


def _render_interleave(
    parent: Session,
    children: list[ArchivedChildMeta],
    width: int = 80,
) -> str:
    """Design A: chronological interleave.

    Mix messages from several child archive jsonl files by timestamp into one timeline. Parent content is already shown above the preview, so this only chronologically integrates children.

    Prefix each message with child short_id and a colored dot to identify the child source (option 7 child_color_distinction).
    """
    lazy = bool(get_archive_pref("archive_lazy_load"))
    color_dist = bool(get_archive_pref("archive_child_color_distinction"))
    max_bytes = 5 * 1024 if lazy else None

    sep = "━" * min(width, 78)
    out: list[str] = []
    out.append("")
    out.append(sep)
    out.append(f"📦 ARCHIVED CHILDREN ({len(children)}) — chronological interleave")
    out.append(sep)
    out.append("")

    # Collect all (child_meta, ts, role, body) tuples and sort by ts.
    flat: list[tuple[str, ArchivedChildMeta, str, str]] = []
    for meta in children:
        for ts, role, body in _read_archive_jsonl_messages(meta.path, max_bytes=max_bytes):
            flat.append((ts, meta, role, body))
    flat.sort(key=lambda x: x[0])

    if not flat:
        out.append("  (no child messages to display)")
        return "\n".join(out)

    last_meta_id = ""
    for ts, meta, role, body in flat:
        if meta.sid != last_meta_id:
            child_tag = f"[●] " if color_dist else "[ ] "
            out.append(f"{child_tag}{_format_child_header(meta)}")
            last_meta_id = meta.sid
        prefix = "  👤" if role == "user" else "  🤖"
        ts_short = ts[11:19] if len(ts) >= 19 else ""
        ts_part = f" ({ts_short})" if ts_short else ""
        out.append(f"{prefix}{ts_part} {body[:600]}")
        out.append("")

    if lazy and max_bytes is not None:
        out.append(f"  …(first {max_bytes // 1024}KB per child only, lazy load)")
    return "\n".join(out)


def _render_headers_only(
    parent: Session,
    children: list[ArchivedChildMeta],
    width: int = 80,
) -> str:
    """Design C: headers plus a short preview (first user/assistant pair).

    A true collapsible view is not possible with TextArea, so show the first user message and first assistant message (200 chars each) as snippets. This is much shorter than tail_sections.
    """
    sep = "━" * min(width, 78)
    out: list[str] = []
    out.append("")
    out.append(sep)
    out.append(f"📦 ARCHIVED CHILDREN ({len(children)}) — headers + preview")
    out.append(sep)
    out.append("")
    for meta in children:
        out.append(_format_child_header(meta))
        if meta.auto_summary:
            summary = meta.auto_summary.replace("\n", " ")[:120]
            out.append(f"   ↳ {summary}")

        # Short snippet: first user plus first assistant message.
        msgs = _read_archive_jsonl_messages(meta.path, max_bytes=10 * 1024)
        first_user = next(((ts, b) for ts, r, b in msgs if r == "user"), None)
        first_asst = next(((ts, b) for ts, r, b in msgs if r == "assistant"), None)
        if first_user:
            body = first_user[1].replace("\n", " ")[:200]
            out.append(f"   👤 {body}")
        if first_asst:
            body = first_asst[1].replace("\n", " ")[:200]
            out.append(f"   🤖 {body}")
        out.append("")
    out.append("  (Change Settings -> preview_mode to tail_sections or interleave for full content.)")
    return "\n".join(out)


def _render_split(
    parent: Session,
    children: list[ArchivedChildMeta],
    width: int = 80,
) -> str:
    """Design D: card layout with strong visual separation by child.

    A true widget split would require a large main TUI change, so each child is shown as a box-drawing card. This separates children more clearly than the simple tail_sections divider.
    """
    lazy = bool(get_archive_pref("archive_lazy_load"))
    max_bytes = 5 * 1024 if lazy else None
    inner_w = min(width, 78) - 2

    out: list[str] = []
    out.append("")
    out.append(f"📦 ARCHIVED CHILDREN ({len(children)}) — card split")
    out.append("")

    for idx, meta in enumerate(children, 1):
        top = "┌" + "─" * inner_w + "┐"
        bot = "└" + "─" * inner_w + "┘"
        mid = "├" + "─" * inner_w + "┤"

        header_line = f" [{idx}/{len(children)}] " + _format_child_header(meta)
        out.append(top)
        out.append("│" + header_line.ljust(inner_w) + "│")
        out.append(mid)

        body = _read_archive_jsonl_preview(meta.path, max_bytes=max_bytes)
        for line in body.splitlines():
            # Padding inside the box; over-wide content is clipped instead of manually wrapped.
            # (TextArea handles wrapping, so this is OK.)
            content = line.rstrip()
            out.append("│" + content.ljust(inner_w)[:inner_w] + "│")

        out.append(bot)
        out.append("")

    return "\n".join(out)


def build_archived_children_section(
    parent: Session,
    width: int = 80,
) -> str:
    """Build archived child section text appended to the end of preview; dispatcher.

    Call one of four renderers according to `archive_preview_mode` (option 1).
    Return an empty string when there are no archived children, causing no preview impact.
    """
    try:
        children = archived_children_for(parent.id)
    except Exception:
        return ""
    if not children:
        return ""

    mode = str(get_archive_pref("archive_preview_mode"))
    if mode == "interleave":
        return _render_interleave(parent, children, width)
    if mode == "headers_only":
        return _render_headers_only(parent, children, width)
    if mode == "split":
        return _render_split(parent, children, width)
    # tail_sections (default)
    return _render_tail_sections(parent, children, width)


# ── ArchiveConfirmScreen — user confirmation modal ─────────────────────────────
# If textual is unavailable, defining this class fails and importing the module also fails; this is intentional.
# The main gccfork runs in a PEP 723 venv with textual, so this is normally OK.
# Unit tests should run in the textual venv or import only data helpers from small scripts.
class ArchiveConfirmScreen(ModalScreen[bool]):
    """Archive move confirmation modal, styled similarly to ForkNameScreen.

    Display contents:
      - N directly selected sessions plus M descendants pulled with them.
      - descendant sid and name preview (first six)
      - separate warning line when starred important sessions are included (option 3 confirm)

    Esc is handled by BINDINGS and does not propagate to App quit.
    """

    BINDINGS = [
        Binding("escape", "cancel_screen", "Cancel", show=False),
    ]

    DEFAULT_CSS = """
    #arc-box {
        background: $panel-darken-2;
        border: round $accent 50%;
        padding: 0;
        width: 96;
        max-width: 96%;
        max-height: 80%;
        height: auto;
        align: center middle;
        layout: vertical;
    }
    #arc-header {
        height: 1;
        background: $accent 16%;
        padding: 0 1;
        layout: horizontal;
    }
    #arc-brand {
        width: auto;
        min-width: 10;
        height: 1;
        color: $accent;
        background: transparent;
        text-align: left;
    }
    #arc-title {
        width: 1fr;
        height: 1;
        color: $text;
        background: transparent;
        text-align: center;
    }
    #arc-meta {
        width: auto;
        min-width: 8;
        height: 1;
        color: $text-muted;
        background: transparent;
        text-align: right;
    }
    #arc-scroll {
        height: auto;
        padding: 1 1 0 1;
        background: transparent;
    }
    .arc-section {
        height: auto;
        margin: 0 0 1 0;
        background: $panel-darken-3;
        border: round $accent 30%;
        padding: 0 1;
    }
    .arc-section-title {
        width: auto;
        height: 1;
        color: $accent;
        background: transparent;
    }
    .arc-section Static {
        width: 100%;
        background: transparent;
    }
    #arc-btn-row {
        height: 4;
        padding: 0 1;
        background: $accent 8%;
        border-top: heavy $accent;
        layout: horizontal;
    }
    #arc-btn-spacer {
        width: 1fr;
        background: transparent;
    }
    #arc-btn-row Button {
        width: 1fr;
        height: 3;
        margin: 0 1 0 0;
        min-width: 0;
        background: $accent 5%;
        color: $text;
        border: round $accent 20%;
        padding: 0 1;
        content-align: center middle;
        text-align: center;
        text-style: bold;
    }
    #arc-btn-row Button:hover {
        background: $accent 10%;
        border: round $accent 35%;
    }
    #arc-btn-row Button:focus {
        border: round $accent;
        background: $accent 16%;
        text-style: bold;
    }
    """

    def __init__(
        self,
        directly_selected: list[Session],
        descendants: list[Session],
        important_count: int,
        gccfork_version: str = "",
    ) -> None:
        super().__init__()
        self.directly_selected = directly_selected
        self.descendants = descendants
        self.important_count = important_count
        self.gccfork_version = gccfork_version
        self.total = len(directly_selected) + len(descendants)

    def compose(self) -> ComposeResult:
        with Vertical(id="arc-box"):
            with Horizontal(id="arc-header"):
                yield Static("[b]GccForK[/]", id="arc-brand", markup=True)
                yield Static("[b]🗂 Archive merge[/]", id="arc-title", markup=True)
                yield Static(
                    f"[dim]v{self.gccfork_version}[/]",
                    id="arc-meta", markup=True,
                )

            with Vertical(id="arc-scroll"):
                # Summary section
                with Vertical(classes="arc-section"):
                    yield Static(
                        "[b][INFO][/] Summary",
                        classes="arc-section-title", markup=True,
                    )
                    yield Static(
                        f"Selected: [b]{len(self.directly_selected)}[/b]  ·  "
                        f"Descendants pulled: [b]{len(self.descendants)}[/b]  ·  "
                        f"Total [b]{self.total}[/b]",
                        markup=True,
                    )
                    if self.important_count > 0:
                        yield Static(
                            f"[red]★[/red] Includes important sessions: [b]{self.important_count}[/b]",
                            markup=True,
                        )

                # direct selection section
                with Vertical(classes="arc-section"):
                    yield Static(
                        "[b][SELECTED][/] Directly selected sessions",
                        classes="arc-section-title", markup=True,
                    )
                    for s in self.directly_selected[:6]:
                        title = (s.title or "(unnamed)")[:50].replace("\n", " ")
                        star = "★ " if s.important else ""
                        yield Static(f"  {star}{s.short_id}  {title}")
                    if len(self.directly_selected) > 6:
                        yield Static(f"  …and {len(self.directly_selected) - 6} more")

                # descendant section, only when present
                if self.descendants:
                    with Vertical(classes="arc-section"):
                        yield Static(
                            "[b][DESCENDANTS][/] Descendants pulled with selection",
                            classes="arc-section-title", markup=True,
                        )
                        for s in self.descendants[:6]:
                            title = (s.title or "(unnamed)")[:50].replace("\n", " ")
                            star = "★ " if s.important else ""
                            yield Static(f"  ↳ {star}{s.short_id}  {title}")
                        if len(self.descendants) > 6:
                            yield Static(f"  …and {len(self.descendants) - 6} more")

                # behavior description
                with Vertical(classes="arc-section"):
                    yield Static(
                        "[b][WHAT HAPPENS][/] What happens",
                        classes="arc-section-title", markup=True,
                    )
                    yield Static("  • jsonl files move into archive/ and are preserved")
                    yield Static("  • registry marks them archived, so they appear under the parent in the tree")
                    yield Static("  • direct sid lookup searches archive automatically, so external .md references stay valid")
                    yield Static("  • restore is possible when trash-pattern restore is enabled in settings")

            with Horizontal(id="arc-btn-row"):
                yield Button("Esc Cancel", id="btn-arc-cancel")
                yield Static("", id="arc-btn-spacer")
                yield Button(
                    f"Archive ({self.total})",
                    id="btn-arc-confirm", variant="primary",
                )
        # CopyMenuOverlay is a main-module class and cannot be imported from this sidecar, so it is omitted here.

    def on_mount(self) -> None:
        # Initial focus is Cancel so an accidental Enter does not trigger a destructive action.
        self.query_one("#btn-arc-cancel", Button).focus()

    def action_cancel_screen(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn-arc-confirm":
            self.dismiss(True)
        elif bid == "btn-arc-cancel":
            self.dismiss(False)


# ── UnmergeConfirmScreen (unmerge confirmation modal) ──────────────────────────────
class UnmergeConfirmScreen(ModalScreen[bool]):
    """Unmerge confirmation modal, paired with ArchiveConfirmScreen.

    Shows one parent and N children merged under it, then unmerges them in bulk on confirm.
    """

    BINDINGS = [
        Binding("escape", "cancel_screen", "Cancel", show=False),
    ]

    DEFAULT_CSS = """
    #unm-box {
        background: $accent 5%;
        border: round $accent 50%;
        padding: 0;
        width: 96;
        max-width: 96%;
        max-height: 80%;
        height: auto;
        align: center middle;
        layout: vertical;
    }
    #unm-header {
        height: 1;
        background: $accent 16%;
        padding: 0 1;
        layout: horizontal;
    }
    #unm-brand {
        width: auto;
        min-width: 10;
        height: 1;
        color: $accent;
        background: transparent;
        text-align: left;
    }
    #unm-title {
        width: 1fr;
        height: 1;
        color: $accent;
        background: transparent;
        text-align: left;
        text-style: bold;
    }
    #unm-meta {
        width: auto;
        min-width: 8;
        height: 1;
        color: $text-muted;
        background: transparent;
        text-align: right;
    }
    #unm-scroll {
        height: auto;
        padding: 1 1 0 1;
        background: transparent;
    }
    /* Body SelectableTextArea supports drag-select and right-click copy. */
    #unm-body {
        height: auto;
        max-height: 24;
        width: 1fr;
        background: $accent 3%;
        border: round $accent 25%;
        padding: 0 1;
        scrollbar-size: 0 0;
    }
    #unm-body:focus {
        border: round $accent 35%;
        background: $accent 5%;
    }
    #unm-btn-row {
        height: 3;
        padding: 0 1;
        border-top: hkey $accent 25%;
        layout: horizontal;
    }
    #unm-btn-spacer {
        width: 1fr;
        background: transparent;
    }
    #unm-btn-row Button {
        width: 1fr;
        height: 3;
        margin: 0 1 0 0;
        min-width: 0;
        background: $accent 5%;
        color: $text;
        border: round $accent 20%;
        padding: 0 1;
        content-align: center middle;
        text-align: center;
        text-style: bold;
    }
    #unm-btn-row Button:hover {
        background: $accent 10%;
        border: round $accent 35%;
    }
    #unm-btn-row Button:focus {
        border: round $accent;
        background: $accent 16%;
        text-style: bold;
    }
    """

    def __init__(
        self,
        parent_session: Session,
        archived_children: list,
        gccfork_version: str = "",
    ) -> None:
        super().__init__()
        self.parent_session = parent_session
        # NOTE: `self.children` is a built-in textual Screen attribute and cannot be shadowed.
        # Use `archived_children` instead.
        self.archived_children = archived_children
        self.gccfork_version = gccfork_version
        self.total = len(archived_children)

    def compose(self) -> ComposeResult:
        # Use one SelectableTextArea for the body so drag-select and right-click copy work consistently.
        # SelectableTextArea lives in the main module, so lazy import avoids circular imports.
        try:
            from gccfork import SelectableTextArea
        except Exception:
            SelectableTextArea = None  # type: ignore

        # Build plain body text without markup because TextArea uses raw text.
        p = self.parent_session
        p_title = (p.title or "(unnamed)")[:60].replace("\n", " ")
        lines: list[str] = []
        lines.append("[PARENT] Parent to unmerge")
        lines.append(f"  📦{self.total}  {p.short_id}  {p_title}")
        lines.append("")
        lines.append(f"[CHILDREN] Children to restore in place — {self.total}")
        for c in self.archived_children[:8]:
            name = (c.name or "(unnamed)")[:50].replace("\n", " ")
            lines.append(f"  ↩ {c.short_id}  {name}")
        if len(self.archived_children) > 8:
            lines.append(f"  …and {len(self.archived_children) - 8} more")
        lines.append("")
        lines.append("[WHAT HAPPENS] What happens")
        lines.append("  • child jsonl files move from archive/ back to original project_dir (inverse operation)")
        lines.append("  • remove the four archive registry fields (archived/archived_into/archive_path/archived_at)")
        lines.append("  • parent 📦 marker disappears automatically when child count becomes 0")
        lines.append("  • deeper archived descendants remain archived; only one level is unmerged")
        body_text = "\n".join(lines)

        with Vertical(id="unm-box"):
            with Horizontal(id="unm-header"):
                yield Static("[b]GccForK[/]", id="unm-brand", markup=True)
                yield Static("[b]🔧 Unmerge[/]", id="unm-title", markup=True)
                yield Static(
                    f"[dim]v{self.gccfork_version}[/]",
                    id="unm-meta", markup=True,
                )

            with Vertical(id="unm-scroll"):
                if SelectableTextArea is not None:
                    yield SelectableTextArea(
                        body_text,
                        id="unm-body",
                        read_only=True,
                        soft_wrap=False,
                        compact=True,
                        show_line_numbers=False,
                        show_cursor=False,
                        highlight_cursor_line=False,
                    )
                else:
                    # fallback: use Static when SelectableTextArea import fails (not selectable)
                    yield Static(body_text, id="unm-body")

            with Horizontal(id="unm-btn-row"):
                yield Button("Esc Cancel", id="btn-unm-cancel")
                yield Static("", id="unm-btn-spacer")
                yield Button(
                    f"Unmerge ({self.total})",
                    id="btn-unm-confirm", variant="primary",
                )

    def on_mount(self) -> None:
        # Initial focus is Cancel so an accidental Enter does not trigger a destructive action.
        self.query_one("#btn-unm-cancel", Button).focus()

    def action_cancel_screen(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn-unm-confirm":
            self.dismiss(True)
        elif bid == "btn-unm-cancel":
            self.dismiss(False)


# ── ArchiveMixin — action methods mixed into the App ──────────────────────────
class ArchiveMixin:
    """Mixin attached to the App class to provide archive actions.

    Required methods/attributes on the App side:
      - self.sessions (list[Session])
      - self._multi_selected_ids (set[str])
      - self.notify(msg, severity=...)
      - self.push_screen(screen, callback)
      - self.reload_sessions()
      - self._update_multi_action_visibility()
      - self.refresh_list()
      - GCCFORK_VERSION global, or empty string if missing
    """

    def action_archive_selected(self) -> None:
        """Move multi-selected sessions and descendants into archive folders.

        Flow:
        1. convert selected sids to Session objects
        2. recursive descendant collection (`collect_subtree`)
        3. starred-important handling branch (opt 3):
             - auto_include: proceed directly
             - confirm: only show a warning inside ArchiveConfirmScreen, without a separate modal
             - reject: reject and notify when important sessions are included
        4. ArchiveConfirmScreen show
        5. on confirm, call archive_session for all
        """
        sel_ids: set[str] = set(getattr(self, "_multi_selected_ids", set()))
        if not sel_ids:
            try:
                self.notify("No sessions selected.", severity="warning")
            except Exception:
                pass
            return

        all_sessions: list[Session] = list(getattr(self, "sessions", []))
        directly_selected = [s for s in all_sessions if s.id in sel_ids]
        if not directly_selected:
            return

        descendants = collect_subtree([s.id for s in directly_selected], all_sessions)
        # Direct selections and descendants are disjoint because collect_subtree excludes root_sids.

        all_targets = directly_selected + descendants
        important_count = sum(1 for s in all_targets if s.important)

        # option 3: starred-important handling
        important_handling = str(get_archive_pref("archive_important_handling"))
        if important_count > 0 and important_handling == "reject":
            try:
                self.notify(
                    f"{important_count} starred important session(s) included; settings reject archiving them. "
                    "Remove the star and try again.",
                    severity="error",
                )
            except Exception:
                pass
            return

        version = ""
        try:
            import sys as _sys
            mod = _sys.modules.get("__main__")
            version = getattr(mod, "GCCFORK_VERSION", "") if mod else ""
        except Exception:
            pass

        def _on_confirm(ok: Optional[bool]) -> None:
            if not ok:
                return
            self._do_archive_batch(directly_selected, descendants)

        try:
            self.push_screen(
                ArchiveConfirmScreen(
                    directly_selected=directly_selected,
                    descendants=descendants,
                    important_count=important_count,
                    gccfork_version=version,
                ),
                _on_confirm,
            )
        except Exception as exc:
            try:
                self.notify(f"failed to open archive modal: {exc}", severity="error")
            except Exception:
                pass

    def _do_archive_batch(
        self,
        directly_selected: list[Session],
        descendants: list[Session],
    ) -> None:
        """Move files after ArchiveConfirmScreen confirmation.

        Parent sid rules:
          - Directly selected sessions → that session parent_id, or empty string when absent
          - descendants -> direct parent sid, preserving parent/child links during recursive archive
        """
        moved = 0
        failed = 0

        # Descendants must keep their direct parent as archived_into to preserve tree structure.
        for sess in descendants:
            parent_sid = sess.parent_id or ""
            if archive_session(sess, parent_sid):
                moved += 1
            else:
                failed += 1

        # Selected sessions use their own parent, or root/empty string when absent.
        for sess in directly_selected:
            parent_sid = sess.parent_id or ""
            if archive_session(sess, parent_sid):
                moved += 1
            else:
                failed += 1

        try:
            if failed:
                self.notify(
                    f"Archive: moved {moved}, failed {failed}",
                    severity="warning",
                )
            else:
                self.notify(f"🗂 Archive: moved {moved}")
        except Exception:
            pass

        # clear multi-selection and reload
        try:
            self._multi_selected_ids.clear()
        except Exception:
            pass
        try:
            self.reload_sessions()
        except Exception:
            pass
        try:
            self._update_multi_action_visibility()
        except Exception:
            pass

    def action_unmerge_selected(self) -> None:
        """Unmerge all archived children of one selected parent session, the inverse of merge.

        Active conditions, guarded by caller:
          - exactly one multi-selected session
          - that session has archived_children > 0 (📦 marker)

        Flow:
        1. selected session -> query children with archived_children_for(sid)
        2. permanent mode guard, reject entirely
        3. UnmergeConfirmScreen show
        4. on confirm, call unmerge_parent(), notify result, and reload
        """
        if str(get_archive_pref("archive_restore_enabled")) == "permanent":
            try:
                self.notify("Unmerge is disabled by permanent archive mode setting.", severity="warning")
            except Exception:
                pass
            return

        sel_ids: set[str] = set(getattr(self, "_multi_selected_ids", set()))
        if len(sel_ids) != 1:
            try:
                self.notify("Unmerge requires selecting exactly one parent.", severity="warning")
            except Exception:
                pass
            return
        target_sid = next(iter(sel_ids))

        all_sessions: list[Session] = list(getattr(self, "sessions", []))
        parent = next((s for s in all_sessions if s.id == target_sid), None)
        if parent is None:
            return

        children = archived_children_for(target_sid)
        if not children:
            try:
                self.notify("Selected session has no merged children (📦 0).", severity="warning")
            except Exception:
                pass
            return

        version = ""
        try:
            import sys as _sys
            mod = _sys.modules.get("__main__")
            version = getattr(mod, "GCCFORK_VERSION", "") if mod else ""
        except Exception:
            pass

        def _on_confirm(ok: Optional[bool]) -> None:
            if not ok:
                return
            success, fail = unmerge_parent(target_sid)
            try:
                if fail == 0:
                    self.notify(f"🔧 Unmerge complete: restored {success} child session(s)")
                else:
                    self.notify(
                        f"🔧 Unmerge partially failed: success {success} / fail {fail}",
                        severity="warning",
                    )
            except Exception:
                pass
            try:
                # clear multi-selection after unmerge; the selected parent is now a normal session
                self._multi_selected_ids.clear()
            except Exception:
                pass
            try:
                self.reload_sessions()
            except Exception:
                pass
            try:
                self._update_multi_action_visibility()
            except Exception:
                pass

        try:
            self.push_screen(
                UnmergeConfirmScreen(
                    parent_session=parent,
                    archived_children=children,
                    gccfork_version=version,
                ),
                _on_confirm,
            )
        except Exception as exc:
            try:
                self.notify(f"failed to open unmerge modal: {exc}", severity="error")
            except Exception:
                pass

    def action_restore_archived(self, sid: str) -> None:
        """Called by restore action in archive view. Reject when option 4 is permanent.

        Called by UI; used by the Phase 4 archive-view modal.
        """
        if str(get_archive_pref("archive_restore_enabled")) == "permanent":
            try:
                self.notify("Restore is disabled by permanent archive mode setting.", severity="warning")
            except Exception:
                pass
            return
        if restore_session(sid):
            try:
                self.notify(f"Restored: {sid[:8]}")
            except Exception:
                pass
            try:
                self.reload_sessions()
            except Exception:
                pass
        else:
            try:
                self.notify(f"Restore failed: {sid[:8]}", severity="error")
            except Exception:
                pass


# ── module exports ─────────────────────────────────────────────────────────
__all__ = [
    "ARCHIVE_DEFAULTS",
    "CENTRAL_ARCHIVE_ROOT",
    "ArchiveConfirmScreen",
    "ArchiveMixin",
    "ArchivedChildMeta",
    "UnmergeConfirmScreen",
    "all_archived_sessions",
    "archive_session",
    "archived_children_count",
    "archived_children_for",
    "build_archived_children_section",
    "collect_subtree",
    "find_archived_session",
    "get_archive_pref",
    "restore_session",
    "sweep_all_known_projects",
    "sweep_stale_stubs",
    "unmerge_parent",
]
