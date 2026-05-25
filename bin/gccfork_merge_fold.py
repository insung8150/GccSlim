"""Pairwise fold-merge — combine a branch chain into one integrated JSONL.

This module replaces the internal stitching used by `gccfork_merge.py`
(True Merge / N new sessions) with a fold-merge algorithm. All five modes are
supported. Pure library code; no Textual dependency.

Design:
    1. Pairwise merge original JSONL files without slimming — fold root → leaf.
    2. Disk usage is secondary; keep intermediate files until verification.
    3. Optionally slim only once at the end.

Algorithm (merge_two_jsonls):
    - Deduplicate all messages from both JSONLs by uuid.
    - On same-uuid collisions, keep the longer content (original beats slim stubs).
    - Sort by timestamp (interleave).
    - Rewrite only sessionId to the new sid, preserving uuid/parentUuid chains.

Verification:
    - Union of all source UUIDs equals result UUIDs.
    - Guarantees zero missing / duplicate UUIDs.
"""
from __future__ import annotations

import json
import shutil
import time
import uuid as _uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ─── Data ─────────────────────────────────────────────
@dataclass
class MergeStepReport:
    step: int
    base_label: str
    next_label: str
    base_uuids: int
    next_uuids: int
    common: int
    union: int
    output_path: Path


@dataclass
class MergeChainReport:
    chain_sids: list[str]
    new_sid: str
    final_path: Path
    intermediate_paths: list[Path] = field(default_factory=list)
    steps: list[MergeStepReport] = field(default_factory=list)
    total_uuids: int = 0
    sources_total_uuids: int = 0
    sources_union_uuids: int = 0
    duration_sec: float = 0.0


# ─── Utilities ────────────────────────────────────────
def _projects_root() -> Path:
    return Path.home() / ".claude" / "projects"


_SKIP_TOKENS = (".bak.", ".archived", ".emergency", ".restore", ".rollback", ".clean-tail")


def find_jsonl_for_sid(sid_prefix: str) -> Optional[Path]:
    """Resolve a sid or prefix to an active JSONL path.

    Excludes .bak / .archived / .emergency / .restore / .rollback /
    .clean-tail / .tmp. If the sid appears in multiple cwd folders, prefer the
    newest mtime as the likely current workspace.
    """
    root = _projects_root()
    if not root.is_dir():
        return None
    matches: list[Path] = []
    for proj in root.iterdir():
        if not proj.is_dir():
            continue
        for f in proj.glob(f"{sid_prefix}*.jsonl"):
            n = f.name
            if any(tok in n for tok in _SKIP_TOKENS) or n.endswith(".tmp"):
                continue
            matches.append(f)
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    return max(matches, key=lambda p: p.stat().st_mtime)


def _content_text_len(msg: dict) -> int:
    """Return message text length for longer-content collision resolution."""
    m = msg.get("message", {}) or {}
    c = m.get("content", "")
    if isinstance(c, str):
        return len(c)
    if isinstance(c, list):
        total = 0
        for blk in c:
            if not isinstance(blk, dict):
                continue
            t = blk.get("type")
            if t == "text":
                total += len(blk.get("text", ""))
            elif t == "tool_result":
                cc = blk.get("content", "")
                if isinstance(cc, str):
                    total += len(cc)
                elif isinstance(cc, list):
                    for x in cc:
                        if isinstance(x, dict) and x.get("type") == "text":
                            total += len(x.get("text", ""))
            elif t == "tool_use":
                inp = blk.get("input", {})
                total += len(json.dumps(inp, ensure_ascii=False)) if inp else 0
        return total
    return 0


def collect_messages(path: Path) -> dict[str, dict]:
    """Return uuid → full message dict, skipping rows without uuid."""
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            d = json.loads(line)
        except Exception:
            continue
        u = d.get("uuid")
        if not u:
            continue
        out[u] = d
    return out


