"""gccfork_merge — True Merge sidecar (Phase 6 / model B).

Merge creates a new session N (UUID4), prebuilds every stitching variant, and
chooses the active jsonl according to prefs.

Principles:
  P1. New sid (UUID4)
  P2. Common prefix plus per-session unique tails
  P3. Create all five variants under .merged/<N.sid>/method-*.jsonl
  P4. Copy active <project_dir>/<N.sid>.jsonl from pref `merge_stitching_method`
  P5. Preserve child sids permanently; only mark archived=true and move jsonl to archive/
  P6. Bidirectional tracking (find_archived_session <-> archived_children_for)
  P7. Unmerge is the exact inverse: zero N traces and originals restored
  P8. Every sessionId in variant jsonl files equals N.sid
  P9. The linear method preserves parentUuid chain integrity
  P10. No common ancestor raises NoCommonAncestorError

Reuses archive_session / restore_session from gccfork_archive.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from gccfork_sessions import (
    Session,
    all_active_sid_pid_map,
    pref_get,
    pref_set,
    registry_get,
    registry_remove,
    registry_set,
)
from gccfork_archive import (
    archive_session,
    archived_children_for,
    restore_session,
)

# Textual UI imports for MergeConfirmScreen and Mixin.
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, RadioButton, RadioSet, Static


# ── constants ─────────────────────────────────────────────────────────────────
STITCHING_METHODS: tuple[str, ...] = (
    "linear",        # common + each unique tail chained in selection order
    "interleave",    # common + all unique messages timestamp-sorted into one chain
    "parallel",      # common + each unique tail preserving original parentUuid, keeping branches
    "common-only",   # common only; no draft/integrated tail
    "as-sections",   # common + section-divider system messages + each unique tail
)

MERGE_DEFAULTS: dict[str, object] = {
    "merge_stitching_method": "interleave",
    # Apply automatic strong in-place slim to N (integrated jsonl) right after merge. This prevents first-resume auto-compact when the merged file is heavy, as in the ca09 21 MB case. Users can toggle it in the modal; default ON.
    "merge_auto_slim_after": True,
    "merge_auto_slim_mode": "strong",
}


class NoCommonAncestorError(ValueError):
    """Raised when selected sessions do not share a common ancestor in the registry parent_id chain."""


class ActiveSessionInMergeError(ValueError):
    """Raised when merge sources include a currently running Claude session sid.

    Archiving an active session can make Claude keep writing to the moved jsonl path, create a stub, and corrupt registry metadata (2026-05-04 incident). This error prevents that scenario.
    """
    def __init__(self, active_sids: list[str]):
        self.active_sids = active_sids
        msg = (
            f"{len(active_sids)} active Claude session(s) are included in merge targets. "
            f"Quit those sessions with /quit first, then try again. "
            f"sids: {[s[:8] for s in active_sids]}"
        )
        super().__init__(msg)


# ── jsonl I/O ────────────────────────────────────────────────────────────
def _read_jsonl_messages(path: Path) -> list[dict]:
    """Return every valid jsonl line as dicts, skipping blank or broken lines."""
    out: list[dict] = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _write_jsonl_messages(path: Path, msgs: list[dict]) -> None:
    """Atomically write msgs as jsonl using tmp -> rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for m in msgs:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
    tmp.replace(path)


def _replace_sid(msg: dict, new_sid: str) -> dict:
    """Copy a message dict and replace only its sessionId with new_sid."""
    out = dict(msg)
    out["sessionId"] = new_sid
    return out


def _origin_prefix(orig_sid: str, timestamp: Optional[str]) -> str:
    """Origin display prefix for interleave mode. Format: '[<sid8> HH:MM] '."""
    sid8 = (orig_sid or "")[:8] or "????????"
    hhmm = ""
    if timestamp and len(timestamp) >= 16:
        # ISO8601 'YYYY-MM-DDTHH:MM:SS...' → 'HH:MM'
        hhmm = timestamp[11:16]
    return f"[{sid8} {hhmm}] " if hhmm else f"[{sid8}] "


def _inject_origin_prefix(msg: dict, orig_sid: str) -> dict:
    """Inject an origin prefix before user/assistant message body text.

    - role != user/assistant returns unchanged; system/metadata are untouched
    - string content -> prepend
    - list[block] content -> prepend to the first text block.
                              if there is no text block (only tool_use), insert a new text block at the front
    Only message/content are rebuilt, leaving the original dict untouched and preserving shallow chains.
    """
    body = msg.get("message")
    if not isinstance(body, dict):
        return msg
    role = body.get("role")
    if role not in ("user", "assistant"):
        return msg

    prefix = _origin_prefix(orig_sid, msg.get("timestamp"))
    new_body = dict(body)
    content = body.get("content")

    if isinstance(content, str):
        new_body["content"] = prefix + content
    elif isinstance(content, list):
        new_content = [dict(b) if isinstance(b, dict) else b for b in content]
        injected = False
        for b in new_content:
            if isinstance(b, dict) and b.get("type") == "text":
                b["text"] = prefix + b.get("text", "")
                injected = True
                break
        if not injected:
            new_content.insert(0, {"type": "text", "text": prefix.rstrip()})
        new_body["content"] = new_content
    else:
        # None or unexpected content type: leave unchanged
        return msg

    out = dict(msg)
    out["message"] = new_body
    return out


# ── content-based diff key ──────────────────────────────────────────────
# Identify messages by human-readable content only, ignoring metadata such as UUID/timestamp. Hard clones with identical content but new sid/uuid still match prefixes.
_META_KEYS_STRIP = {
    "uuid", "parentUuid", "sessionId", "requestId", "timestamp",
    "id", "tool_use_id", "cache_control",
}


def _strip_meta(obj):
    """Recursively strip metadata keys from dict/list while preserving body text."""
    if isinstance(obj, dict):
        return {
            k: _strip_meta(v) for k, v in obj.items()
            if k not in _META_KEYS_STRIP
        }
    if isinstance(obj, list):
        return [_strip_meta(x) for x in obj]
    return obj


