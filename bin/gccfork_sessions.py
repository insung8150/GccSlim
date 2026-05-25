"""gccfork data layer: JSONL parsing, session indexing, registry, ancestry.

Shared by the main `gccfork` app and sidecars such as cli, autoreload, and
search.

Core optimizations:
  - `parse_session` results are cached by (size, mtime) in `_PARSE_CACHE`, so
    unchanged JSONL files cost one stat plus a dict lookup.
  - `_PARSE_CACHE` is persisted to `~/.claude/gccfork-parse-cache.pickle`, so
    new processes can reuse previous parse results. mtime/size validation
    automatically invalidates stale entries.
  - `load_registry` / `load_legacy_registry` are also cached by (mtime, data),
    turning scan_sessions registry reads from N² into one read.

This module does not import the main app or other sidecars.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import pickle
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional


# ── Constants ───────────────────────────────────────────────────────────
CLAUDE_ROOT = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_ROOT / "projects"
SESSIONS_DIR = CLAUDE_ROOT / "sessions"
REGISTRY_PATH = CLAUDE_ROOT / "gccfork-registry.json"
CCFORK_LEGACY_REGISTRY_PATH = CLAUDE_ROOT / "ccfork-registry.json"


# Prefixes that can appear in message bodies but should not count as user
# utterances. Keep this aligned with the main module and settings sidecar.
INTERNAL_USER_PREFIXES = (
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<local-command-stdout>",
    "<bash-stdout>",
    "<bash-stderr>",
    "Caveat: The messages below were generated",
    "<system-reminder>",
)

UUID_SUFFIX_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
    re.IGNORECASE,
)
UUID_ANY_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)

_PARENT_4_RE = re.compile(r"\[<=\s*([0-9a-f]{4})[^\]]*\]")


# ── Small helpers ───────────────────────────────────────────────────────
def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_cwd(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    try:
        return Path(path).resolve().as_posix()
    except OSError:
        return Path(path).expanduser().as_posix()


def extract_session_id_from_path(path: Path) -> Optional[str]:
    """Claude session filenames are `<uuid>.jsonl`; the stem is the session id."""
    match = UUID_SUFFIX_RE.search(path.stem)
    return match.group(1) if match else None


def cwd_to_slug(cwd: str) -> str:
    """Claude Code project folder naming rule.

    Replaces `/`, `_`, and non-ASCII characters with `-`.
    """
    out = []
    for ch in cwd:
        if ch == "/" or ch == "_" or ord(ch) > 127:
            out.append("-")
        else:
            out.append(ch)
    return "".join(out)


def slug_to_cwd_candidates(slug: str) -> list[str]:
    """Cannot reverse reliably because slugging loses information."""
    return []


# ── live sessions/<PID>.json ────────────────────────────────────────────
def read_live_sessions() -> list[dict]:
    """Read sessions/<PID>.json for all live Claude instances.

    Stale files for dead PIDs are automatically excluded.
    """
    out: list[dict] = []
    if not SESSIONS_DIR.exists():
        return out
    for f in SESSIONS_DIR.glob("*.json"):
        try:
            pid = int(f.stem)
        except ValueError:
            continue
        if not Path(f"/proc/{pid}").exists():
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if "sessionId" not in d:
            continue
        out.append(d)
    return out


def find_live_session_by_pid(pid: int) -> Optional[dict]:
    """Return the live sessions/<PID>.json for pid, or None."""
    f = SESSIONS_DIR / f"{pid}.json"
    if not f.exists() or not Path(f"/proc/{pid}").exists():
        return None
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        return d if "sessionId" in d else None
    except (OSError, json.JSONDecodeError):
        return None


def find_live_pid_by_sid(sid: str) -> Optional[int]:
    """Return the first live Claude PID for sid.

    Deprecated for multi-instance cases; use `find_live_pids_by_sid`.
    """
    pids = find_live_pids_by_sid(sid)
    return pids[0] if pids else None


def find_live_pids_by_sid(sid: str) -> list[int]:
    """Return all live Claude PIDs for sid.

    sessions/<PID>.json is the truth source. Avoid indirect guesses such as
    cmdline, mtime, fd, or env.

    Returning all matches keeps destructive actions safe when rare races leave
    multiple PIDs on the same sid.
    """
    if not sid:
        return []
    out: list[int] = []
    for d in read_live_sessions():
        if d.get("sessionId") != sid:
            continue
        pid = d.get("pid")
        if isinstance(pid, int):
            out.append(pid)
    return out


def all_active_sid_pid_map() -> dict[str, list[int]]:
    """Return active sid -> PID list using one read_live_sessions call."""

    out: dict[str, list[int]] = {}
    for d in read_live_sessions():
        sid = d.get("sessionId")
        pid = d.get("pid")
        if not isinstance(sid, str) or not isinstance(pid, int):
            continue
        out.setdefault(sid, []).append(pid)
    return out


def _parse_parent_sid_from_name(name: Optional[str]) -> Optional[str]:
    """Extract parent sid prefix from a `[<= XXXX]` name marker."""
    if not name:
        return None
    m = _PARENT_4_RE.search(name)
    return m.group(1) if m else None


def _resolve_full_sid_from_prefix(prefix4: str, sessions_pool: list[dict]) -> Optional[str]:
    """Resolve a 4-character prefix to a full sid when uniquely matched."""
    matches = [d["sessionId"] for d in sessions_pool
               if d.get("sessionId", "").startswith(prefix4)]
    return matches[0] if len(matches) == 1 else None


def reconcile_registry_from_live_sessions(apply: bool = False) -> dict:
    """Reconcile registry entries from all active sessions/<PID>.json files.

    Rules:
      1. Missing registry entry -> create from live session data.
      2. Empty registry name + live name -> fill from live name.
      3. Different registry name -> live name wins.
      4. Missing parent_id + `[<= XXXX]` marker -> resolve and store parent.
      5. pid is updated from live state.

    apply=False is dry-run.
    """
    live_sessions = read_live_sessions()
    reg = load_registry()
    entries = reg["sessions"]

    new_list: list[dict] = []
    updated_list: list[dict] = []
    unchanged_count = 0
    skipped: list[dict] = []

    for d in live_sessions:
        sid = d["sessionId"]
        pid = d.get("pid")
        live_name = d.get("name") or ""
        existing = entries.get(sid, {}) or {}

        changes: dict = {}

        if not existing:
            if live_name:
                changes["name"] = live_name
            parent4 = _parse_parent_sid_from_name(live_name)
            if parent4:
                resolved = _resolve_full_sid_from_prefix(parent4, live_sessions)
                if resolved and resolved != sid:
                    changes["parent_id"] = resolved
            changes["pid"] = pid
            if changes:
                new_list.append({"sid": sid, "changes": changes})
                if apply:
                    registry_set(sid, **changes)
            continue

        old_name = existing.get("name") or ""
        if live_name and live_name != old_name:
            changes["name"] = live_name

        if "parent_id" not in existing:
            parent4 = _parse_parent_sid_from_name(live_name)
            if parent4:
                resolved = _resolve_full_sid_from_prefix(parent4, live_sessions)
                if resolved and resolved != sid:
                    changes["parent_id"] = resolved

        if existing.get("pid") != pid:
            changes["pid"] = pid

        if changes:
            updated_list.append({
                "sid": sid,
                "old_name": old_name,
                "live_name": live_name,
                "changes": changes,
            })
            if apply:
                registry_set(sid, **changes)
        else:
            unchanged_count += 1

    return {
        "new": new_list,
        "updated": updated_list,
        "unchanged_count": unchanged_count,
        "skipped": skipped,
        "live_count": len(live_sessions),
        "applied": apply,
    }


# ── Message text extraction ─────────────────────────────────────────────
def _extract_text_from_message(message: dict | None) -> str:
    """Extract readable text from Claude `message.content`.

    - str: use directly, common for short user inputs.
    - list: join the `text` field of `type == "text"` blocks.
    - other blocks such as tool_use, tool_result, thinking, and image are
      skipped for title/summary extraction.
    """
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "text":
                continue
            text = item.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
        return "\n".join(parts).strip()
    return ""


def _is_internal_user_text(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    for prefix in INTERNAL_USER_PREFIXES:
        if stripped.startswith(prefix):
            return True
    return False


def _append_edge_message(buf: list, msg, keep_edge: int) -> None:
    buf.append(msg)
    if len(buf) > keep_edge * 2:
        buf.pop(0)


# ── Data model ──────────────────────────────────────────────────────────
@dataclass
class Msg:
    role: str
    text: str


@dataclass
class Session:
    id: str
    jsonl_path: Path
    mtime: datetime
    turn_count: int
    size_bytes: int = 0
    first_msgs: list = field(default_factory=list)
    last_msgs: list = field(default_factory=list)
    auto_summary: Optional[str] = None
    cwd: Optional[str] = None
    source: Optional[str] = None
    originator: Optional[str] = None
    custom_name: Optional[str] = None
    parent_id: Optional[str] = None
    fork_type: Optional[str] = None
    compact_count: int = 0
    first_parent_uuid: Optional[str] = None
    ai_summary: Optional[str] = None
    live_turn_count: int = 0
    important: bool = False  # Red star marker, persisted in registry.

    @property
    def title(self) -> str:
        return self.custom_name or self.auto_summary or "(empty)"

    @property
    def short_id(self) -> str:
        return self.id[:8]


# ── registry I/O ────────────────────────────────────────────────────────
# scan_sessions used to call registry_get N² times, repeatedly reading and
# json.loads-ing the file. Cache by (mtime, data); unchanged registry reads cost
# zero file reads inside the same process.
_REGISTRY_CACHE: Optional[tuple[float, dict]] = None
_LEGACY_REGISTRY_CACHE: Optional[tuple[float, dict]] = None


def load_registry() -> dict:
    global _REGISTRY_CACHE
    if not REGISTRY_PATH.exists():
        _REGISTRY_CACHE = None
        return {"sessions": {}}
    try:
        mtime = REGISTRY_PATH.stat().st_mtime
    except OSError:
        return {"sessions": {}}
    if _REGISTRY_CACHE is not None and _REGISTRY_CACHE[0] == mtime:
        return _REGISTRY_CACHE[1]
    try:
        data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"sessions": {}}
    _REGISTRY_CACHE = (mtime, data)
    return data


def load_legacy_registry() -> dict:
    """Read legacy ccfork-registry.json as read-only data."""
    global _LEGACY_REGISTRY_CACHE
    if not CCFORK_LEGACY_REGISTRY_PATH.exists():
        _LEGACY_REGISTRY_CACHE = None
        return {"sessions": {}}
    try:
        mtime = CCFORK_LEGACY_REGISTRY_PATH.stat().st_mtime
    except OSError:
        return {"sessions": {}}
    if _LEGACY_REGISTRY_CACHE is not None and _LEGACY_REGISTRY_CACHE[0] == mtime:
        return _LEGACY_REGISTRY_CACHE[1]
    try:
        data = json.loads(CCFORK_LEGACY_REGISTRY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"sessions": {}}
    _LEGACY_REGISTRY_CACHE = (mtime, data)
    return data


def save_registry(data: dict) -> None:
    global _REGISTRY_CACHE
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    # Refresh cache with the new mtime so load_registry hits immediately.
    try:
        _REGISTRY_CACHE = (REGISTRY_PATH.stat().st_mtime, data)
    except OSError:
        _REGISTRY_CACHE = None


def registry_set(session_id: str, **fields) -> None:
    """Write only to gccfork-registry.json; never mutate legacy registry.

    Automatically invalidates parse cache entries for this sid so registry
    changes such as name/important are visible on the next reload.
    """
    reg = load_registry()
    entry = reg["sessions"].get(session_id, {})
    for key, value in fields.items():
        if value is None:
            entry.pop(key, None)
        else:
            entry[key] = value
    reg["sessions"][session_id] = entry
    save_registry(reg)
    # Invalidate the parse cache entry for this sid.
    try:
        for p in list(_PARSE_CACHE.keys()):
            cached = _PARSE_CACHE.get(p)
            if cached and len(cached) >= 3 and getattr(cached[2], "id", None) == session_id:
                invalidate_parse_cache(p)
                break
    except Exception:
        pass


def registry_get(session_id: str) -> dict:
    """Merge gccfork registry data over ccfork legacy fallback data."""
    own = load_registry()["sessions"].get(session_id, {}) or {}
    legacy = load_legacy_registry()["sessions"].get(session_id, {}) or {}
    if not legacy:
        return own
    merged = dict(legacy)
    merged.update(own)
    return merged


def registry_remove(session_id: str) -> None:
    """Remove from gccfork-registry only; legacy registry is read-only."""
    reg = load_registry()
    reg["sessions"].pop(session_id, None)
    save_registry(reg)


# ── prefs ───────────────────────────────────────────────────────────────
# Project-local prefs override:
#   <cwd>/.gccfork/ccfork-prefs.json — flat {key: value} dict.
# Policy (user choice 2026-05-08, "B"):
#   - When the project file EXISTS, reads come from it ONLY (global ignored).
#   - When it does NOT exist, reads fall back to global registry prefs.
#   - Writes follow the active scope (set_active_pref_scope):
#       * scope = "project" → write to <cwd>/.gccfork/ccfork-prefs.json
#                              (auto-create the file/dir on first write)
#       * scope = "global"  → write to ~/.claude/gccfork-registry.json prefs
#   - Settings UI default scope = "project" (per user decision).
PROJECT_PREFS_DIRNAME = ".gccfork"
PROJECT_PREFS_FILENAME = "ccfork-prefs.json"

# Module-level state — set by TUI on_mount or /slim dispatcher per-request.
_ACTIVE_PROJECT_CWD: Optional[Path] = None
_ACTIVE_PREF_SCOPE: str = "project"  # "project" or "global"


def set_active_project_cwd(cwd) -> None:
    """Set the active project cwd used by pref_get/pref_set when scope=project.
    Pass None to clear (forces global-only behaviour)."""
    global _ACTIVE_PROJECT_CWD
    _ACTIVE_PROJECT_CWD = Path(cwd) if cwd else None


def get_active_project_cwd() -> Optional[Path]:
    return _ACTIVE_PROJECT_CWD


def set_active_pref_scope(scope: str) -> None:
    """Set the active pref scope: 'project' or 'global'."""
    global _ACTIVE_PREF_SCOPE
    if scope in ("project", "global"):
        _ACTIVE_PREF_SCOPE = scope


def get_active_pref_scope() -> str:
    return _ACTIVE_PREF_SCOPE


def _project_prefs_path(cwd: Optional[Path] = None) -> Optional[Path]:
    """Return path to <cwd>/.gccfork/ccfork-prefs.json (does not check existence).
    Returns None if no active cwd."""
    base = cwd or _ACTIVE_PROJECT_CWD
    if base is None:
        return None
    return base / PROJECT_PREFS_DIRNAME / PROJECT_PREFS_FILENAME


def load_project_prefs(cwd: Optional[Path] = None) -> Optional[dict]:
    """Load project prefs flat dict. Returns None if file doesn't exist.
    Returns {} if file exists but unreadable/empty."""
    p = _project_prefs_path(cwd)
    if p is None or not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def save_project_prefs(prefs: dict, cwd: Optional[Path] = None) -> bool:
    """Atomically write project prefs to <cwd>/.gccfork/ccfork-prefs.json.
    Auto-creates the .gccfork directory. Returns True on success."""
    p = _project_prefs_path(cwd)
    if p is None:
        return False
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: tmp + os.replace
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(prefs, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, p)
        return True
    except Exception:
        return False


def load_prefs() -> dict:
    """Active prefs dict (project file if exists, else global).

    Per user policy B: project file existence acts as a hard override —
    when present, global is fully ignored (no recursive merge).
    """
    proj = load_project_prefs()
    if proj is not None:
        return proj
    reg = load_registry()
    return reg.get("prefs", {}) or {}


def save_prefs(prefs: dict) -> None:
    """Write prefs to the active scope (project or global).

    Always writes the FULL dict — caller pre-merges in pref_set().
    """
    if _ACTIVE_PREF_SCOPE == "project" and _ACTIVE_PROJECT_CWD is not None:
        if save_project_prefs(prefs):
            return
        # If project write failed for any reason, fall through to global to
        # avoid silently losing the user's change.
    reg = load_registry()
    reg["prefs"] = prefs
    save_registry(reg)


def pref_get(key: str, default=None):
    return load_prefs().get(key, default)


def pref_set(key: str, value) -> None:
    prefs = load_prefs()
    if value is None:
        prefs.pop(key, None)
    else:
        prefs[key] = value
    save_prefs(prefs)


# ── Parent inference / colors ───────────────────────────────────────────
# Runtime override for parents inferred during scan_sessions post-processing.
# Keep this in memory only because it can be recalculated from JSONL.
_RUNTIME_PARENT_OVERRIDE: dict[str, str] = {}


def _parent_for(session_id: str) -> Optional[str]:
    """Prefer registry parent, then runtime inferred parent map."""
    explicit = registry_get(session_id).get("parent_id")
    return explicit or _RUNTIME_PARENT_OVERRIDE.get(session_id)


def _compute_fork_depth(session_id: str, max_depth: int = 30) -> int:
    depth = 0
    current = session_id
    visited = {current}
    while depth < max_depth:
        parent = _parent_for(current)
        if not parent or parent in visited:
            break
        visited.add(parent)
        current = parent
        depth += 1
    return depth


_COLOR_EMOJIS = [
    "🟥", "🟧", "🟨", "🟩", "🟦", "🟪",
    "🔴", "🟠", "🟢", "🔵", "🟣", "🟤",
]
_COLOR_STYLES = [
    "red", "dark_orange", "yellow", "green", "blue", "magenta",
    "bright_red", "orange1", "bright_green", "bright_blue", "bright_magenta", "rgb(139,69,19)",
]
# User-selected 6-distinct palette order.
_DISTINCT_6_INDICES = [6, 8, 9, 2, 10, 7]

# root_id -> color_index map refreshed by the app during refresh_list.
_ROOT_COLOR_MAP: dict[str, int] = {}


def _set_root_color_map(roots: list[str]) -> None:
    """Persistently assign color indices to root sessions.

    Once assigned, a color should not change. Existing registry colors win.
    New roots get the next free slot from the 6-distinct palette, then the
    12-color pool, then a deterministic sha256 fallback.
    """
    global _ROOT_COLOR_MAP
    seen: set[str] = set()
    ordered: list[str] = []
    for r in roots:
        if r and r not in seen:
            seen.add(r)
            ordered.append(r)

    new_map: dict[str, int] = {}
    used_indices: set[int] = set()
    pending: list[str] = []
    for root in ordered:
        explicit = registry_get(root).get("color")
        if explicit and explicit in _COLOR_EMOJIS:
            idx = _COLOR_EMOJIS.index(explicit)
            new_map[root] = idx
            used_indices.add(idx)
        else:
            pending.append(root)

    palette_order = list(_DISTINCT_6_INDICES) + [
        i for i in range(len(_COLOR_EMOJIS)) if i not in _DISTINCT_6_INDICES
    ]
    for root in pending:
        slot: Optional[int] = None
        for cand in palette_order:
            if cand not in used_indices:
                slot = cand
                break
        if slot is None:
            digest = hashlib.sha256(root.encode("utf-8")).digest()
            slot = digest[0] % len(_COLOR_EMOJIS)
        new_map[root] = slot
        used_indices.add(slot)
        try:
            registry_set(root, color=_COLOR_EMOJIS[slot])
        except Exception:
            pass

    _ROOT_COLOR_MAP = new_map


_VSCODE_TERMINAL_COLORS = [
    "terminal.ansiRed",            # 🟥
    "terminal.ansiYellow",         # 🟧 (orange approximated with yellow)
    "terminal.ansiYellow",         # 🟨
    "terminal.ansiGreen",          # 🟩
    "terminal.ansiBlue",           # 🟦
    "terminal.ansiMagenta",        # 🟪
    "terminal.ansiBrightRed",      # 🔴
    "terminal.ansiBrightYellow",   # 🟠
    "terminal.ansiBrightGreen",    # 🟢
    "terminal.ansiBrightBlue",     # 🔵
    "terminal.ansiBrightMagenta",  # 🟣
    "terminal.ansiBrightBlack",    # 🟤
]


def _root_session_id(session_id: str, max_depth: int = 30) -> str:
    current = session_id
    visited = {current}
    depth = 0
    while depth < max_depth:
        parent = _parent_for(current)
        if not parent or parent in visited:
            return current
        current = parent
        visited.add(current)
        depth += 1
    return current


def _color_index_for_session(session_id: str) -> int:
    root = _root_session_id(session_id)
    explicit = registry_get(root).get("color")
    if explicit and explicit in _COLOR_EMOJIS:
        return _COLOR_EMOJIS.index(explicit)
    if root in _ROOT_COLOR_MAP:
        return _ROOT_COLOR_MAP[root]
    try:
        digest = hashlib.sha256(root.encode("utf-8")).digest()
        return digest[0] % len(_COLOR_EMOJIS)
    except Exception:
        return sum(ord(c) for c in root) % len(_COLOR_EMOJIS)


def _color_for_session(session_id: str) -> str:
    return _COLOR_EMOJIS[_color_index_for_session(session_id)]


def _color_style_for_session(session_id: str) -> str:
    return _COLOR_STYLES[_color_index_for_session(session_id)]


def _vscode_terminal_color_for_session(session_id: str) -> str:
    """ThemeColor id for VSCode terminal panel color tags."""
    return _VSCODE_TERMINAL_COLORS[_color_index_for_session(session_id)]


# ── Cache + parser + scanner ────────────────────────────────────────────
# Parse-result cache keyed by (path, size, mtime).
# value: (size, mtime, Session, frozenset[uuid]).
# Unchanged JSONL files cost one stat plus one dict lookup.
_PARSE_CACHE: dict[Path, tuple[int, float, "Session", frozenset]] = {}

# Disk-persistent cache survives process exit. On startup, restore only entries
# whose mtime/size still match current files.
_DISK_CACHE_FILE = CLAUDE_ROOT / "gccfork-parse-cache.pickle"
# Dirty flag. If unchanged, atexit skips disk writes.
_PARSE_CACHE_DIRTY = False


def invalidate_parse_cache(path: Optional[Path] = None) -> None:
    """Invalidate one parse-cache entry, or all entries when path is None."""
    global _PARSE_CACHE_DIRTY
    if path is None:
        if _PARSE_CACHE:
            _PARSE_CACHE_DIRTY = True
        _PARSE_CACHE.clear()
    else:
        if _PARSE_CACHE.pop(path, None) is not None:
            _PARSE_CACHE_DIRTY = True


def prune_parse_cache() -> int:
    """Remove entries for missing paths and return the number removed."""
    global _PARSE_CACHE_DIRTY
    stale = [p for p in _PARSE_CACHE if not p.exists()]
    for p in stale:
        _PARSE_CACHE.pop(p, None)
    if stale:
        _PARSE_CACHE_DIRTY = True
    return len(stale)


def _load_disk_cache() -> int:
    """Restore disk cache entries into memory at process startup.

    Each entry survives only when its size and mtime still match the current
    file. Missing or corrupt pickle files are ignored silently.

    Returns the number of restored entries.
    """
    if not _DISK_CACHE_FILE.exists():
        return 0
    try:
        with _DISK_CACHE_FILE.open("rb") as fh:
            disk_cache = pickle.load(fh)
    except Exception:
        return 0
    if not isinstance(disk_cache, dict):
        return 0
    restored = 0
    for path, entry in disk_cache.items():
        if not isinstance(path, Path) or not isinstance(entry, tuple) or len(entry) != 4:
            continue
        size, mtime_ts, _session, _uuids = entry
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_size == size and stat.st_mtime == mtime_ts:
            _PARSE_CACHE[path] = entry
            restored += 1
    return restored


def _save_disk_cache() -> None:
    """Save parse cache to disk at process exit.

    Registered with atexit. When `_PARSE_CACHE_DIRTY` is False, skip the write.
    """
    if not _PARSE_CACHE_DIRTY:
        return
    try:
        _DISK_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: write a temp file, then rename.
        tmp = _DISK_CACHE_FILE.with_suffix(".pickle.tmp")
        with tmp.open("wb") as fh:
            pickle.dump(dict(_PARSE_CACHE), fh, protocol=pickle.HIGHEST_PROTOCOL)
        tmp.replace(_DISK_CACHE_FILE)
    except Exception:
        pass


# Load disk cache and register atexit save at module import time.
_load_disk_cache()
atexit.register(_save_disk_cache)


def parse_session(
    jsonl_path: Path,
    keep_edge: int = 3,
    uuid_sink: Optional[set] = None,
) -> Optional[Session]:
    """Parse one Claude Code session JSONL into a `Session`.

    Each Claude-format line is a self-contained JSON event:
      {
        "sessionId": "...", "type": "user"|"assistant"|"summary"|"system",
        "message": {"role": "...", "content": str | list[block]},
        "uuid": "...", "parentUuid": "...", "cwd": "...", "version": "...",
        "isSidechain": bool, "isMeta": bool, "isCompactSummary": bool,
        "timestamp": "..."
      }

    If `uuid_sink` is provided, all message UUIDs in this session are added to it.

    Cache: when (size, mtime) match, skip full parsing and reuse the cached
    Session plus cached UUID set.
    """
    try:
        stat = jsonl_path.stat()
    except OSError:
        return None
    size = stat.st_size
    mtime_ts = stat.st_mtime

    cached = _PARSE_CACHE.get(jsonl_path)
    if cached is not None and cached[0] == size and cached[1] == mtime_ts:
        if uuid_sink is not None:
            uuid_sink.update(cached[3])
        return cached[2]

    mtime = datetime.fromtimestamp(mtime_ts)

    session_id = extract_session_id_from_path(jsonl_path)
    turn_count = 0
    first: list[Msg] = []
    last_buf: list[Msg] = []
    auto_summary: Optional[str] = None
    cwd: Optional[str] = None
    originator: Optional[str] = None
    compact_count = 0
    first_parent_uuid: Optional[str] = None
    first_real_uuid_seen = False
    live_turn_count = 0  # user turns since the last isCompactSummary

    # Always collect line UUIDs for cache, even when uuid_sink is None, because
    # a later call may request them.
    all_uuids: set[str] = set()

    try:
        with jsonl_path.open("r", encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line:
                    continue

                if '"isCompactSummary":true' in line or '"isCompactSummary": true' in line:
                    compact_count += 1
                    live_turn_count = 0  # Restart live count after compaction.

                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if not session_id:
                    sid = obj.get("sessionId")
                    if isinstance(sid, str) and sid:
                        session_id = sid
                line_cwd = obj.get("cwd")
                if isinstance(line_cwd, str) and line_cwd:
                    line_cwd_norm = normalize_cwd(line_cwd)
                    if not cwd:
                        cwd = line_cwd_norm
                    if obj.get("type") == "system" and obj.get("isMeta") is True:
                        content = obj.get("content") or ""
                        if isinstance(content, str) and (
                            "[gccfork-move]" in content or "[gccfork-copy]" in content
                        ):
                            cwd = line_cwd_norm
                if not originator:
                    ver = obj.get("version")
                    if isinstance(ver, str) and ver:
                        originator = f"claude-code {ver}"

                typ = obj.get("type")

                u = obj.get("uuid")
                if isinstance(u, str) and u:
                    all_uuids.add(u)

                if typ in {"summary", "system"}:
                    continue
                if obj.get("isSidechain") or obj.get("isMeta"):
                    continue
                if typ not in {"user", "assistant"}:
                    continue

                if not first_real_uuid_seen:
                    first_real_uuid_seen = True
                    parent_uuid = obj.get("parentUuid")
                    if isinstance(parent_uuid, str) and parent_uuid:
                        first_parent_uuid = parent_uuid

                message = obj.get("message") or {}
                role = message.get("role") or typ
                if role not in {"user", "assistant"}:
                    continue

                text = _extract_text_from_message(message)
                if role == "user" and _is_internal_user_text(text):
                    continue
                if not text:
                    continue

                msg = Msg(role=role, text=text[:2000])
                if len(first) < keep_edge * 2:
                    first.append(msg)
                _append_edge_message(last_buf, msg, keep_edge)

                if role == "user":
                    turn_count += 1
                    live_turn_count += 1
                    if not auto_summary:
                        auto_summary = text.replace("\n", " ")[:120]
    except OSError:
        return None

    if not session_id:
        return None

    if uuid_sink is not None:
        uuid_sink.update(all_uuids)

    reg = registry_get(session_id)
    fork_type = reg.get("fork_type")
    if reg.get("parent_id") and not fork_type:
        fork_type = "hard"

    session = Session(
        id=session_id,
        jsonl_path=jsonl_path,
        mtime=mtime,
        turn_count=turn_count,
        size_bytes=size,
        first_msgs=first[: keep_edge * 2],
        last_msgs=last_buf,
        auto_summary=auto_summary,
        cwd=cwd,
        source="claude-code",
        originator=originator,
        custom_name=reg.get("name"),
        parent_id=reg.get("parent_id"),
        fork_type=fork_type,
        compact_count=compact_count,
        first_parent_uuid=first_parent_uuid,
        ai_summary=reg.get("ai_summary"),
        live_turn_count=live_turn_count,
        important=bool(reg.get("important", False)),
    )

    global _PARSE_CACHE_DIRTY
    _PARSE_CACHE[jsonl_path] = (size, mtime_ts, session, frozenset(all_uuids))
    _PARSE_CACHE_DIRTY = True
    return session


def parse_session_meta_only(jsonl_path: Path) -> Optional[Session]:
    """Fast metadata-only parse for the first screen.

    - stat for size/mtime; sid from filename
    - first 5KB for header fields such as cwd, version, sessionId,
      first_parent_uuid
    - last 16KB for the latest user message -> auto_summary

    `turn_count = -1` marks "not backfilled yet"; the UI can draw a placeholder.
    uuid_sink / compact_count are not filled. Parent inference becomes accurate
    only after backfill.

    Cache hits return full parse results. Cache misses return quick metadata and
    are not stored so a later full parse can replace them.
    """
    try:
        stat = jsonl_path.stat()
    except OSError:
        return None
    size = stat.st_size
    mtime_ts = stat.st_mtime

    # Cache hits contain accurate full-parse fields such as turn_count.
    cached = _PARSE_CACHE.get(jsonl_path)
    if cached is not None and cached[0] == size and cached[1] == mtime_ts:
        return cached[2]

    mtime = datetime.fromtimestamp(mtime_ts)
    session_id = extract_session_id_from_path(jsonl_path)

    cwd: Optional[str] = None
    originator: Optional[str] = None
    first_parent_uuid: Optional[str] = None
    auto_summary: Optional[str] = None

    HEAD_BYTES = 5 * 1024     # Usually enough for cwd/version in first lines.
    TAIL_BYTES = 16 * 1024    # Enough for the latest user message or two.

    try:
        with jsonl_path.open("rb") as fh:
            # ── HEAD ────────────────────────────────────────────────────
            head_bytes = fh.read(HEAD_BYTES)
            head_text = head_bytes.decode("utf-8", errors="ignore")
            first_real_uuid_seen = False
            for line in head_text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not session_id:
                    sid = obj.get("sessionId")
                    if isinstance(sid, str) and sid:
                        session_id = sid
                line_cwd = obj.get("cwd")
                if isinstance(line_cwd, str) and line_cwd and not cwd:
                    cwd = normalize_cwd(line_cwd)
                if not originator:
                    ver = obj.get("version")
                    if isinstance(ver, str) and ver:
                        originator = f"claude-code {ver}"
                # Capture parentUuid from the first real user/assistant line.
                typ = obj.get("type")
                if not first_real_uuid_seen and typ in {"user", "assistant"}:
                    if not (obj.get("isSidechain") or obj.get("isMeta")):
                        first_real_uuid_seen = True
                        parent_uuid = obj.get("parentUuid")
                        if isinstance(parent_uuid, str) and parent_uuid:
                            first_parent_uuid = parent_uuid

            # ── TAIL ────────────────────────────────────────────────────
            # Seek near EOF and extract the latest user message.
            if size > HEAD_BYTES:
                tail_start = max(HEAD_BYTES, size - TAIL_BYTES)
                fh.seek(tail_start)
                tail_bytes = fh.read()
            else:
                tail_bytes = head_bytes  # Whole file fits in head.
            tail_text = tail_bytes.decode("utf-8", errors="ignore")
            tail_lines = tail_text.splitlines()
            # The first/last tail lines can be partial; trim the first when possible.
            if len(tail_lines) > 2:
                tail_lines = tail_lines[1:]
            # Search backwards for the latest user message.
            for raw in reversed(tail_lines):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                typ = obj.get("type")
                if typ != "user" or obj.get("isSidechain") or obj.get("isMeta"):
                    continue
                message = obj.get("message") or {}
                if message.get("role") != "user":
                    continue
                text = _extract_text_from_message(message)
                if _is_internal_user_text(text) or not text:
                    continue
                auto_summary = text.replace("\n", " ")[:120]
                break
    except OSError:
        return None

    if not session_id:
        return None

    reg = registry_get(session_id)
    fork_type = reg.get("fork_type")
    if reg.get("parent_id") and not fork_type:
        fork_type = "hard"

    return Session(
        id=session_id,
        jsonl_path=jsonl_path,
        mtime=mtime,
        turn_count=-1,            # not-backfilled marker
        size_bytes=size,
        first_msgs=[],
        last_msgs=[],
        auto_summary=auto_summary,
        cwd=cwd,
        source="claude-code",
        originator=originator,
        custom_name=reg.get("name"),
        parent_id=reg.get("parent_id"),
        fork_type=fork_type,
        compact_count=0,
        first_parent_uuid=first_parent_uuid,
        ai_summary=reg.get("ai_summary"),
        live_turn_count=0,
        important=bool(reg.get("important", False)),
    )


def _session_rank(session: Session) -> tuple[int, float, int]:
    """Reliability rank when multiple files share the same session.id.

    1. Filename suffix UUID matches actual session.id.
    2. Newer mtime.
    3. Larger file.
    """
    path_matches = int(extract_session_id_from_path(session.jsonl_path) == session.id)
    return (path_matches, session.mtime.timestamp(), session.size_bytes)


def _session_jsonl_paths(current_cwd: Optional[str], scope_all: bool) -> Iterator[Path]:
    """Iterate JSONL paths to scan.

    - scope_all: sessions from every project folder (`projects/*/*.jsonl`)
    - scope_current: only the slug folder for current cwd

    Excludes `.bak.<timestamp>.jsonl` backups so backup files never reappear as
    live sessions after trash/delete flows.
    """
    if not PROJECTS_DIR.exists():
        return
    def _is_real_session(p: Path) -> bool:
        return ".bak." not in p.stem
    if scope_all or not current_cwd:
        yield from (p for p in PROJECTS_DIR.glob("*/*.jsonl") if _is_real_session(p))
        return
    slug = cwd_to_slug(current_cwd)
    slug_dir = PROJECTS_DIR / slug
    if slug_dir.exists():
        yield from (p for p in slug_dir.glob("*.jsonl") if _is_real_session(p))


def scan_sessions(current_cwd: Optional[str], scope_all: bool) -> list[Session]:
    """Scan Claude session folders and return Session objects.

    Post-processing cross-matches `first_parent_uuid` against other sessions'
    message UUID sets to infer parent_id / fork_type. Existing registry parent
    data, such as hard forks, is respected.
    """
    target_cwd = normalize_cwd(current_cwd)
    deduped: dict[str, Session] = {}
    uuid_to_sessions: dict[str, list[str]] = {}

    for jsonl in _session_jsonl_paths(target_cwd, scope_all):
        local_uuids: set[str] = set()
        session = parse_session(jsonl, uuid_sink=local_uuids)
        if not session:
            continue
        existing = deduped.get(session.id)
        if existing is None or _session_rank(session) > _session_rank(existing):
            deduped[session.id] = session
            for u in local_uuids:
                uuid_to_sessions.setdefault(u, []).append(session.id)

    # Parent auto-inference: respect parent_id already stored in registry.
    _RUNTIME_PARENT_OVERRIDE.clear()
    for session in deduped.values():
        if session.parent_id:
            _RUNTIME_PARENT_OVERRIDE[session.id] = session.parent_id

    def _would_create_cycle(child_id: str, candidate_parent: str, max_depth: int = 30) -> bool:
        current: Optional[str] = candidate_parent
        visited = {child_id}
        for _ in range(max_depth):
            if current is None:
                return False
            if current in visited:
                return True
            visited.add(current)
            nxt = _RUNTIME_PARENT_OVERRIDE.get(current)
            if nxt is None:
                nxt = registry_get(current).get("parent_id")
            current = nxt
        return False

    for session in deduped.values():
        if session.parent_id:
            continue
        if not session.first_parent_uuid:
            continue
        candidates = uuid_to_sessions.get(session.first_parent_uuid, [])
        candidates_older = []
        for sid in candidates:
            if sid == session.id:
                continue
            cand = deduped.get(sid)
            if cand is None:
                continue
            if cand.mtime >= session.mtime:
                continue
            candidates_older.append(cand)
        if not candidates_older:
            continue
        parent_session = max(candidates_older, key=lambda s: s.mtime)
        parent_sid = parent_session.id
        if _would_create_cycle(session.id, parent_sid):
            continue
        session.parent_id = parent_sid
        _RUNTIME_PARENT_OVERRIDE[session.id] = parent_sid
        if not session.fork_type:
            session.fork_type = "auto"

    out = list(deduped.values())
    out.sort(key=lambda item: item.mtime, reverse=True)
    return out


def scan_sessions_fast(current_cwd: Optional[str], scope_all: bool) -> list[Session]:
    """Fast metadata-only scan for the first screen.

    Cache hits return accurate full-parse results. Cache misses use
    `parse_session_meta_only`. Parent inference is limited to registry parent_id
    until backfill rebuilds the uuid_to_sessions index.

    After the backfill worker completes, call `scan_sessions` again for the
    accurate tree.
    """
    target_cwd = normalize_cwd(current_cwd)
    deduped: dict[str, Session] = {}
    for jsonl in _session_jsonl_paths(target_cwd, scope_all):
        session = parse_session_meta_only(jsonl)
        if not session:
            continue
        existing = deduped.get(session.id)
        if existing is None or _session_rank(session) > _session_rank(existing):
            deduped[session.id] = session

    # Phase-1 parent inference: use explicit registry parent_id only.
    _RUNTIME_PARENT_OVERRIDE.clear()
    for session in deduped.values():
        if session.parent_id:
            _RUNTIME_PARENT_OVERRIDE[session.id] = session.parent_id

    out = list(deduped.values())
    out.sort(key=lambda item: item.mtime, reverse=True)
    return out


def reinfer_parents_from_cache(sessions: list[Session]) -> None:
    """Infer parents from cached UUID sets after all sessions are fully parsed.

    Reads UUID frozensets from `_PARSE_CACHE`, builds a uuid_to_sessions index,
    and matches sessions without parent_id via first_parent_uuid. Results are
    written to `session.parent_id` and `_RUNTIME_PARENT_OVERRIDE`.
    """
    by_id: dict[str, Session] = {s.id: s for s in sessions}

    # uuid -> session id list, using cache frozensets.
    uuid_to_sessions: dict[str, list[str]] = {}
    for session in sessions:
        cached = _PARSE_CACHE.get(session.jsonl_path)
        if cached is None:
            continue
        for u in cached[3]:  # frozenset of uuids
            uuid_to_sessions.setdefault(u, []).append(session.id)

    def _would_create_cycle(child_id: str, candidate_parent: str, max_depth: int = 30) -> bool:
        current: Optional[str] = candidate_parent
        visited = {child_id}
        for _ in range(max_depth):
            if current is None:
                return False
            if current in visited:
                return True
            visited.add(current)
            nxt = _RUNTIME_PARENT_OVERRIDE.get(current)
            if nxt is None:
                nxt = registry_get(current).get("parent_id")
            current = nxt
        return False

    for session in sessions:
        if session.parent_id:
            continue
        if not session.first_parent_uuid:
            continue
        candidates = uuid_to_sessions.get(session.first_parent_uuid, [])
        candidates_older = []
        for sid in candidates:
            if sid == session.id:
                continue
            cand = by_id.get(sid)
            if cand is None:
                continue
            if cand.mtime >= session.mtime:
                continue
            candidates_older.append(cand)
        if not candidates_older:
            continue
        parent_session = max(candidates_older, key=lambda s: s.mtime)
        parent_sid = parent_session.id
        if _would_create_cycle(session.id, parent_sid):
            continue
        session.parent_id = parent_sid
        _RUNTIME_PARENT_OVERRIDE[session.id] = parent_sid
        if not session.fork_type:
            session.fork_type = "auto"


def find_session_by_id(session_id: str) -> Optional[Session]:
    if not PROJECTS_DIR.exists():
        return None
    best: Optional[Session] = None
    for jsonl in PROJECTS_DIR.glob("*/*.jsonl"):
        if jsonl.stem != session_id:
            continue
        session = parse_session(jsonl)
        if not session or session.id != session_id:
            continue
        if best is None or _session_rank(session) > _session_rank(best):
            best = session
    if best is not None:
        return best
    # If not found in active JSONL files, search archive lazily to avoid cycles.
    try:
        from gccfork_archive import find_archived_session
    except ImportError:
        return None
    archive_path = find_archived_session(session_id)
    if archive_path is None:
        return None
    return parse_session(archive_path)