# ─── Core — merge two JSONLs ──────────────────────────
def merge_two_jsonls(
    base_path: Path,
    next_path: Path,
    output_path: Path,
    new_sid: str,
) -> MergeStepReport:
    """A + B → C with uuid dedup, timestamp sorting, and sessionId rewrite.

    On collisions, keep longer content (original beats slim stubs).
    """
    base = collect_messages(base_path)
    nxt = collect_messages(next_path)

    merged: dict[str, dict] = {}
    common = 0
    for u, m in base.items():
        merged[u] = m
    for u, m in nxt.items():
        if u in merged:
            common += 1
            if _content_text_len(m) > _content_text_len(merged[u]):
                merged[u] = m
        else:
            merged[u] = m

    sorted_msgs = sorted(merged.values(), key=lambda d: d.get("timestamp", ""))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for m in sorted_msgs:
            m["sessionId"] = new_sid
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
    tmp.replace(output_path)

    return MergeStepReport(
        step=0,
        base_label=base_path.stem[:8],
        next_label=next_path.stem[:8],
        base_uuids=len(base),
        next_uuids=len(nxt),
        common=common,
        union=len(merged),
        output_path=output_path,
    )


# ─── Fold — root → leaf order ─────────────────────────
def fold_merge_chain(
    chain_sids_root_to_leaf: list[str],
    output_dir: Path,
    new_sid: Optional[str] = None,
    keep_intermediate: bool = True,
) -> MergeChainReport:
    """Pairwise-fold the chain in root → leaf order."""
    if len(chain_sids_root_to_leaf) < 2:
        raise ValueError("chain requires at least two sids")

    new_sid = new_sid or str(_uuid.uuid4())
    output_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    steps: list[MergeStepReport] = []
    intermediate: list[Path] = []

    base_path = find_jsonl_for_sid(chain_sids_root_to_leaf[0][:8])
    if base_path is None:
        raise FileNotFoundError(f"root sid not found: {chain_sids_root_to_leaf[0]}")

    accumulator = base_path
    for i, sid in enumerate(chain_sids_root_to_leaf[1:], start=1):
        next_path = find_jsonl_for_sid(sid[:8])
        if next_path is None:
            raise FileNotFoundError(f"chain sid not found: {sid}")
        out = output_dir / f"merge-step-{i:02d}-{sid[:8]}.jsonl"
        rep = merge_two_jsonls(accumulator, next_path, out, new_sid)
        rep.step = i
        steps.append(rep)
        intermediate.append(out)
        accumulator = out

    final_path = output_dir / f"{new_sid}.jsonl"
    shutil.copy2(accumulator, final_path)

    src_union: set[str] = set()
    for sid in chain_sids_root_to_leaf:
        p = find_jsonl_for_sid(sid[:8])
        if p:
            src_union |= set(collect_messages(p).keys())
    result_uuids = set(collect_messages(final_path).keys())

    if not keep_intermediate:
        for p in intermediate[:-1]:
            p.unlink(missing_ok=True)

    return MergeChainReport(
        chain_sids=chain_sids_root_to_leaf,
        new_sid=new_sid,
        final_path=final_path,
        intermediate_paths=intermediate,
        steps=steps,
        total_uuids=len(result_uuids),
        sources_total_uuids=len(src_union),
        sources_union_uuids=len(result_uuids & src_union),
        duration_sec=time.time() - t0,
    )


# ─── Diagnostics / verification ───────────────────────
def verify_merge(report: MergeChainReport) -> dict:
    expected = report.sources_total_uuids
    actual = report.total_uuids
    coverage = report.sources_union_uuids
    return {
        "expected_uuids": expected,
        "actual_uuids": actual,
        "covered": coverage,
        "missing": expected - coverage,
        "extra": actual - coverage,
        "ok": coverage == expected and actual == expected,
    }


# ─── Five stitching modes (fold-based implementation) ─
def _last_anchor_uuid(msgs: list[dict]) -> Optional[str]:
    """Skip metadata messages (uuid=None) and return the real chain anchor."""
    for m in reversed(msgs):
        u = m.get("uuid")
        if u:
            return u
    return None


def _replace_sid_inline(msg: dict, new_sid: str) -> dict:
    """Replace only sessionId, mutating the original dict."""
    msg["sessionId"] = new_sid
    return msg