def _msg_content_key(msg: dict) -> str:
    """sha256 hash of a message after removing metadata and normalizing body content.

    Same content produces the same key, so prefixes match even with different UUIDs.
    Hard clone, fork, and identical prompt reruns produce the same key.
    """
    cleaned = _strip_meta(msg)
    canonical = json.dumps(cleaned, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ── common/unique split ──────────────────────────────────────────────────────
def extract_common_and_unique(
    sessions: list[Session],
) -> tuple[list[dict], dict[str, list[dict]]]:
    """Extract common prefix plus per-session unique tails from selected sessions.

    Common prefix is the longest prefix where every session line i has the same content key (sha256 excluding metadata).
    Unique tail is each session content after the common prefix.

    Comparison is content-based rather than UUID-based, so hard clones with same content and new sid/uuid match correctly.

    Returns:
        (common_msgs, unique_by_sid)
    """
    if not sessions:
        return [], {}
    msgs_by_sid: dict[str, list[dict]] = {
        s.id: _read_jsonl_messages(s.jsonl_path) for s in sessions
    }
    min_len = min(len(v) for v in msgs_by_sid.values()) if msgs_by_sid else 0
    common: list[dict] = []
    common_end = 0
    first_sid = sessions[0].id
    for i in range(min_len):
        keys_at_i = {_msg_content_key(msgs_by_sid[s.id][i]) for s in sessions}
        if len(keys_at_i) == 1:
            common.append(msgs_by_sid[first_sid][i])
            common_end = i + 1
        else:
            break
    unique: dict[str, list[dict]] = {
        s.id: msgs_by_sid[s.id][common_end:] for s in sessions
    }
    return common, unique


# ── LCA (registry parent_id chain) ──────────────────────────────────────
def _ancestor_chain(sid: str) -> list[str]:
    """parent_id chain from sid to root, including self, descendant-to-ancestor order."""
    out: list[str] = []
    cur: Optional[str] = sid
    seen: set[str] = set()
    while cur and cur not in seen:
        out.append(cur)
        seen.add(cur)
        e = registry_get(cur)
        cur = e.get("parent_id") if e else None
    return out


def find_lca(sessions: list[Session]) -> Optional[str]:
    """Closest descendant among the intersection of every session ancestor chain, i.e. LCA.

    Returns:
        LCA sid, or None when there is no common ancestor.
        For a single session, return that sid itself.
    """
    if not sessions:
        return None
    chains = [_ancestor_chain(s.id) for s in sessions]
    # chains[0] is descendant-to-ancestor; walk from descendant side and choose the first sid present in all other chains
    for candidate in chains[0]:
        if all(candidate in chain for chain in chains[1:]):
            return candidate
    return None


# ── stitching helper ────────────────────────────────────────────────────
def _last_anchor_uuid(msgs: list[dict]) -> Optional[str]:
    """Walk msgs in reverse and return the first non-None uuid, or None.

    Used as the chain stitching anchor. If the last common message is metadata (agent-name / permission-mode / custom-title system event without uuid), common[-1].get("uuid") is None and the next unique message parentUuid becomes None, causing Claude resume to treat it as a chain root and hide previous history.

    This helper skips metadata and uses the uuid of the last real conversational user/assistant/tool/system message as anchor.
    """
    for m in reversed(msgs):
        u = m.get("uuid")
        if u:
            return u
    return None


# ── five stitching methods ───────────────────────────────────────────────────────
def _stitch_linear(
    common: list[dict],
    unique_by_sid: dict[str, list[dict]],
    sids_in_order: list[str],
    new_sid: str,
) -> list[dict]:
    """Chain common plus each session unique tail in order, reconnecting parentUuid.

    Skip None values when updating last_uuid for metadata messages, so the next sid first message points to the real chain anchor rather than metadata uuid=None.
    """
    out: list[dict] = [_replace_sid(m, new_sid) for m in common]
    last_uuid: Optional[str] = _last_anchor_uuid(common)
    for sid in sids_in_order:
        unique = unique_by_sid.get(sid, [])
        for i, msg in enumerate(unique):
            new_msg = _replace_sid(msg, new_sid)
            if i == 0:
                new_msg["parentUuid"] = last_uuid
            out.append(new_msg)
            new_uuid = msg.get("uuid")
            if new_uuid:
                last_uuid = new_uuid
    return out


def _stitch_interleave(
    common: list[dict],
    unique_by_sid: dict[str, list[dict]],
    sids_in_order: list[str],
    new_sid: str,
) -> list[dict]:
    """Chain common plus all unique messages sorted by timestamp ascending.

    Inject '[<sid8> HH:MM] ' origin prefix before each user/assistant message body.
    (origin marker so Claude UI shows which branch it came from).
    Skip None values while updating last_uuid, because metadata messages do not anchor the chain.
    """
    out: list[dict] = [_replace_sid(m, new_sid) for m in common]
    flat_with_origin: list[tuple[str, dict]] = []
    for sid in sids_in_order:
        for msg in unique_by_sid.get(sid, []):
            flat_with_origin.append((sid, msg))
    flat_with_origin.sort(key=lambda pair: pair[1].get("timestamp") or "")
    last_uuid: Optional[str] = _last_anchor_uuid(common)
    for orig_sid, msg in flat_with_origin:
        new_msg = _replace_sid(msg, new_sid)
        new_msg = _inject_origin_prefix(new_msg, orig_sid)
        new_msg["parentUuid"] = last_uuid
        out.append(new_msg)
        new_uuid = msg.get("uuid")
        if new_uuid:
            last_uuid = new_uuid
    return out


def _stitch_parallel(
    common: list[dict],
    unique_by_sid: dict[str, list[dict]],
    sids_in_order: list[str],
    new_sid: str,
) -> list[dict]:
    """Common plus each unique tail preserving original parentUuid, keeping branches intact."""
    out: list[dict] = [_replace_sid(m, new_sid) for m in common]
    for sid in sids_in_order:
        for msg in unique_by_sid.get(sid, []):
            out.append(_replace_sid(msg, new_sid))
    return out


def _stitch_common_only(
    common: list[dict],
    unique_by_sid: dict[str, list[dict]],
    sids_in_order: list[str],
    new_sid: str,
) -> list[dict]:
    """Common prefix only; drop all unique tails."""
    return [_replace_sid(m, new_sid) for m in common]


def _stitch_as_sections(
    common: list[dict],
    unique_by_sid: dict[str, list[dict]],
    sids_in_order: list[str],
    new_sid: str,
) -> list[dict]:
    """Common plus synthetic section-divider system messages plus each unique tail."""
    out: list[dict] = [_replace_sid(m, new_sid) for m in common]
    last_uuid: Optional[str] = _last_anchor_uuid(common)
    for sid in sids_in_order:
        unique = unique_by_sid.get(sid, [])
        if not unique:
            continue
        divider_uuid = f"div-{sid[:8]}-{uuid.uuid4().hex[:8]}"
        divider = {
            "uuid": divider_uuid,
            "parentUuid": last_uuid,
            "sessionId": new_sid,
            "type": "system",
            "message": {"role": "system", "content": f"──── branch {sid[:8]} ────"},
            "timestamp": unique[0].get("timestamp") or "",
            "isMergeDivider": True,
        }
        out.append(divider)
        last_uuid = divider_uuid
        for msg in unique:
            new_msg = _replace_sid(msg, new_sid)
            new_msg["parentUuid"] = last_uuid
            out.append(new_msg)
            new_uuid = msg.get("uuid")
            if new_uuid:
                last_uuid = new_uuid
    return out


_STITCHERS = {
    "linear": _stitch_linear,
    "interleave": _stitch_interleave,
    "parallel": _stitch_parallel,
    "common-only": _stitch_common_only,
    "as-sections": _stitch_as_sections,
}


# ── helpers for N project_dir and variants folder ──────────────────────────────
def _project_dir_for(sessions: list[Session]) -> Path:
    """Common project_dir for selected sessions; assumes all are the same."""
    return sessions[0].jsonl_path.parent


def _variants_dir(project_dir: Path, new_sid: str) -> Path:
    return project_dir / ".merged" / new_sid


def _active_path(project_dir: Path, new_sid: str) -> Path:
    return project_dir / f"{new_sid}.jsonl"


# ── public API ────────────────────────────────────────────────────────────
def merge_into_new_session(
    sessions: list[Session],
    name: Optional[str] = None,
) -> str:
    """Model B merge: create new sid N, create five variants, and mark children archived.

    Args:
        sessions: merge targets; >=2 recommended, one session behaves like simple archive
        name: custom_name for N; auto-generated when None

    Returns:
        sid of N as UUID4 string

    Raises:
        NoCommonAncestorError: selected sessions have no common ancestor in registry.
        ValueError: sessions is empty.
    """
    if not sessions:
        raise ValueError("merge_into_new_session: sessions is empty")

    # Safety guard §1: block active sids to prevent the 2026-05-04 incident.
    # Putting active sessions in sources lets Claude create stubs after archive, corrupt registry metadata, and make unmerge miss some children. Block this up front.
    active_map = all_active_sid_pid_map()
    active_in_sources = [s.id for s in sessions if s.id in active_map]
    if active_in_sources:
        raise ActiveSessionInMergeError(active_in_sources)

    # Check common ancestor. For one session, the session itself is LCA and passes.
    lca = find_lca(sessions)
    if lca is None and len(sessions) > 1:
        raise NoCommonAncestorError(
            f"No common ancestor; unrelated sessions cannot be merged "
            f"(sids: {[s.id[:8] for s in sessions]})"
        )

    # assign new sid
    new_sid = str(uuid.uuid4())
    project_dir = _project_dir_for(sessions)
    variants_dir = _variants_dir(project_dir, new_sid)
    variants_dir.mkdir(parents=True, exist_ok=True)

    # ── Create five variants with fold-merge, replacing old stitchers. ──
    # Previous LCA/content-key approach is replaced by fold-merge UUID intersection.
    # Safely handles slimmed jsonl and guarantees zero missing / zero duplicate messages.
    from gccfork_merge_fold import (
        split_common_and_unique as _fold_split,
        STITCHERS as _FOLD_STITCHERS,
    )

    sources_in_order = [s.jsonl_path for s in sessions]
    common, unique_by_path = _fold_split(sources_in_order)

    # NoCommonAncestorError compatibility: common 0 with more than one session means no real common ancestor.
    if not common and len(sessions) > 1:
        raise NoCommonAncestorError(
            f"UUID intersection is zero; no common messages "
            f"(sids: {[s.id[:8] for s in sessions]})"
        )

    for method, stitcher in _FOLD_STITCHERS.items():
        msgs = stitcher(common, unique_by_path, new_sid)
        _write_jsonl_messages(variants_dir / f"method-{method}.jsonl", msgs)

    sids_in_order = [s.id for s in sessions]

    # registry N entry: parent_id is LCA when LCA is parent of selection, otherwise None
    # Simplification: if any selected session is the LCA, use that LCA's parent as N's parent.
    n_parent_id: Optional[str] = lca
    if lca and lca in sids_in_order:
        # if LCA is one of the selections, that LCA parent_id becomes N parent
        lca_entry = registry_get(lca)
        n_parent_id = lca_entry.get("parent_id") if lca_entry else None

    auto_name = name or f"🗂 merged: {len(sessions)}"
    registry_set(
        new_sid,
        parent_id=n_parent_id,
        name=auto_name,
        merged_from=sids_in_order,
        merged_at=datetime.now(timezone.utc).isoformat(),
        is_merged=True,
    )

    # choose and copy active jsonl
    sync_active_jsonl(new_sid, project_dir=project_dir)

    # archive children using existing archive_session with archived_into set to N
    for s in sessions:
        archive_session(s, parent_sid=new_sid)

    return new_sid


def sync_active_jsonl(
    new_sid: str,
    project_dir: Optional[Path] = None,
) -> bool:
    """Copy active jsonl from variant according to pref `merge_stitching_method`.

    Args:
        new_sid: sid of merged result N
        project_dir: pass when known; None infers from archived children.

    Returns:
        True on success, False when not found.
    """
    method = str(pref_get("merge_stitching_method") or MERGE_DEFAULTS["merge_stitching_method"])
    if method not in STITCHING_METHODS:
        method = MERGE_DEFAULTS["merge_stitching_method"]

    if project_dir is None:
        # infer from children archive_path
        children = archived_children_for(new_sid)
        if children:
            # archive/<jsonl> → archive/ → project_dir
            project_dir = children[0].path.parent.parent
    if project_dir is None:
        return False

    src = _variants_dir(project_dir, new_sid) / f"method-{method}.jsonl"
    dst = _active_path(project_dir, new_sid)
    if not src.exists():
        return False
    shutil.copy2(src, dst)
    return True


def is_merge_pristine(new_sid: str) -> bool:
    """N active jsonl is pristine when it matches any variant, otherwise dirty.

    pristine means immediately after merge or only method-switched, with zero user-added work
    dirty means new messages were added, for example by claude --resume

    Returns:
        True means pristine and safe to delete completely
        False means dirty and needs preserve-unmerge to avoid losing new work
    """
    children = archived_children_for(new_sid)
    if not children:
        # With no children there is no comparison baseline; consider pristine (safe).
        return True
    project_dir = children[0].path.parent.parent
    active = _active_path(project_dir, new_sid)
    if not active.exists():
        return True
    variants_dir = _variants_dir(project_dir, new_sid)
    if not variants_dir.exists():
        return True
    try:
        active_bytes = active.read_bytes()
    except OSError:
        return True
    for variant in variants_dir.glob("method-*.jsonl"):
        try:
            if variant.read_bytes() == active_bytes:
                return True
        except OSError:
            continue
    return False


def count_new_lines_since_merge(new_sid: str) -> int:
    """active line count minus largest variant line count, a rough new-turn count.

    Approximate only because variants can have different line counts and users may add work after method switches. Exact counts require jsonl diff; this is for UI display.
    """
    children = archived_children_for(new_sid)
    if not children:
        return 0
    project_dir = children[0].path.parent.parent
    active = _active_path(project_dir, new_sid)
    if not active.exists():
        return 0
    try:
        active_count = sum(1 for line in active.read_text(encoding="utf-8").splitlines() if line.strip())
    except OSError:
        return 0
    variants_dir = _variants_dir(project_dir, new_sid)
    if not variants_dir.exists():
        return active_count
    max_v = 0
    for v in variants_dir.glob("method-*.jsonl"):
        try:
            c = sum(1 for line in v.read_text(encoding="utf-8").splitlines() if line.strip())
        except OSError:
            continue
        if c > max_v:
            max_v = c
    return max(0, active_count - max_v)


def unmerge_new_session(new_sid: str, mode: str = "auto") -> bool:
    """Merge inverse: automatically branch on pristine/dirty (mode="auto") or force mode.

    mode:
      "auto"     — pristine -> full delete; dirty -> preserve N and restore only children
      "delete"   — always delete fully; dangerous when dirty because new turns are lost
      "preserve" — always preserve N as an independent session, even when pristine

    Common work for all modes:
      - archived_children_for(N) → restore_session() for each child
      - .merged/<N.sid>/ delete variant folder; no archival value remains
      - .merged/ remove empty .merged directory as cosmetic cleanup

    mode='deleted' additional work:
      - delete active jsonl of N
      - remove N registry entry entirely, so sid disappears

    mode='preserved' additional work:
      - keep active jsonl of N to preserve user-added work
      - registry: remove only is_merged / merged_from / merged_at
      - keep sid + parent_id + custom_name, so it appears as an independent session in the tree
      - external references such as `claude --resume <N.sid>` and .md links remain valid

    Returns:
        True on success; False when N does not exist, etc.
    """
    n_entry = registry_get(new_sid)
    if not n_entry:
        return False

    if mode == "auto":
        action = "deleted" if is_merge_pristine(new_sid) else "preserved"
    elif mode in ("delete", "preserve"):
        action = "deleted" if mode == "delete" else "preserved"
    else:
        action = "deleted" if is_merge_pristine(new_sid) else "preserved"

    children = archived_children_for(new_sid)
    project_dir: Optional[Path] = None
    if children:
        project_dir = children[0].path.parent.parent

    # restore children, common to all modes
    for c in children:
        restore_session(c.sid)

    # .merged/<N.sid>/ delete folder, common to all modes; no longer meaningful
    if project_dir is not None:
        variants = _variants_dir(project_dir, new_sid)
        if variants.exists():
            try:
                shutil.rmtree(variants)
            except OSError:
                pass
        merged_root = project_dir / ".merged"
        try:
            if merged_root.exists() and not any(merged_root.iterdir()):
                merged_root.rmdir()
        except OSError:
            pass

    if action == "deleted":
        # delete active jsonl and remove registry entry entirely
        if project_dir is not None:
            active = _active_path(project_dir, new_sid)
            if active.exists():
                try:
                    active.unlink()
                except OSError:
                    pass
        registry_remove(new_sid)
    else:
        # preserve: keep active jsonl and remove only merge traces from registry
        # Safety guard §6: explicitly preserve core fields so entry does not become an empty dict.
        # (Previously, popping only is_merged/merged_from/merged_at could leave entry empty.)
        existing = n_entry or {}
        keep_name = existing.get("name") or "🗂 (separated)"
        keep_parent = existing.get("parent_id")
        keep_fork_type = existing.get("fork_type")
        registry_set(
            new_sid,
            is_merged=None,
            merged_from=None,
            merged_at=None,
            name=keep_name,
            parent_id=keep_parent,
            fork_type=keep_fork_type,
            unmerged_at=datetime.now(timezone.utc).isoformat(),
        )

    return True


# ── MergeConfirmScreen (merge confirmation modal, model B) ───────────────────
# Follows the design philosophy in CLAUDE.md sections 1-5:
#   - colors use only the $accent percentage ladder
#   - borders use round $accent percentages
#   - header is left-aligned, bold, and accent-colored
#   - 8-grid spacing and four widget levels
class MergeConfirmScreen(ModalScreen[Optional[dict]]):
    """Merge confirmation modal with analysis preview, method choice, and name input.

    dismiss(None)              # cancel
    dismiss({"method": ..., "name": ...})  # confirm
    """

    BINDINGS = [
        Binding("escape", "cancel_screen", "Cancel", show=False),
    ]

    DEFAULT_CSS = """
    #merge-box {
        background: $accent 5%;
        border: round $accent 35%;
        padding: 0;
        width: 100;
        max-width: 96%;
        height: 90%;
        align: center middle;
        layout: vertical;
    }
    #merge-header {
        height: 1;
        background: $accent 16%;
        padding: 0 1;
        layout: horizontal;
    }
    #merge-brand {
        width: auto;
        min-width: 10;
        height: 1;
        color: $accent;
        background: transparent;
        text-align: left;
    }
    #merge-title {
        width: 1fr;
        height: 1;
        color: $accent;
        background: transparent;
        text-align: left;
        text-style: bold;
    }
    #merge-meta {
        width: auto;
        min-width: 8;
        height: 1;
        color: $text-muted;
        background: transparent;
        text-align: right;
    }
    #merge-scroll {
        height: 1fr;
        padding: 1 2;
        overflow-y: auto;
        scrollbar-size: 1 1;
    }
    .merge-section {
        height: auto;
        margin: 0 0 1 0;
        background: $accent 5%;
        border: round $accent 20%;
        padding: 0 1;
    }
    .merge-section-title {
        height: 1;
        color: $accent;
        background: transparent;
        text-style: bold;
    }
    #merge-method-set {
        height: auto;
        background: transparent;
        border: none;
        padding: 0;
    }
    #merge-method-set RadioButton {
        background: $accent 5%;
        border: round $accent 20%;
        padding: 0 1;
        margin: 0 0 0 0;
    }
    #merge-method-set RadioButton:hover {
        background: $accent 10%;
        border: round $accent 35%;
    }
    #merge-method-set RadioButton:focus {
        background: $accent 16%;
        border: round $accent;
    }
    #merge-name-input {
        width: 100%;
        height: 3;
        background: $accent 5%;
        border: round $accent 20%;
        padding: 0 1;
    }
    #merge-name-input:focus {
        background: $accent 10%;
        border: round $accent;
    }
    #merge-btn-row {
        height: 4;
        padding: 0 1;
        background: $accent 8%;
        border-top: hkey $accent 30%;
        layout: horizontal;
        dock: bottom;
    }
    .merge-spacer {
        width: 1fr;
        background: transparent;
    }
    #merge-btn-row Button {
        width: auto;
        min-width: 16;
        height: 3;
        margin: 0 1 0 0;
        background: $accent 5%;
        color: $text;
        border: round $accent 20%;
        padding: 0 2;
        content-align: center middle;
        text-align: center;
        text-style: bold;
    }
    #merge-btn-row Button:hover {
        background: $accent 10%;
        border: round $accent 35%;
    }
    #merge-btn-row Button:focus {
        background: $accent 16%;
        border: round $accent;
    }
    """

    # Short visible method labels plus stable method ids.
    _METHOD_LABELS: tuple[tuple[str, str], ...] = (
        ("interleave",   "interleave  — common + unique messages by timestamp + origin prefix [sid HH:MM] (default)"),
        ("linear",       "linear  — common + each unique tail as a sequential chain"),
        ("parallel",     "parallel  — common + keep original branches intact"),
        ("common-only",  "common-only  — common only; drop all unique tails"),
        ("as-sections",  "as-sections  — common + section dividers + each unique tail"),
    )

    def __init__(
        self,
        targets: list[Session],
        common_count: int,
        unique_counts: dict[str, int],
        suggested_name: str,
        default_method: str = "interleave",
        gccfork_version: str = "",
        default_auto_slim: bool = True,
    ) -> None:
        super().__init__()
        self.targets = targets
        self.common_count = common_count
        self.unique_counts = unique_counts
        self.suggested_name = suggested_name
        self.default_method = default_method if default_method in STITCHING_METHODS else "interleave"
        self.gccfork_version = gccfork_version
        self._selected_method = self.default_method
        self._default_auto_slim = bool(default_auto_slim)

    def compose(self) -> ComposeResult:
        with Vertical(id="merge-box"):
            with Horizontal(id="merge-header"):
                yield Static("[b]GccForK[/]", id="merge-brand", markup=True)
                yield Static("[b]🗂 Merge — true merge into a new sid[/]",
                             id="merge-title", markup=True)
                yield Static(f"[dim]v{self.gccfork_version}[/]",
                             id="merge-meta", markup=True)

            with Vertical(id="merge-scroll"):
                # 1. Name at the top.
                with Vertical(classes="merge-section"):
                    yield Static("📛 New session name", classes="merge-section-title")
                    yield Input(value=self.suggested_name, id="merge-name-input")

                # 2. Post-merge options.
                with Vertical(classes="merge-section"):
                    yield Static("⚙ Post-merge options", classes="merge-section-title")
                    yield Checkbox(
                        "🔻 Auto-slim after merge (strong, in-place) — recommended to keep the merged session light",
                        value=self._default_auto_slim,
                        id="merge-auto-slim-cb",
                    )

                # 3. Selected session list.
                with Vertical(classes="merge-section"):
                    yield Static("📋 Selected sessions", classes="merge-section-title")
                    for t in self.targets[:8]:
                        title = (t.title or "(untitled)")[:50].replace("\n", " ")
                        yield Static(f"  • {t.short_id}  {title}")
                    if len(self.targets) > 8:
                        yield Static(f"  ...and {len(self.targets) - 8} more")

                # 4. Analysis result.
                with Vertical(classes="merge-section"):
                    yield Static("🔍 Message analysis", classes="merge-section-title")
                    yield Static(f"  Common prefix: [b]{self.common_count}[/b] messages", markup=True)
                    for t in self.targets[:6]:
                        n = self.unique_counts.get(t.id, 0)
                        yield Static(f"  {t.short_id} unique: [b]{n}[/b]", markup=True)

                # 5. Method choice.
                with Vertical(classes="merge-section"):
                    yield Static("🔧 Stitching method", classes="merge-section-title")
                    with RadioSet(id="merge-method-set"):
                        for method, label in self._METHOD_LABELS:
                            rb = RadioButton(label, value=(method == self.default_method),
                                             id=f"method-{method}")
                            yield rb

                # 6. Operation summary.
                with Vertical(classes="merge-section"):
                    yield Static("ℹ Operation", classes="merge-section-title")
                    yield Static("  • Assign a new sid (UUID4) and prebuild all five variants in .merged/<N>/")
                    yield Static("  • The selected method becomes the active jsonl; it can be switched later in settings")
                    yield Static("  • Originals are kept in archive with permanent sids and can be fully restored by unmerge")
                    yield Static("  • Auto-slim avoids immediate auto-compact on the first resume")

            with Horizontal(id="merge-btn-row"):
                yield Button("Esc Cancel", id="btn-merge-cancel")
                yield Static("", classes="merge-spacer")
                yield Button(f"🗂 Run merge ({len(self.targets)})",
                             id="btn-merge-confirm", variant="primary")

    def on_mount(self) -> None:
        # Initial focus stays on cancel to protect destructive workflows.
        try:
            self.query_one("#btn-merge-cancel", Button).focus()
        except Exception:
            pass

    def action_cancel_screen(self) -> None:
        self.dismiss(None)

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        # Extract method from RadioButton id = "method-<name>".
        rb_id = event.pressed.id or ""
        if rb_id.startswith("method-"):
            self._selected_method = rb_id[len("method-"):]

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn-merge-confirm":
            try:
                name_input = self.query_one("#merge-name-input", Input)
                name = name_input.value.strip() or self.suggested_name
            except Exception:
                name = self.suggested_name
            try:
                auto_slim_cb = self.query_one("#merge-auto-slim-cb", Checkbox)
                auto_slim = bool(auto_slim_cb.value)
            except Exception:
                auto_slim = self._default_auto_slim
            self.dismiss({
                "method": self._selected_method,
                "name": name,
                "auto_slim": auto_slim,
            })
        elif bid == "btn-merge-cancel":
            self.dismiss(None)


# ── UnmergePreserveConfirmScreen (preserve confirmation for dirty N) ──────────
class UnmergePreserveConfirmScreen(ModalScreen[Optional[str]]):
    """Unmerge mode chooser shown when N contains new dirty work.

    dismiss(None)         # cancel
    dismiss("preserve")   # preserve N while unmerging; default and recommended
    dismiss("delete")     # force full deletion; new work is lost
    """

    BINDINGS = [Binding("escape", "cancel_screen", "Cancel", show=False)]

    DEFAULT_CSS = """
    #unmp-box {
        background: $accent 5%;
        border: round $accent 35%;
        padding: 0;
        width: 80;
        max-width: 96%;
        height: auto;
        max-height: 70%;
        align: center middle;
        layout: vertical;
    }
    #unmp-header {
        height: 1;
        background: $accent 16%;
        padding: 0 1;
        layout: horizontal;
    }
    #unmp-brand {
        width: auto;
        min-width: 10;
        height: 1;
        color: $accent;
        background: transparent;
        text-align: left;
    }
    #unmp-title {
        width: 1fr;
        height: 1;
        color: $accent;
        background: transparent;
        text-align: left;
        text-style: bold;
    }
    #unmp-meta {
        width: auto;
        min-width: 8;
        height: 1;
        color: $text-muted;
        background: transparent;
        text-align: right;
    }
    #unmp-body {
        height: auto;
        padding: 1 2;
    }
    .unmp-section {
        height: auto;
        margin: 0 0 1 0;
        background: $accent 5%;
        border: round $accent 20%;
        padding: 0 1;
    }
    .unmp-section-title {
        height: 1;
        color: $accent;
        background: transparent;
        text-style: bold;
    }
    #unmp-btn-row {
        height: 4;
        padding: 0 1;
        background: $accent 8%;
        border-top: hkey $accent 30%;
        layout: horizontal;
        dock: bottom;
    }
    .unmp-spacer {
        width: 1fr;
        background: transparent;
    }
    #unmp-btn-row Button {
        width: auto;
        min-width: 18;
        height: 3;
        margin: 1 1 0 0;
        background: $accent 5%;
        color: $text;
        border: round $accent 20%;
        padding: 0 2;
        content-align: center middle;
        text-align: center;
        text-style: bold;
    }
    #unmp-btn-row Button:hover {
        background: $accent 10%;
        border: round $accent 35%;
    }
    #unmp-btn-row Button:focus {
        background: $accent 16%;
        border: round $accent;
    }
    """

    def __init__(
        self,
        new_sid: str,
        new_turn_count: int,
        gccfork_version: str = "",
    ) -> None:
        super().__init__()
        self.new_sid = new_sid
        self.new_turn_count = new_turn_count
        self.gccfork_version = gccfork_version

    def compose(self) -> ComposeResult:
        with Vertical(id="unmp-box"):
            with Horizontal(id="unmp-header"):
                yield Static("[b]GccForK[/]", id="unmp-brand", markup=True)
                yield Static("[b]🔧 Unmerge — new work detected[/]",
                             id="unmp-title", markup=True)
                yield Static(f"[dim]v{self.gccfork_version}[/]",
                             id="unmp-meta", markup=True)

            with Vertical(id="unmp-body"):
                with Vertical(classes="unmp-section"):
                    yield Static("⚠ Situation", classes="unmp-section-title")
                    yield Static(
                        f"  N ({self.new_sid[:8]}) has about [b]{self.new_turn_count} new messages[/b] after merge",
                        markup=True,
                    )
                    yield Static("  -> deleting it now permanently loses that work.")

                with Vertical(classes="unmp-section"):
                    yield Static("✅ Recommended: preserve unmerge", classes="unmp-section-title")
                    yield Static("  • Restore children (B/C/...) to their original positions")
                    yield Static("  • Keep N's active jsonl as-is to preserve new work")
                    yield Static("  • N remains visible as an [b]independent session[/b] with a permanent sid", markup=True)
                    yield Static("  • Remove only the .merged/<N>/ variant folder")

                with Vertical(classes="unmp-section"):
                    yield Static("⚠ Force delete", classes="unmp-section-title")
                    yield Static("  • Delete N's active jsonl — [b]new work is permanently lost[/b]", markup=True)
                    yield Static("  • Remove the entire registry entry; external N.sid references become dead links")

            with Horizontal(id="unmp-btn-row"):
                yield Button("Esc Cancel", id="btn-unmp-cancel")
                yield Static("", classes="unmp-spacer")
                yield Button("⚠ Force delete", id="btn-unmp-delete")
                yield Button("✅ Preserve unmerge (recommended)", id="btn-unmp-preserve",
                             variant="primary")

    def on_mount(self) -> None:
        # Default focus is preserve, so Enter chooses the safer action.
        try:
            self.query_one("#btn-unmp-preserve", Button).focus()
        except Exception:
            pass

    def action_cancel_screen(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn-unmp-preserve":
            self.dismiss("preserve")
        elif bid == "btn-unmp-delete":
            self.dismiss("delete")
        elif bid == "btn-unmp-cancel":
            self.dismiss(None)


# ── MergeMixin (for UI integration; mixed into the main app) ──────────────────
class MergeMixin:
    """App-side action methods mixed into the main gccfork app.

    Required app-side members:
      - self.sessions
      - self._multi_selected_ids
      - self.notify
      - self.push_screen / reload_sessions / refresh_list
    """

    def action_merge_selected(self) -> None:
        """Model B merge: show MergeConfirmScreen and execute after confirmation."""
        sel_ids: set[str] = set(getattr(self, "_multi_selected_ids", set()))
        if len(sel_ids) < 2:
            try:
                self.notify("Select at least two sessions to merge.", severity="warning")
            except Exception:
                pass
            return
        all_sessions = list(getattr(self, "sessions", []))
        targets = [s for s in all_sessions if s.id in sel_ids]
        if len(targets) < 2:
            return

        # Analysis preview only; no actual changes here.
        try:
            common, unique = extract_common_and_unique(targets)
        except Exception:
            common, unique = [], {}
        common_count = len(common)
        unique_counts = {sid: len(msgs) for sid, msgs in unique.items()}

        # Preflight common-ancestor check before showing the modal.
        try:
            lca = find_lca(targets)
            if lca is None:
                self.notify(
                    "Merge refused: selected sessions do not share a common ancestor.",
                    severity="error",
                )
                return
        except Exception:
            pass

        # Automatic name suggestion.
        first_name = (targets[0].title or targets[0].short_id)[:30]
        suggested = f"🗂 merged: {first_name} +{len(targets) - 1}"

        default_method = str(
            pref_get("merge_stitching_method") or MERGE_DEFAULTS["merge_stitching_method"]
        )
        if default_method not in STITCHING_METHODS:
            default_method = MERGE_DEFAULTS["merge_stitching_method"]

        version = ""
        try:
            import sys as _sys
            mod = _sys.modules.get("__main__")
            version = getattr(mod, "GCCFORK_VERSION", "") if mod else ""
        except Exception:
            pass

        def _on_confirm(result: Optional[dict]) -> None:
            if not result:
                return
            method = result.get("method", MERGE_DEFAULTS["merge_stitching_method"])
            name = result.get("name") or suggested
            auto_slim = bool(result.get("auto_slim", MERGE_DEFAULTS["merge_auto_slim_after"]))
            # Persist method changes made in the modal as the next merge default.
            try:
                pref_set("merge_stitching_method", method)
            except Exception:
                pass
            # Persist auto_slim as the next merge default.
            try:
                pref_set("merge_auto_slim_after", bool(auto_slim))
            except Exception:
                pass
            try:
                new_sid = merge_into_new_session(targets, name=name)
            except ActiveSessionInMergeError as e:
                # Safety guard §1: block active sids to prevent the 2026-05-04 incident.
                try:
                    self.notify(f"⛔ Merge blocked: {e}", severity="error", timeout=8.0)
                except Exception:
                    pass
                return
            except NoCommonAncestorError as e:
                try:
                    self.notify(f"Merge refused: {e}", severity="error")
                except Exception:
                    pass
                return
            except Exception as e:
                try:
                    self.notify(f"Merge failed: {e}", severity="error")
                except Exception:
                    pass
                return
            try:
                self.notify(
                    f"🗂 Merge complete: {new_sid[:8]} (method={method}, children={len(targets)})"
                )
            except Exception:
                pass

            # Auto-slim after merge when enabled: run in-place strong slim on N's active jsonl.
            if auto_slim:
                try:
                    # Lazy import from gccfork main to avoid a circular import.
                    from gccfork import slim_fork_session_with
                    from gccfork_sessions import parse_session
                    project_dir = _project_dir_for(targets)
                    n_path = _active_path(project_dir, new_sid)
                    n_session = parse_session(n_path) if n_path.exists() else None
                    if n_session is None:
                        raise RuntimeError("failed to parse the newly merged jsonl")
                    mode = str(MERGE_DEFAULTS["merge_auto_slim_mode"])
                    size_before = n_path.stat().st_size if n_path.exists() else 0
                    stats = slim_fork_session_with(
                        n_session, n_session.id, name,
                        mode=mode, in_place=True, backup=True,
                    )
                    size_after = n_path.stat().st_size if n_path.exists() else 0
                    pct = (1 - size_after / size_before) * 100 if size_before else 0
                    try:
                        self.notify(
                            f"🔻 Auto-slim complete: {size_before // 1024}K -> "
                            f"{size_after // 1024}K (-{pct:.1f}%)",
                            timeout=6.0,
                        )
                    except Exception:
                        pass
                except Exception as exc:
                    try:
                        self.notify(
                            f"⚠ Auto-slim failed, but the merge succeeded: {exc}",
                            severity="warning", timeout=8.0,
                        )
                    except Exception:
                        pass

            try:
                self._multi_selected_ids.clear()
            except Exception:
                pass
            try:
                self._update_multi_action_visibility()
            except Exception:
                pass
            try:
                self.reload_sessions()
            except Exception:
                pass

        # default_auto_slim: prefer prefs, then fall back to MERGE_DEFAULTS.
        default_auto_slim = bool(
            pref_get("merge_auto_slim_after", MERGE_DEFAULTS["merge_auto_slim_after"])
        )

        try:
            self.push_screen(
                MergeConfirmScreen(
                    targets=targets,
                    common_count=common_count,
                    unique_counts=unique_counts,
                    suggested_name=suggested,
                    default_method=default_method,
                    gccfork_version=version,
                    default_auto_slim=default_auto_slim,
                ),
                _on_confirm,
            )
        except Exception as exc:
            try:
                self.notify(f"Failed to open merge modal: {exc}", severity="error")
            except Exception:
                pass

    def action_unmerge_selected_v2(self) -> None:
        """Model B unmerge via unmerge_new_session.

        Activation conditions, guarded by caller:
          - exactly one selected session
          - that session entry has is_merged == True

        branch:
          - pristine (right after merge, or only method-switched) -> immediately delete all
          - dirty (new messages added through resume, etc.) -> show preserve/delete/cancel modal
        """
        sel_ids: set[str] = set(getattr(self, "_multi_selected_ids", set()))
        if len(sel_ids) != 1:
            try:
                self.notify("Unmerge requires exactly one selected session.", severity="warning")
            except Exception:
                pass
            return
        target_sid = next(iter(sel_ids))
        entry = registry_get(target_sid)
        if not entry.get("is_merged"):
            # Safety guard section 7: identify hard-fork copies and show a clear message.
            forked_from = entry.get("forked_from_merged")
            if forked_from:
                msg = (
                    f"This session is a hard-branch copy of a merge result. "
                    f"Unmerge is only available from the original [{forked_from[:4]}]."
                )
            else:
                msg = "The selected session is not a merge result (is_merged=false)."
            try:
                self.notify(msg, severity="warning")
            except Exception:
                pass
            return

        # Detect pristine state: whether new work exists.
        try:
            pristine = is_merge_pristine(target_sid)
        except Exception:
            pristine = True   # Safe default on detection failure.

        if pristine:
            # Immediate full delete.
            self._do_unmerge_v2(target_sid, mode="delete")
            return

        # Dirty state: show the modal.
        new_count = 0
        try:
            new_count = count_new_lines_since_merge(target_sid)
        except Exception:
            pass

        version = ""
        try:
            import sys as _sys
            mod = _sys.modules.get("__main__")
            version = getattr(mod, "GCCFORK_VERSION", "") if mod else ""
        except Exception:
            pass

        def _on_choice(choice: Optional[str]) -> None:
            if choice not in ("preserve", "delete"):
                return   # cancel
            self._do_unmerge_v2(target_sid, mode=choice)

        try:
            self.push_screen(
                UnmergePreserveConfirmScreen(
                    new_sid=target_sid,
                    new_turn_count=new_count,
                    gccfork_version=version,
                ),
                _on_choice,
            )
        except Exception as exc:
            try:
                self.notify(f"Failed to open unmerge modal: {exc}", severity="error")
            except Exception:
                pass

    def _do_unmerge_v2(self, target_sid: str, mode: str) -> None:
        """Call unmerge_new_session, notify, and refresh the UI."""
        try:
            ok = unmerge_new_session(target_sid, mode=mode)
        except Exception as exc:
            try:
                self.notify(f"🔧 Unmerge failed: {exc}", severity="error")
            except Exception:
                pass
            return
        try:
            if ok:
                if mode == "preserve":
                    self.notify(
                        f"🔧 Preserve unmerge complete: children restored; N({target_sid[:8]}) remains independent"
                    )
                else:
                    self.notify(f"🔧 Unmerge complete: {target_sid[:8]} -> original sessions restored")
            else:
                self.notify(f"🔧 Unmerge failed: {target_sid[:8]}", severity="error")
        except Exception:
            pass
        try:
            self._multi_selected_ids.clear()
        except Exception:
            pass
        try:
            self._update_multi_action_visibility()
        except Exception:
            pass
        try:
            self.reload_sessions()
        except Exception:
            pass


__all__ = [
    "MERGE_DEFAULTS",
    "MergeConfirmScreen",
    "MergeMixin",
    "NoCommonAncestorError",
    "STITCHING_METHODS",
    "UnmergePreserveConfirmScreen",
    "count_new_lines_since_merge",
    "extract_common_and_unique",
    "find_lca",
    "is_merge_pristine",
    "merge_into_new_session",
    "sync_active_jsonl",
    "unmerge_new_session",
]