def _origin_prefix(orig_sid: str, ts: str) -> str:
    hh = ts[11:16] if ts and len(ts) >= 16 else "??:??"
    return f"[{orig_sid[:8]} {hh}] "


def _inject_origin_prefix(msg: dict, orig_sid: str) -> dict:
    """Inject an origin prefix into the first user/assistant text block."""
    m = msg.get("message", {})
    if not isinstance(m, dict):
        return msg
    role = m.get("role")
    if role not in ("user", "assistant"):
        return msg
    ts = msg.get("timestamp", "")
    prefix = _origin_prefix(orig_sid, ts)
    c = m.get("content")
    if isinstance(c, str):
        m["content"] = prefix + c
    elif isinstance(c, list):
        for blk in c:
            if isinstance(blk, dict) and blk.get("type") == "text":
                blk["text"] = prefix + blk.get("text", "")
                break
    return msg


def split_common_and_unique(
    sources_in_order: list[Path],
) -> tuple[list[dict], dict[Path, list[dict]]]:
    """Fold-merge LCA replacement based on UUID intersection/difference.

    common = messages whose UUID exists in every source.
    unique_by_path = assign each UUID exactly once, preferring first source
                     appearance. common + sum(unique) exactly equals the source
                     UUID union.

    On collisions, keep longer content (original beats slim stubs).
    """
    sets: list[set[str]] = []
    msg_maps: list[dict[str, dict]] = []
    for p in sources_in_order:
        m = collect_messages(p)
        msg_maps.append(m)
        sets.append(set(m.keys()))

    common_uuids = set.intersection(*sets) if sets else set()

    # common: prefer longer body on collisions.
    common_msgs: list[dict] = []
    for u in sorted(common_uuids, key=lambda u: msg_maps[0][u].get("timestamp", "")):
        candidates = [mm[u] for mm in msg_maps if u in mm]
        best = max(candidates, key=_content_text_len)
        common_msgs.append(json.loads(json.dumps(best)))

    # unique: assign each UUID only to the first source where it appears.
    seen_unique: set[str] = set()
    unique_by_path: dict[Path, list[dict]] = {}
    for p, mm in zip(sources_in_order, msg_maps):
        own_uuids = set(mm.keys()) - common_uuids - seen_unique
        # If the same UUID appears in later sources, prefer the longer body.
        chosen: list[dict] = []
        for u in sorted(own_uuids, key=lambda u: mm[u].get("timestamp", "")):
            candidates = [later_mm[u] for later_mm in msg_maps if u in later_mm]
            best = max(candidates, key=_content_text_len)
            chosen.append(json.loads(json.dumps(best)))
        unique_by_path[p] = chosen
        seen_unique |= own_uuids

    return common_msgs, unique_by_path


def stitch_linear(
    common: list[dict],
    unique_by_path: dict[Path, list[dict]],
    new_sid: str,
) -> list[dict]:
    """Chain common + each source's unique rows in order, reconnecting parentUuid."""
    out = [_replace_sid_inline(m, new_sid) for m in common]
    last_uuid = _last_anchor_uuid(common)
    for path, unique in unique_by_path.items():
        for i, msg in enumerate(unique):
            new_msg = _replace_sid_inline(msg, new_sid)
            if i == 0:
                new_msg["parentUuid"] = last_uuid
            out.append(new_msg)
            u = msg.get("uuid")
            if u:
                last_uuid = u
    return out


def stitch_interleave(
    common: list[dict],
    unique_by_path: dict[Path, list[dict]],
    new_sid: str,
) -> list[dict]:
    """Sort common + all unique messages by timestamp and inject origin prefixes."""
    out = [_replace_sid_inline(m, new_sid) for m in common]
    flat: list[tuple[str, dict]] = []
    for path, unique in unique_by_path.items():
        sid_label = path.stem[:8]
        for msg in unique:
            flat.append((sid_label, msg))
    flat.sort(key=lambda pair: pair[1].get("timestamp", ""))
    last_uuid = _last_anchor_uuid(common)
    for orig_sid, msg in flat:
        new_msg = _replace_sid_inline(msg, new_sid)
        new_msg = _inject_origin_prefix(new_msg, orig_sid)
        new_msg["parentUuid"] = last_uuid
        out.append(new_msg)
        u = msg.get("uuid")
        if u:
            last_uuid = u
    return out


def stitch_parallel(
    common: list[dict],
    unique_by_path: dict[Path, list[dict]],
    new_sid: str,
) -> list[dict]:
    """common + each source's unique rows while preserving original parentUuid."""
    out = [_replace_sid_inline(m, new_sid) for m in common]
    for path, unique in unique_by_path.items():
        for msg in unique:
            out.append(_replace_sid_inline(msg, new_sid))
    return out


def stitch_common_only(
    common: list[dict],
    unique_by_path: dict[Path, list[dict]],
    new_sid: str,
) -> list[dict]:
    """Keep only the common prefix and drop all unique parts."""
    return [_replace_sid_inline(m, new_sid) for m in common]


def stitch_as_sections(
    common: list[dict],
    unique_by_path: dict[Path, list[dict]],
    new_sid: str,
) -> list[dict]:
    """common + section dividers (synthetic system messages) + source uniques."""
    out = [_replace_sid_inline(m, new_sid) for m in common]
    last_uuid = _last_anchor_uuid(common)
    for path, unique in unique_by_path.items():
        if not unique:
            continue
        sid_label = path.stem[:8]
        divider_uuid = f"div-{sid_label}-{_uuid.uuid4().hex[:8]}"
        divider = {
            "uuid": divider_uuid,
            "parentUuid": last_uuid,
            "sessionId": new_sid,
            "type": "system",
            "message": {"role": "system", "content": f"──── branch {sid_label} ────"},
            "timestamp": unique[0].get("timestamp", ""),
            "isMergeDivider": True,
        }
        out.append(divider)
        last_uuid = divider_uuid
        for msg in unique:
            new_msg = _replace_sid_inline(msg, new_sid)
            new_msg["parentUuid"] = last_uuid
            out.append(new_msg)
            u = msg.get("uuid")
            if u:
                last_uuid = u
    return out


STITCHERS: dict[str, callable] = {
    "linear": stitch_linear,
    "interleave": stitch_interleave,
    "parallel": stitch_parallel,
    "common-only": stitch_common_only,
    "as-sections": stitch_as_sections,
}


def merge_with_mode(
    sources_in_order: list[Path],
    output_path: Path,
    new_sid: str,
    mode: str = "interleave",
) -> dict:
    """Stitch with one of the five modes and write the output JSONL.

    Returns: {"mode", "kept", "common", "unique_total", "out_path"}
    """
    if mode not in STITCHERS:
        raise ValueError(f"unknown mode: {mode} (valid: {list(STITCHERS.keys())})")
    common, unique = split_common_and_unique(sources_in_order)
    msgs = STITCHERS[mode](common, unique, new_sid)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for m in msgs:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
    tmp.replace(output_path)
    return {
        "mode": mode,
        "kept": len(msgs),
        "common": len(common),
        "unique_total": sum(len(v) for v in unique.values()),
        "out_path": output_path,
    }


def format_report(report: MergeChainReport) -> str:
    lines = [
        f"🔱 Merge fold complete ({report.duration_sec * 1000:.0f}ms)",
        f"  chain: {len(report.chain_sids)} sids (root → leaf)",
        f"  new sid: {report.new_sid}",
        f"  result: {report.final_path}",
        f"  intermediate files: {len(report.intermediate_paths)}",
        "",
        "  steps:",
    ]
    for s in report.steps:
        lines.append(
            f"    [{s.step:02d}] {s.base_label} ({s.base_uuids}) + "
            f"{s.next_label} ({s.next_uuids}) → {s.union} (common {s.common})"
        )
    v = verify_merge(report)
    lines.append("")
    status = "✅ full coverage" if v["ok"] else f"❌ {v['missing']} missing / {v['extra']} extra"
    lines.append(f"  verification: source {v['expected_uuids']} uuid → result {v['actual_uuids']} uuid  {status}")
    return "\n".join(lines)
