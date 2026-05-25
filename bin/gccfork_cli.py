"""gccfork headless CLI sidecar.

Allows external callers to run core features even when the TUI is not open.
Each subcommand imports stateless functions from the main gccfork module, so
the CLI can stay thin.

Entrypoints:
    gccfork search <query> [--cwd PATH] [--all] [--no-filter] [--json]
    gccfork list                [--cwd PATH] [--all] [--json]
    gccfork detail <sid>        [--json]
    gccfork ancestry <sid>      [--json]
    gccfork parent-of <sid>     [--json]
    gccfork rename <sid> <name>
    gccfork hard-fork <sid> [--name NAME]
    gccfork delete <sid>        [--force]
    gccfork prefs get [<key>]
    gccfork prefs set <key> <value>
    gccfork stats               [--cwd PATH] [--all] [--json]

cwd resolution priority:
    1) --cwd flag
    2) GCCFORK_CWD environment variable
    3) $PWD / os.getcwd()

JSON output (--json) is automation-friendly. Without it, output is intended
for humans.

Registry locking:
    rename / hard-fork / delete use fcntl.flock for read-modify-write
    protection. This prevents last-write-wins corruption if the TUI updates
    the registry at the same time.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

_SRC_CANDIDATES: list[Path] = []
try:
    _SCRIPT_PATH = Path(__file__).resolve()
    if len(_SCRIPT_PATH.parents) > 1:
        _SRC_CANDIDATES.append(_SCRIPT_PATH.parents[1] / "src")
except OSError:
    pass
_SRC_CANDIDATES.append(Path.cwd() / "src")
if os.environ.get("GCCFORK_REPO"):
    _SRC_CANDIDATES.append(Path(os.environ["GCCFORK_REPO"]).expanduser() / "src")
for _SRC_PATH in _SRC_CANDIDATES:
    if _SRC_PATH.exists() and str(_SRC_PATH) not in sys.path:
        sys.path.insert(0, str(_SRC_PATH))
        break

_TRACE_T0 = time.perf_counter()

CLEAR_SLASH = "/clear"
RESUME_SLASH = "/resume"
KEYBIND_CLEAR_CHORD = "ctrl+x ctrl+g"
KEYBIND_RESUME_CHORD = "ctrl+x ctrl+r"
KEYBIND_CLEAR_SEQUENCE = "\x18\x07"   # Ctrl+X, Ctrl+G
KEYBIND_RESUME_SEQUENCE = "\x18\x12"  # Ctrl+X, Ctrl+R
KEYBINDINGS_PATH = Path.home() / ".claude" / "keybindings.json"


def trace_sar(label: str) -> None:
    elapsed_ms = (time.perf_counter() - _TRACE_T0) * 1000
    print(f"  [sar  +{elapsed_ms:7.1f}ms] {label}", file=sys.stderr, flush=True)


# ─── cwd resolution ─────────────────────────────────────────────────────
def resolve_cwd(arg_cwd: Optional[str]) -> str:
    """--cwd > GCCFORK_CWD > $PWD / os.getcwd()."""
    if arg_cwd:
        return os.path.abspath(arg_cwd)
    env = os.environ.get("GCCFORK_CWD")
    if env:
        return os.path.abspath(env)
    return os.environ.get("PWD") or os.getcwd()


# ─── Registry lock for multi-process safety ─────────────────────────────
_LOCK_PATH = Path.home() / ".claude" / ".gccfork-cli.lock"


@contextmanager
def registry_lock():
    """Acquire ~/.claude/.gccfork-cli.lock with fcntl.flock.

    This protects against concurrent registry writes from the TUI. Waits up to
    five seconds before failing.
    """
    _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fh = open(_LOCK_PATH, "w")
    try:
        deadline = time.time() + 5.0
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() >= deadline:
                    raise RuntimeError("registry lock timeout (5s)")
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        fh.close()


# ─── Short session id -> full id mapping ────────────────────────────────
def resolve_sid(short: str, sessions) -> Optional[str]:
    """Resolve a prefix such as `3a35` or `f46da6c8` to a full session id.

    1. Prefer exact matches for full UUID input.
    2. Otherwise use prefix matching; any unique length is accepted.
    3. Return None for zero or multiple matches.
    """
    short = short.strip().lower()
    if not short:
        return None
    for s in sessions:
        if s.id.lower() == short:
            return s.id
    matches = [s.id for s in sessions if s.id.lower().startswith(short)]
    if len(matches) == 1:
        return matches[0]
    return None


def _project_jsonl_roots(cwd: Optional[str]) -> list[Path]:
    projects_dir = Path.home() / ".claude" / "projects"
    roots: list[Path] = []
    if cwd:
        from gccfork import cwd_to_slug
        slug = cwd_to_slug(cwd)
        current = projects_dir / slug
        if current.is_dir():
            roots.append(current)
    if projects_dir.is_dir():
        roots.extend(p for p in projects_dir.iterdir() if p.is_dir() and p not in roots)
    return roots


def find_session_jsonl_fast(sid_or_prefix: str, cwd: Optional[str]) -> Optional[Path]:
    """Resolve sid by filename without parsing every JSONL file."""
    wanted = sid_or_prefix.strip().lower()
    if not wanted:
        return None
    matches: dict[str, list[Path]] = {}
    for root in _project_jsonl_roots(cwd):
        try:
            paths = root.glob(f"{wanted}*.jsonl")
            for path in paths:
                if ".bak." in path.name:
                    continue
                stem = path.stem.lower()
                if stem.startswith(wanted):
                    matches.setdefault(stem, []).append(path)
        except OSError:
            continue
    if not matches:
        return None
    if wanted in matches:
        candidates = matches[wanted]
    elif len(matches) == 1:
        candidates = next(iter(matches.values()))
    else:
        return None
    cwd_root = _project_jsonl_roots(cwd)[:1]
    cwd_dir = cwd_root[0] if cwd_root else None

    def rank(path: Path) -> tuple[int, float, int]:
        try:
            st = path.stat()
            return (int(cwd_dir is not None and path.parent == cwd_dir), st.st_mtime, st.st_size)
        except OSError:
            return (0, 0.0, 0)

    return max(candidates, key=rank)


def make_minimal_session_for_jsonl(jsonl_path: Path, sid: str, cwd: Optional[str]):
    """Build a minimal Session for the slim-and-reload fast path."""
    from gccfork import Session
    try:
        st = jsonl_path.stat()
    except OSError:
        return None
    return Session(
        id=sid,
        jsonl_path=jsonl_path,
        mtime=datetime.fromtimestamp(st.st_mtime),
        turn_count=0,
        size_bytes=st.st_size,
        cwd=cwd,
        source="claude-code",
    )


# ─── Session -> JSON ────────────────────────────────────────────────────
def session_to_dict(s) -> dict:
    """Convert a Session object into a JSON-serializable dict."""
    return {
        "id": s.id,
        "jsonl_path": str(s.jsonl_path),
        "mtime": s.mtime.isoformat() if s.mtime else None,
        "turn_count": s.turn_count,
        "live_turn_count": s.live_turn_count,
        "size_bytes": s.size_bytes,
        "title": s.title,
        "cwd": s.cwd,
        "custom_name": s.custom_name,
        "auto_summary": s.auto_summary,
        "ai_summary": s.ai_summary,
        "parent_id": s.parent_id,
        "fork_type": s.fork_type,
        "compact_count": s.compact_count,
        "originator": s.originator,
        "source": s.source,
    }


# ─── Subcommand: search ─────────────────────────────────────────────────
def cmd_search(args) -> int:
    """Scan full session text with noise filters enabled by default."""
    from gccfork import scan_sessions, cwd_to_slug
    from gccfork_settings import (
        DEEP_SEARCH_ITEMS,
        get_deep_prefs_snapshot,
        get_scannable_text,
    )

    cwd = resolve_cwd(args.cwd)
    sessions = scan_sessions(cwd, args.all)
    n = len(sessions)

    if args.no_filter:
        prefs = {item["key"]: True for item in DEEP_SEARCH_ITEMS}
    else:
        prefs = get_deep_prefs_snapshot()

    qlow = args.query.lower()
    qstripped = re.sub(r"\s+", "", qlow)
    qtokens = [t for t in qlow.split() if len(t) >= 2]

    # Fuzzy matcher. Enabled by prefs or --fuzzy; --no-fuzzy has priority.
    fuzz = None
    fuzzy_on = (prefs.get("deep_include_fuzzy", False) or args.fuzzy) and not args.no_fuzzy
    if fuzzy_on:
        try:
            from rapidfuzz import fuzz as _fuzz
            fuzz = _fuzz
        except ImportError:
            pass

    def line_match(line_lower: str) -> bool:
        if not line_lower:
            return False
        if qlow in line_lower:
            return True
        if qstripped and qstripped in re.sub(r"\s+", "", line_lower):
            return True
        if len(qtokens) >= 2 and all(t in line_lower for t in qtokens):
            return True
        if fuzz is not None and len(line_lower) < 1500:
            if fuzz.partial_ratio(qlow, line_lower) >= 80:
                return True
        return False

    matched: list[dict] = []
    unmatched: list[dict] = []
    started = time.time()
    for s in sessions:
        if not s.jsonl_path.exists():
            continue
        hit = False
        try:
            with s.jsonl_path.open("r", encoding="utf-8", errors="ignore") as fh:
                for raw in fh:
                    try:
                        obj = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    scannable = get_scannable_text(obj, prefs)
                    if scannable and line_match(scannable):
                        hit = True
                        break
        except OSError:
            continue
        entry = {
            "id": s.id,
            "title": s.title,
            "mtime": s.mtime.isoformat() if s.mtime else None,
            "size_bytes": s.size_bytes,
            "turn_count": s.turn_count,
        }
        (matched if hit else unmatched).append(entry)
    elapsed = time.time() - started

    out = {
        "scope": {
            "mode": "all" if args.all else "current_cwd",
            "cwd": cwd,
            "slug": cwd_to_slug(cwd),
            "session_count": n,
        },
        "query": args.query,
        "filter": {
            "noise_filter": not args.no_filter,
            "fuzzy": fuzz is not None,
            "prefs": prefs,
        },
        "elapsed_sec": round(elapsed, 3),
        "matched": matched,
        "unmatched": unmatched,
    }

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        _print_search_human(out)
    return 0


def _print_search_human(out: dict) -> None:
    sc = out["scope"]
    f = out["filter"]
    print(f"📁 Scope: {sc['mode']}  ·  {sc['cwd']}")
    print(f"   slug: {sc['slug']}  ·  sessions {sc['session_count']}")
    flt = "ON (default, excludes 5 noise items)" if f["noise_filter"] else "OFF (--no-filter, legacy behavior)"
    fz = "rapidfuzz" if f["fuzzy"] else "off"
    print(f"🔍 query: '{out['query']}'  ·  noise filter {flt}  ·  fuzzy {fz}")
    print(f"⏱  {out['elapsed_sec']}s  ·  matched {len(out['matched'])} / unmatched {len(out['unmatched'])}\n")
    print(f"━━ ✅ Matched ({len(out['matched'])}) ━━")
    for e in out["matched"]:
        title = (e.get("title") or "")[:60]
        print(f"  {e['id'][:8]}  {e.get('mtime', '')[:16]}  t{e.get('turn_count', 0):>4}  {title}")
    print(f"\n━━ ❌ Unmatched ({len(out['unmatched'])}) ━━")
    for e in out["unmatched"]:
        title = (e.get("title") or "")[:60]
        print(f"  {e['id'][:8]}  {e.get('mtime', '')[:16]}  t{e.get('turn_count', 0):>4}  {title}")


# ─── Subcommand: list ───────────────────────────────────────────────────
def cmd_list(args) -> int:
    from gccfork import scan_sessions, cwd_to_slug
    cwd = resolve_cwd(args.cwd)
    sessions = scan_sessions(cwd, args.all)
    out = {
        "scope": {
            "mode": "all" if args.all else "current_cwd",
            "cwd": cwd,
            "slug": cwd_to_slug(cwd),
            "session_count": len(sessions),
        },
        "sessions": [session_to_dict(s) for s in sessions],
    }
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        sc = out["scope"]
        print(f"📁 {sc['cwd']}  ·  sessions {sc['session_count']}")
        for s in out["sessions"]:
            t = (s.get("title") or "")[:60]
            print(f"  {s['id'][:8]}  {(s.get('mtime') or '')[:16]}  t{s.get('turn_count', 0):>4}  {t}")
    return 0


# ─── Subcommand: detail ─────────────────────────────────────────────────
def cmd_detail(args) -> int:
    from gccfork import scan_sessions, cwd_to_slug
    cwd = resolve_cwd(args.cwd)
    sessions = scan_sessions(cwd, scope_all=True)  # detail searches all scopes
    sid = resolve_sid(args.sid, sessions)
    if not sid:
        print(f"❌ ambiguous or missing session ID: '{args.sid}'", file=sys.stderr)
        return 2
    s = next((x for x in sessions if x.id == sid), None)
    if s is None:
        print(f"❌ session not found: {sid}", file=sys.stderr)
        return 2
    out = session_to_dict(s)
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        for k, v in out.items():
            print(f"  {k:>16}: {v}")
    return 0


# ─── Subcommand: ancestry / parent-of ───────────────────────────────────
def cmd_ancestry(args) -> int:
    from gccfork import cmd_ancestry as _impl
    return _impl(args.sid, as_json=args.json)


def cmd_parent_of(args) -> int:
    from gccfork import cmd_parent_of as _impl
    return _impl(args.sid, as_json=args.json)


# ─── Subcommand: rename ─────────────────────────────────────────────────
def cmd_rename(args) -> int:
    from gccfork import scan_sessions, registry_set
    cwd = resolve_cwd(args.cwd)
    sessions = scan_sessions(cwd, scope_all=True)
    sid = resolve_sid(args.sid, sessions)
    if not sid:
        print(f"❌ ambiguous or missing session ID: '{args.sid}'", file=sys.stderr)
        return 2
    name = args.name.strip()
    if not name:
        print("❌ name is empty", file=sys.stderr)
        return 2
    with registry_lock():
        registry_set(sid, name=name)
    print(f"✅ rename {sid[:8]} → '{name}'")
    return 0


# ─── Subcommand: slim — fork with slimmed transcript body ───────────────
def cmd_slim(args) -> int:
    """Create a slim fork by keeping/stubbing/dropping lines per mode.

    Example: gccfork slim 4c03 --mode medium --name "test"
    """
    from gccfork import scan_sessions, slim_fork_session_with, registry_get
    from gccfork_settings import SLIM_MODE_ALIASES
    cwd = resolve_cwd(args.cwd)
    sessions = scan_sessions(cwd, scope_all=True)
    sid = resolve_sid(args.sid, sessions)
    if not sid:
        print(f"❌ ambiguous or missing session ID: '{args.sid}'", file=sys.stderr)
        return 2
    src = next((x for x in sessions if x.id == sid), None)
    if src is None:
        print(f"❌ session not found: {sid}", file=sys.stderr)
        return 2
    # Migrate old keys (strict/balanced/loose) to new keys.
    mode = SLIM_MODE_ALIASES.get(args.mode, args.mode)
    if mode not in {"strong", "medium", "weak"}:
        print(f"❌ unknown mode '{args.mode}' (strong/medium/weak)", file=sys.stderr)
        return 2

    new_id = str(uuid.uuid4())
    name = (args.name or f"🔻{new_id[:4]}[<= {src.id[:4]}]").strip()
    with registry_lock():
        new_sess = slim_fork_session_with(src, new_id, name, mode)
    reg = registry_get(new_id) or {}
    stats = reg.get("slim_stats") or {}
    out = {
        "old_id": src.id,
        "old_size_bytes": src.size_bytes,
        "new_id": new_sess.id,
        "new_size_bytes": new_sess.size_bytes,
        "mode": args.mode,
        "name": name,
        "jsonl_path": str(new_sess.jsonl_path),
        "stats": stats,
        "ratio_pct": round(new_sess.size_bytes * 100 / src.size_bytes, 2)
                     if src.size_bytes else 0,
    }
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(f"🔻 slim fork ({args.mode})  {src.id[:8]} → {new_sess.id[:8]}  '{name}'")
        print(f"   original {src.size_bytes:,}B → slim {new_sess.size_bytes:,}B  ({out['ratio_pct']}%)")
        if stats:
            print(f"   KEEP {stats.get('kept', 0)} / STUB {stats.get('stubbed', 0)} / DROP {stats.get('dropped', 0)}")
        print(f"   {new_sess.jsonl_path}")
    return 0


# ─── Subcommand: slim-inplace — slim while preserving sid ───────────────
def cmd_slim_inplace(args) -> int:
    """Slim in-place while preserving the same sid.

    Unlike fork mode, this does not create a new JSONL or registry entry.
    Re-running/resuming the same session then uses fewer context tokens.
    backup=True creates `.bak.<ts>.jsonl` automatically.
    `--keep-recent N` protects the latest N lines for active sessions.
    """
    # Prefer Rust delegation. If unavailable/disabled, fall back to Python.
    from gccfork_rust_dispatch import try_rust_slim_inplace
    rc = try_rust_slim_inplace(args)
    if rc is not None:
        return rc

    from gccfork import scan_sessions, slim_fork_session_with
    from gccfork_settings import SLIM_MODE_ALIASES
    cwd = resolve_cwd(args.cwd)
    sessions = scan_sessions(cwd, scope_all=True)
    sid = resolve_sid(args.sid, sessions)
    if not sid:
        print(f"❌ ambiguous or missing session ID: '{args.sid}'", file=sys.stderr)
        return 2
    src = next((x for x in sessions if x.id == sid), None)
    if src is None:
        print(f"❌ session not found: {sid}", file=sys.stderr)
        return 2
    mode = SLIM_MODE_ALIASES.get(args.mode, args.mode)
    if mode not in {"strong", "medium", "weak"}:
        print(f"❌ unknown mode '{args.mode}' (strong/medium/weak)", file=sys.stderr)
        return 2

    # Anti-fragmentation follows settings unless explicitly specified.
    from gccfork import pref_get
    anti_frag = bool(getattr(args, "anti_fragmentation", None)) \
        if hasattr(args, "anti_fragmentation") and args.anti_fragmentation is not None \
        else bool(pref_get("slim_default_anti_fragmentation", False))

    result = slim_fork_session_with(
        src,
        src.id,           # Ignored for in-place mode; session.id is used.
        "",               # custom_name does not matter here.
        mode,
        in_place=True,
        backup=not args.no_backup,
        keep_recent_lines=args.keep_recent,
        keep_recent_turns=getattr(args, "keep_recent_turns", 0) or 0,
        dry_run=args.dry_run,
        anti_fragmentation=anti_frag,
    )
    # In-place branch must return a dict.
    assert isinstance(result, dict), "in_place branch must return a dict"

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        old_kb = result["old_size"] / 1024
        new_kb = result["new_size"] / 1024
        saved = 100 - result["ratio_pct"]
        tag = "🔍 dry-run" if args.dry_run else "🔻 in-place slim"
        print(f"{tag} ({result['mode']})  sid={result['sid'][:8]}")
        print(f"   target     : {result['path']}")
        print(f"   marker idx : {result['marker_idx']}  "
              f"({result['pre_kept']:,} pre-marker lines preserved)")
        if result.get("recent_kept"):
            unit = "turns" if (getattr(args, "keep_recent_turns", 0) or 0) > 0 else "lines"
            n = (getattr(args, "keep_recent_turns", 0) or 0) or args.keep_recent
            print(f"   recent KEEP: last {n} {unit} protected "
                  f"({result['recent_kept']:,} lines)")
        print(f"   verdict    : KEEP={result['kept']:,}  STUB={result['stubbed']:,}  "
              f"DROP={result['dropped']:,}  REBIND={result['rebinded']:,}")
        print(f"   size       : {old_kb:,.1f} KB → {new_kb:,.1f} KB ({saved:.1f}% saved)")
        if not args.dry_run and result.get("backup"):
            print(f"   backup     : {result['backup']}")
    return 0


# ─── Subcommand: live-sessions ──────────────────────────────────────────
# ─── Hot-reload helpers: detect PID/sid and write injection sidecar ─────
INJECT_DIR = Path.home() / ".claude" / "gccfork-inject-requests"
INJECT_STATUS_DIR = Path.home() / ".claude" / "gccfork-inject-status"


def find_self_claude_pid_and_sid() -> tuple[Optional[int], Optional[str]]:
    """Return nearest parent Claude PID and sid from the current process tree.

    Walk PPIDs, find the first `/proc/<pid>/comm == "claude"`, then read
    sessionId from sessions/<PID>.json.
    """
    pid = os.getpid()
    for _ in range(32):
        if pid <= 1:
            break
        try:
            comm = Path(f"/proc/{pid}/comm").read_text().strip()
        except OSError:
            return None, None
        if comm == "claude":
            sf = Path.home() / ".claude" / "sessions" / f"{pid}.json"
            if sf.exists():
                try:
                    return pid, json.loads(sf.read_text()).get("sessionId")
                except (OSError, json.JSONDecodeError):
                    return pid, None
            return pid, None
        try:
            for ln in Path(f"/proc/{pid}/status").read_text().split("\n"):
                if ln.startswith("PPid:"):
                    pid = int(ln.split()[1])
                    break
            else:
                return None, None
        except OSError:
            return None, None
    return None, None


def get_ppid(pid: int) -> Optional[int]:
    try:
        for ln in Path(f"/proc/{pid}/status").read_text().split("\n"):
            if ln.startswith("PPid:"):
                return int(ln.split()[1])
    except OSError:
        pass
    return None


def find_claude_for_sid(sid: str) -> tuple[Optional[int], Optional[int]]:
    """Resolve sid -> (claude_pid, shell_pid) by scanning sessions/<PID>.json."""
    started = time.perf_counter()
    sess_dir = Path.home() / ".claude" / "sessions"
    if not sess_dir.is_dir():
        return None, None
    checked = 0
    for sf in sess_dir.glob("*.json"):
        checked += 1
        try:
            data = json.loads(sf.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("sessionId") == sid:
            try:
                cpid = int(sf.stem)
            except ValueError:
                cpid = data.get("pid")
            trace_sar(
                f"find_claude_for_sid matched pid={cpid} files={checked} "
                f"elapsed={(time.perf_counter() - started) * 1000:.1f}ms"
            )
            return cpid, get_ppid(cpid) if cpid else None
    trace_sar(
        f"find_claude_for_sid no-match files={checked} "
        f"elapsed={(time.perf_counter() - started) * 1000:.1f}ms"
    )
    return None, None


def wait_inject_done(req_id: str, *, timeout: float = 5.5) -> dict:
    """Wait for VSCode bridge status ack after sendText/curtain handling."""
    trace_sar(f"wait_inject_done start req={req_id} timeout={timeout:.1f}s")
    status_path = INJECT_STATUS_DIR / f"{req_id}.json"
    deadline = time.monotonic() + timeout
    last_state = None
    while time.monotonic() < deadline:
        try:
            data = json.loads(status_path.read_text())
        except (OSError, json.JSONDecodeError):
            time.sleep(0.05)
            continue
        last_state = data.get("state")
        if last_state == "done":
            trace_sar(f"wait_inject_done done req={req_id} state=done")
            return data
        if last_state == "failed":
            trace_sar(f"wait_inject_done failed req={req_id} error={data.get('error')}")
            raise RuntimeError(f"inject {req_id} failed: {data.get('error')}")
        time.sleep(0.05)
    trace_sar(f"wait_inject_done timeout req={req_id} last_state={last_state}")
    raise TimeoutError(f"inject {req_id} ack timeout (last_state={last_state})")


def write_inject(
    shell_pid: int,
    steps: list[dict],
    req_id: str,
    *,
    curtain_tty: Optional[str] = None,
    wait_for: Optional[dict] = None,
    ack_timeout: float = 5.5,
    transaction_timeout_ms: int = 5_000,
) -> dict:
    INJECT_DIR.mkdir(parents=True, exist_ok=True)
    INJECT_STATUS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "targetShellPid": shell_pid,
        "steps": steps,
        "requestId": req_id,
        "transactionTimeoutMs": transaction_timeout_ms,
    }
    if curtain_tty:
        payload["curtainTty"] = curtain_tty
    if wait_for:
        payload["waitFor"] = wait_for
    out = INJECT_DIR / f"{req_id}.json"
    tmp = INJECT_DIR / f"{req_id}.json.tmp"
    status_path = INJECT_STATUS_DIR / f"{req_id}.json"
    try:
        status_path.unlink()
    except OSError:
        pass
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    trace_sar(
        f"write_inject request ready req={req_id} steps={len(steps)} "
        f"curtain={'on' if curtain_tty else 'off'} wait={wait_for.get('kind') if wait_for else 'none'}"
    )
    os.replace(tmp, out)
    trace_sar(f"write_inject request published req={req_id}")
    try:
        return wait_inject_done(req_id, timeout=ack_timeout)
    except Exception:
        # If the bridge has not picked the request yet, remove it so a later
        # polling cycle cannot execute stale input.
        for p in (out, tmp):
            try:
                p.unlink()
            except OSError:
                pass
        raise


def build_command_steps(command: str, *, preclear: bool = True) -> list[dict]:
    """Build injection steps for slash commands such as /clear and /resume.

    Stale textarea contents would prefix `/clear` and turn the slash command
    into user text. ESC risks aborting an answer, and repeated Backspace can be
    swallowed by bracketed paste, so Ctrl+U clears the input line first.
    """
    if not preclear:
        return [{"text": command, "delayMs": 0, "addNewLine": True}]
    return [
        {"text": "\x15", "delayMs": 0, "addNewLine": False},  # Ctrl+U: clear input line
        {"text": command, "delayMs": 50, "addNewLine": True},
    ]


def _split_control_sequence(sequence: str) -> list[dict]:
    """Send chord keys through VSCode sendText as individual keypresses."""
    return [
        {"text": ch, "delayMs": 20 if idx else 0, "addNewLine": False}
        for idx, ch in enumerate(sequence)
    ]


def build_keybinding_steps(command: str) -> list[dict]:
    if command == CLEAR_SLASH:
        return _split_control_sequence(KEYBIND_CLEAR_SEQUENCE)
    if command.startswith(f"{RESUME_SLASH} "):
        return _split_control_sequence(KEYBIND_RESUME_SEQUENCE)
    return build_command_steps(command, preclear=True)


def ensure_gccfork_keybindings(sid: str) -> bool:
    """Install keybindings that run /clear and /resume without typing.

    The executor passes action strings as `/${action[8:]}`, so
    `command:resume <sid>` becomes `/resume <sid>`.
    """
    KEYBINDINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    existed = KEYBINDINGS_PATH.exists()
    if existed:
        try:
            data = json.loads(KEYBINDINGS_PATH.read_text())
        except (OSError, json.JSONDecodeError):
            ts = int(time.time())
            backup = KEYBINDINGS_PATH.with_suffix(f".json.bak-invalid-{ts}")
            try:
                KEYBINDINGS_PATH.replace(backup)
                trace_sar(f"keybindings invalid backup={backup.name}")
            except OSError:
                pass
            data = {"bindings": []}
    else:
        data = {"bindings": []}

    if not isinstance(data, dict):
        data = {"bindings": []}
    blocks = data.get("bindings")
    if not isinstance(blocks, list):
        blocks = []
        data["bindings"] = blocks

    chat_block = None
    for block in blocks:
        if isinstance(block, dict) and block.get("context") == "Chat" and isinstance(block.get("bindings"), dict):
            chat_block = block
            break
    if chat_block is None:
        chat_block = {"context": "Chat", "bindings": {}}
        blocks.append(chat_block)

    bindings = chat_block["bindings"]
    assert isinstance(bindings, dict)
    desired = {
        KEYBIND_CLEAR_CHORD: "command:clear",
        KEYBIND_RESUME_CHORD: f"command:resume {sid}",
    }
    if all(bindings.get(k) == v for k, v in desired.items()):
        return False

    if existed:
        try:
            import shutil
            backup = KEYBINDINGS_PATH.with_suffix(f".json.bak-gccfork-{int(time.time())}")
            shutil.copy2(KEYBINDINGS_PATH, backup)
            trace_sar(f"keybindings backup={backup.name}")
        except OSError:
            pass
    bindings.update(desired)

    tmp = KEYBINDINGS_PATH.with_suffix(".json.tmp-gccfork")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, KEYBINDINGS_PATH)
    return True


def _resolve_target_pid_and_sid(args) -> tuple[Optional[int], Optional[int], Optional[str]]:
    """Resolve --self / --sid into (claude_pid, shell_pid, sid)."""
    trace_sar("resolve target start")
    # Reuse decorator result when available to avoid duplicate prints.
    cached = getattr(args, "_resolved_cache", None)
    if cached is not None:
        trace_sar("resolve target from cache")
        return cached
    if getattr(args, "self_target", False):
        cpid, sid = find_self_claude_pid_and_sid()
        if not cpid or not sid:
            print("❌ --self: could not find claude in the parent chain "
                  "(works only inside Claude Bash or hooks)",
                  file=sys.stderr)
            return None, None, None
        spid = get_ppid(cpid)
        return cpid, spid, sid
    if getattr(args, "sid", None):
        # Allow sid prefixes; recover full sid from session scan.
        cwd = resolve_cwd(getattr(args, "cwd", None))
        fast_path = find_session_jsonl_fast(args.sid, cwd)
        if fast_path is not None:
            full = fast_path.stem
            trace_sar(f"resolve target fast jsonl={fast_path.name}")
        else:
            from gccfork import scan_sessions
            scan_started = time.perf_counter()
            sessions = scan_sessions(cwd, scope_all=True)
            trace_sar(
                f"resolve target fallback scan_sessions count={len(sessions)} "
                f"elapsed={(time.perf_counter() - scan_started) * 1000:.1f}ms"
            )
            full = resolve_sid(args.sid, sessions)
        if not full:
            print(f"❌ ambiguous or missing session ID: '{args.sid}'", file=sys.stderr)
            return None, None, None
        cpid, spid = find_claude_for_sid(full)
        if not cpid or not spid:
            print(f"❌ sid {full[:8]} has no active Claude instance "
                  "(sessions/<PID>.json missing)",
                  file=sys.stderr)
            return None, None, None
        trace_sar(f"resolve target done sid={full[:8]} claude_pid={cpid} shell_pid={spid}")
        return cpid, spid, full
    print("❌ either --self or --sid is required", file=sys.stderr)
    return None, None, None


# ─── Subcommand: hot-reload — /clear + /resume <sid> only ───────────────
def inject_slash_command(
    *,
    shell_pid: int,
    claude_pid: int,
    sid: str,
    command: str,
    req_id: str,
    preclear: bool,
    use_curtain: bool,
    wait_kind: str,
    jsonl_dir: Optional[str] = None,
    inject_mode: str = "keybinding",
) -> dict:
    """Inject a slash command through a bridge transaction.

    Curtain applies only during the bridge sendText handling window, not the
    whole CLI process. Before ALT_OFF, the bridge watches sid/status changes in
    ~/.claude/sessions/<pid>.json.
    """
    if inject_mode == "keybinding" and command == CLEAR_SLASH and wait_kind == "clear-jsonl":
        # Hidden /clear returns "skip", so there may be no new jsonl marker.
        # Accept session-id changes, session heartbeat updates, or legacy jsonl markers.
        wait_kind = "clear-done"

    curtain_tty = None
    if use_curtain:
        trace_sar(f"curtain tty lookup start pid={claude_pid}")
        try:
            from gccfork_curtain import find_claude_tty
            curtain_tty = find_claude_tty(claude_pid)
        except Exception:
            curtain_tty = None
        trace_sar(f"curtain tty lookup done tty={curtain_tty or 'none'}")
    wait_for = {
        "kind": wait_kind,
        "claudePid": claude_pid,
        "sessionId": sid,
        "status": "any" if wait_kind == "session-not" else "idle",
        "timeoutMs": 4500,
        "sinceMs": int(time.time() * 1000),
    }
    if jsonl_dir:
        wait_for["projectDir"] = jsonl_dir
    if inject_mode == "keybinding":
        changed = ensure_gccfork_keybindings(sid)
        if changed:
            trace_sar("keybindings changed; wait hot-reload 0.75s")
            time.sleep(0.75)
        steps = build_keybinding_steps(command)
    else:
        steps = build_command_steps(command, preclear=preclear)
    return write_inject(
        shell_pid,
        steps,
        req_id,
        curtain_tty=curtain_tty,
        wait_for=wait_for,
        ack_timeout=5.5,
        transaction_timeout_ms=5_000,
    )


def _ensure_claude_patched(args, claude_pid: int, label: str) -> int:
    """Pre-chord patch gate — verifies the active claude binary has the
    hidden-clear patch applied. Required before any chord injection because
    claude code auto-updates silently and a fresh version starts unpatched
    → /clear chord becomes a visible empty user message (race / 🔻 lost).

    Behavior:
      - Default: check + auto-apply patch (idempotent, ~hundreds of ms).
      - `--skip-claude-patch-check`: skip the gate entirely.
      - `--no-auto-patch-claude`: report status only, do not apply.
      - `--require-hidden-clear`: refuse to proceed when patch missing or
        a running process holds a deleted (unpatched) inode.

    Returns 0 on success/skip, 5 on strict failure.
    """
    if getattr(args, "skip_claude_patch_check", False):
        return 0
    try:
        from gccfork_claude_patch import check_and_patch, format_report
        report = check_and_patch(
            auto=getattr(args, "auto_patch_claude", True),
            running_pids=[claude_pid],
        )
        if not report.ok or report.restart_required:
            print(f"⚠ {label} — Claude hidden-clear patch status:", file=sys.stderr)
            print(format_report(report), file=sys.stderr)
            if getattr(args, "require_hidden_clear", False):
                return 5
        else:
            try:
                trace_sar(f"{label}: claude patch check ok")
            except Exception:
                pass
    except Exception as exc:
        print(f"⚠ {label} — Claude hidden-clear patch check failed: {exc}",
              file=sys.stderr)
        if getattr(args, "require_hidden_clear", False):
            return 5
    return 0


def cmd_hot_reload(args) -> int:
    """Hot-reload the same PID by injecting /clear and /resume <sid>.

    The same process rereads JSONL. /clear writes a SessionStart marker, then
    /resume reads after that marker.
    """
    cpid, spid, sid = _resolve_target_pid_and_sid(args)
    if not cpid or not spid or not sid:
        return 2
    clear_slash, resume_slash = CLEAR_SLASH, RESUME_SLASH

    # Patch gate — verify claude binary has hidden-clear patch right before
    # we inject chord. Catches mid-session auto-updates (claude updates
    # silently while a TUI is open, leaving the next chord unpatched).
    rc_patch = _ensure_claude_patched(args, cpid, "hot-reload")
    if rc_patch:
        return rc_patch

    print(f"🚀 hot-reload  sid={sid[:8]}  claude PID={cpid}  shell PID={spid}", file=sys.stderr)

    if args.initial_delay > 0:
        print(f"  sleep {args.initial_delay}s (initial-delay)", file=sys.stderr)
        time.sleep(args.initial_delay)

    preclear = not getattr(args, "no_preclear", False)
    jsonl_dir = None
    try:
        from gccfork import scan_sessions
        cwd = resolve_cwd(getattr(args, "cwd", None))
        src = next((x for x in scan_sessions(cwd, scope_all=True) if x.id == sid), None)
        if src is not None:
            jsonl_dir = str(src.jsonl_path.parent)
    except Exception:
        pass
    if not args.no_clear:
        cid = f"hr-clear-{uuid.uuid4().hex[:8]}"
        try:
            inject_slash_command(
                shell_pid=spid, claude_pid=cpid, sid=sid, command=clear_slash,
                req_id=cid, preclear=preclear, use_curtain=args.curtain,
                wait_kind="clear-jsonl", jsonl_dir=jsonl_dir,
                inject_mode=args.inject_mode,
            )
        except Exception as exc:
            print(f"❌ inject {clear_slash} failed: {exc}", file=sys.stderr)
            return 4
        print(f"  inject {clear_slash}  → {cid}{' (preclear)' if preclear else ''} (ack)", file=sys.stderr)
        if args.clear_wait > 0:
            print(f"  sleep {args.clear_wait}s (clear-wait)", file=sys.stderr)
            time.sleep(args.clear_wait)

    if not args.no_resume:
        rid = f"hr-resume-{uuid.uuid4().hex[:8]}"
        try:
            inject_slash_command(
                shell_pid=spid, claude_pid=cpid, sid=sid, command=f"{resume_slash} {sid}",
                req_id=rid, preclear=preclear, use_curtain=args.curtain,
                wait_kind="session-updated",
                inject_mode=args.inject_mode,
            )
        except Exception as exc:
            print(f"❌ inject {resume_slash} failed: {exc}", file=sys.stderr)
            return 4
        print(f"  inject {resume_slash} {sid[:8]}  → {rid} (ack)", file=sys.stderr)

    if args.json:
        print(json.dumps({
            "sid": sid, "claude_pid": cpid, "shell_pid": spid,
            "cleared": not args.no_clear, "resumed": not args.no_resume,
        }, ensure_ascii=False))
    else:
        print("✓ hot-reload complete", file=sys.stderr)
    return 0


def _repair_last_prompt_leaf(jsonl_path: Path) -> dict | None:
    """Repair last-prompt.leafUuid after slim+reload when it points too early.

    Handles cases where the Claude TUI freezes the leaf on the last user
    message right after chord /clear -> /resume. Returns None when no change is
    needed.
    """
    if not jsonl_path.exists():
        return None
    try:
        lines = jsonl_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return None

    last_prompt_idx = None
    old_leaf = None
    for i in range(len(lines) - 1, -1, -1):
        try:
            d = json.loads(lines[i])
        except Exception:
            continue
        if d.get("type") == "last-prompt":
            last_prompt_idx = i
            old_leaf = d.get("leafUuid")
            break
    if last_prompt_idx is None:
        return None

    new_leaf = None
    for i in range(len(lines) - 1, -1, -1):
        try:
            d = json.loads(lines[i])
        except Exception:
            continue
        if d.get("type") in ("user", "assistant") and d.get("uuid"):
            new_leaf = d.get("uuid")
            break
    if not new_leaf or new_leaf == old_leaf:
        return None

    orphans = 0
    found_old = False
    for line in lines:
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("uuid") == old_leaf:
            found_old = True
            continue
        if found_old and d.get("type") in ("user", "assistant"):
            orphans += 1

    try:
        d = json.loads(lines[last_prompt_idx])
        d["leafUuid"] = new_leaf
        lines[last_prompt_idx] = json.dumps(d, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return None

    tmp = jsonl_path.with_name(jsonl_path.name + f".leaf-repair.{os.getpid()}")
    try:
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(tmp, jsonl_path)
    except Exception:
        try: tmp.unlink()
        except Exception: pass
        return None

    return {"old": old_leaf, "new": new_leaf, "orphans": orphans}


# ─── Subcommand: slim-and-reload — slim + /clear + /resume ──────────────
def cmd_slim_and_reload(args) -> int:
    """Automatic slim + hot-reload via slim-inplace, /clear, and /resume.

    Flow:
      1. optional initial-delay
      2. inject /clear     write SessionStart marker
      3. (clear-wait)
      4. slim-inplace      atomic JSONL slim + .bak
      5. (resume-wait)
      6. inject /resume    same PID reloads slimmed JSONL
    """
    # Prefer Rust delegation. If unavailable/disabled, fall back to Python.
    from gccfork_rust_dispatch import try_rust_slim_and_reload
    rc = try_rust_slim_and_reload(args)
    if rc is not None:
        return rc

    trace_sar("cmd_slim_and_reload entered")
    from gccfork import scan_sessions, slim_fork_session_with
    from gccfork_settings import SLIM_MODE_ALIASES
    trace_sar("cmd imports complete")

    cpid, spid, sid = _resolve_target_pid_and_sid(args)
    if not cpid or not spid or not sid:
        return 2
    clear_slash, resume_slash = CLEAR_SLASH, RESUME_SLASH

    # Pre-chord patch gate — same helper as hot-reload. Catches
    # claude code auto-updates that happened while the TUI was open.
    rc_patch = _ensure_claude_patched(args, cpid, "slim-and-reload")
    if rc_patch:
        return rc_patch

    cwd = resolve_cwd(getattr(args, "cwd", None))
    trace_sar(f"source resolve start cwd={cwd}")
    src_path = find_session_jsonl_fast(sid, cwd)
    if src_path is not None:
        src = make_minimal_session_for_jsonl(src_path, sid, cwd)
        trace_sar(f"source resolve fast jsonl={src_path.name}")
    else:
        scan_started = time.perf_counter()
        sessions = scan_sessions(cwd, scope_all=True)
        trace_sar(
            f"source resolve fallback scan_sessions count={len(sessions)} "
            f"elapsed={(time.perf_counter() - scan_started) * 1000:.1f}ms"
        )
        src = next((x for x in sessions if x.id == sid), None)
    if src is None:
        print(f"❌ session not found: {sid}", file=sys.stderr)
        return 2

    mode = SLIM_MODE_ALIASES.get(args.mode, args.mode)
    if mode not in {"strong", "medium", "weak"}:
        print(f"❌ unknown mode '{args.mode}' (strong/medium/weak)", file=sys.stderr)
        return 2

    print(f"🚀 slim-and-reload  sid={sid[:8]}  claude PID={cpid}  shell PID={spid}",
          file=sys.stderr)
    keep_turns_val = getattr(args, "keep_recent_turns", 0) or 0
    if keep_turns_val > 0:
        print(f"   mode={mode}  keep-recent-turns={keep_turns_val}", file=sys.stderr)
    else:
        print(f"   mode={mode}  keep-recent-lines={args.keep_recent}", file=sys.stderr)
    trace_sar("header printed")

    # Preflight runs checks in parallel and refuses entry on failure.
    if not getattr(args, "no_preflight", False):
        from gccfork_preflight import run_preflight, spawn_lock_holder
        trace_sar("preflight start")
        pf = run_preflight(sid=sid, jsonl_path=src.jsonl_path, claude_pid=cpid)
        trace_sar("preflight done")
        print(f"  {pf.summary()}", file=sys.stderr)
        if not pf.ok:
            print(f"❌ preflight fail — slim entry refused (--no-preflight can force it)",
                  file=sys.stderr)
            return 3
        # Hold spawn lock for this function to prevent concurrent same-sid spawns.
        try:
            _spawn_lock = spawn_lock_holder(sid).__enter__()
        except RuntimeError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 3
    else:
        _spawn_lock = None

    if args.initial_delay > 0:
        print(f"  sleep {args.initial_delay}s (initial-delay)", file=sys.stderr)
        time.sleep(args.initial_delay)

    # 1. Pre-clear backup preserves a clean state before /clear appends JSONL
    # lines. Backing up after /clear would capture marker noise and is less
    # useful for recovery.
    pre_backup_path = None
    if not args.no_backup and not args.dry_run:
        import shutil
        ts = int(time.time())
        pre_backup_path = src.jsonl_path.parent / f"{src.jsonl_path.stem}.bak.{ts}.jsonl"
        try:
            trace_sar("pre-clear backup start")
            shutil.copy2(src.jsonl_path, pre_backup_path)
            trace_sar("pre-clear backup done")
            print(f"  ★ pre-clear backup: {pre_backup_path.name} "
                  f"({pre_backup_path.stat().st_size:,} bytes)", file=sys.stderr)
        except OSError as exc:
            print(f"❌ pre-clear backup failed: {exc}", file=sys.stderr)
            if _spawn_lock is not None:
                try: _spawn_lock.__exit__(None, None, None)
                except Exception: pass
            return 4

    # 2. /clear injection is handled as a synchronous bridge transaction,
    # including sendText, curtain, and sid-change observation. Avoid fixed sleep
    # guesses for input echo.
    preclear = not getattr(args, "no_preclear", False)
    cid = None
    clear_inject_time = None  # mtime guard for automatic phantom trash move
    if not args.no_clear:
        cid = f"sar-clear-{uuid.uuid4().hex[:8]}"
        clear_inject_time = time.time()
        try:
            trace_sar(f"inject {clear_slash} start req={cid}")
            inject_slash_command(
                shell_pid=spid, claude_pid=cpid, sid=sid, command=clear_slash,
                req_id=cid, preclear=preclear, use_curtain=args.curtain,
                wait_kind="clear-jsonl", jsonl_dir=str(src.jsonl_path.parent),
                inject_mode=args.inject_mode,
            )
            trace_sar(f"inject {clear_slash} done req={cid}")
        except Exception as exc:
            print(f"❌ inject {clear_slash} failed: {exc}", file=sys.stderr)
            # Even if /clear injection times out/fails, try leaf repair. If the
            # chord reached Claude and polluted the transcript, leaf freeze may
            # have happened, so updating last-prompt can restore the screen.
            try:
                lr = _repair_last_prompt_leaf(src.jsonl_path)
                if lr:
                    print(f"  ⚓ last-prompt leaf repaired after inject failure: "
                          f"{lr['old'][:8] if lr['old'] else '(none)'} → {lr['new'][:8]} "
                          f"({lr['orphans']} orphaned)", file=sys.stderr)
            except Exception as e2:
                print(f"  ⚠ leaf repair failed; ignored: {e2}", file=sys.stderr)
            if _spawn_lock is not None:
                try: _spawn_lock.__exit__(None, None, None)
                except Exception: pass
            return 7
        print(f"  inject {clear_slash}  → {cid}{' (preclear)' if preclear else ''} (ack)", file=sys.stderr)

    import threading
    slim_result_holder: dict = {"result": None, "error": None}

    # Anti-fragmentation option follows the slim_default_anti_fragmentation pref.
    from gccfork import pref_get as _pg
    anti_frag = bool(getattr(args, "anti_fragmentation", None)) \
        if hasattr(args, "anti_fragmentation") and args.anti_fragmentation is not None \
        else bool(_pg("slim_default_anti_fragmentation", False))

    def _slim_worker():
        try:
            slim_result_holder["result"] = slim_fork_session_with(
                src, src.id, "", mode,
                in_place=True,
                backup=False,           # pre-clear backup already exists
                keep_recent_lines=args.keep_recent,
                keep_recent_turns=getattr(args, "keep_recent_turns", 0) or 0,
                dry_run=args.dry_run,
                defer_commit=True,      # defer atomic os.replace to this caller
                anti_fragmentation=anti_frag,
            )
        except Exception as exc:
            slim_result_holder["error"] = exc

    print(f"  slim-inplace start...", file=sys.stderr)
    trace_sar("slim worker start")
    slim_thread = threading.Thread(target=_slim_worker, name="slim-worker")
    slim_thread.start()

    # Main thread: clear-wait sleep while background slim runs.
    if not args.no_clear and args.clear_wait > 0:
        print(f"  sleep {args.clear_wait}s (clear-wait, slim in parallel)", file=sys.stderr)
        time.sleep(args.clear_wait)

    # Wait for background slim completion.
    slim_thread.join()
    trace_sar("slim worker joined")
    if slim_result_holder["error"] is not None:
        print(f"❌ slim failed: {slim_result_holder['error']}", file=sys.stderr)
        if _spawn_lock is not None:
            try: _spawn_lock.__exit__(None, None, None)
            except Exception: pass
        return 5
    result = slim_result_holder["result"]
    assert isinstance(result, dict), "defer_commit branch must return a dict"

    # 3. Atomic commit: os.replace slim tmp_path into the JSONL location.
    # Replacing after /clear may remove the marker written to the old inode, but
    # /resume can still read after the latest SessionStart marker in the new
    # JSONL.
    if not args.dry_run and result.get("tmp_path") and result.get("new_path"):
        try:
            trace_sar("atomic commit start")
            os.replace(result["tmp_path"], result["new_path"])
            result["committed"] = True
            trace_sar("atomic commit done")
        except Exception as exc:
            print(f"❌ atomic commit failed: {exc}", file=sys.stderr)
            try:
                Path(result["tmp_path"]).unlink()
            except Exception:
                pass
            if _spawn_lock is not None:
                try: _spawn_lock.__exit__(None, None, None)
                except Exception: pass
            return 6

    # Attach pre-clear backup path so --json output can reference it.
    if pre_backup_path is not None:
        result["backup"] = str(pre_backup_path)

    if not args.dry_run:
        old_kb = result["old_size"] / 1024
        new_kb = result["new_size"] / 1024
        saved = 100 - result["ratio_pct"]
        print(f"  slim complete: {old_kb:,.1f}KB → {new_kb:,.1f}KB ({saved:.1f}% saved)",
              file=sys.stderr)
        print(f"    KEEP={result['kept']:,}  STUB={result['stubbed']:,}  "
              f"DROP={result['dropped']:,}  REBIND={result['rebinded']:,}",
              file=sys.stderr)
        if result.get("backup"):
            print(f"    backup: {result['backup']}", file=sys.stderr)

    # 3. resume-wait
    if args.resume_wait > 0 and not args.no_resume:
        time.sleep(args.resume_wait)

    # 4. /resume injection using the preclear sequence to avoid races.
    if not args.no_resume and not args.dry_run:
        rid = f"sar-resume-{uuid.uuid4().hex[:8]}"
        try:
            trace_sar(f"inject {resume_slash} start req={rid}")
            inject_slash_command(
                shell_pid=spid, claude_pid=cpid, sid=sid, command=f"{resume_slash} {sid}",
                req_id=rid, preclear=preclear, use_curtain=args.curtain,
                wait_kind="session-updated",
                inject_mode=args.inject_mode,
            )
            trace_sar(f"inject {resume_slash} done req={rid}")
        except Exception as exc:
            print(f"❌ inject {resume_slash} failed: {exc}", file=sys.stderr)
            if _spawn_lock is not None:
                try: _spawn_lock.__exit__(None, None, None)
                except Exception: pass
            return 8
        print(f"  inject {resume_slash} {sid[:8]}  → {rid} (ack)", file=sys.stderr)

    # 5. Auto-trash phantom /clear artifacts. Pattern is deterministic:
    # created after clear_inject_time, <= 10 lines, no assistant messages, and
    # not the current sid. This moves to GccSlim trash instead of deleting.
    phantoms_trashed: list[str] = []
    if (not args.no_phantom_trash and not args.dry_run
            and not args.no_clear and not args.no_resume
            and clear_inject_time is not None):
        try:
            trace_sar("phantom trash scan start")
            from gccfork import move_session_to_trash
            proj_dir = src.jsonl_path.parent
            for jsonl in proj_dir.glob("*.jsonl"):
                if ".bak." in jsonl.name:
                    continue
                if jsonl.stem == sid:
                    continue
                try:
                    if jsonl.stat().st_mtime <= clear_inject_time:
                        continue  # not created by this run
                    if jsonl.stat().st_size > 500_000:
                        continue  # too large; could be a real conversation
                    line_count = 0
                    has_assistant = False
                    with jsonl.open() as f:
                        for line in f:
                            line_count += 1
                            if line_count > 30:
                                break  # safety guard: real conversation
                            try:
                                if json.loads(line).get("type") == "assistant":
                                    has_assistant = True
                                    break
                            except (json.JSONDecodeError, ValueError):
                                pass
                    if line_count > 30 or has_assistant:
                        continue  # not a phantom
                    # Pattern matched; move to trash.
                    if move_session_to_trash(jsonl.stem, jsonl):
                        phantoms_trashed.append(jsonl.stem)
                        print(f"  🗑 phantom trashed: {jsonl.stem[:8]} "
                              f"({line_count} lines, assistant=0)", file=sys.stderr)
                except OSError:
                    continue
            trace_sar(f"phantom trash scan done trashed={len(phantoms_trashed)}")
        except Exception as exc:
            print(f"  ⚠ phantom trash failed; ignored: {exc}", file=sys.stderr)

    # 6. Repair last-prompt.leafUuid when a frozen leaf makes the screen look as
    # if it returned to an older conversation. Force it to the latest normal
    # user/assistant UUID.
    leaf_repair: dict | None = None
    if not args.dry_run:
        try:
            trace_sar("leaf repair start")
            leaf_repair = _repair_last_prompt_leaf(src.jsonl_path)
            if leaf_repair:
                print(f"  ⚓ last-prompt leaf repaired: "
                      f"{leaf_repair['old'][:8] if leaf_repair['old'] else '(none)'}"
                      f" → {leaf_repair['new'][:8]}"
                      f" ({leaf_repair['orphans']} orphaned messages)", file=sys.stderr)
            trace_sar(f"leaf repair done updated={leaf_repair is not None}")
        except Exception as exc:
            print(f"  ⚠ leaf repair failed; ignored: {exc}", file=sys.stderr)

    if args.json:
        out = {
            "sid": sid, "claude_pid": cpid, "shell_pid": spid,
            "cleared": not args.no_clear,
            "resumed": not args.no_resume and not args.dry_run,
            "phantoms_trashed": phantoms_trashed,
            "leaf_repair": leaf_repair,
            "slim": result,
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print("✓ slim-and-reload complete" + (" (dry-run)" if args.dry_run else ""),
              file=sys.stderr)

    # Release spawn lock held by preflight.
    if _spawn_lock is not None:
        try:
            _spawn_lock.__exit__(None, None, None)
        except Exception:
            pass
    return 0


def cmd_live_sessions(args) -> int:
    """Show all active Claude instances with PID/sid/cwd/status.

    Uses sessions/<PID>.json as the truth source, independent of registry.
    """
    from gccfork import read_live_sessions
    sessions = read_live_sessions()
    cwd_filter = resolve_cwd(args.cwd) if args.cwd else None
    if cwd_filter:
        sessions = [d for d in sessions if d.get("cwd") == cwd_filter]
    sessions.sort(key=lambda d: (d.get("status", "") != "busy", -d.get("updatedAt", 0)))

    if args.json:
        print(json.dumps(sessions, ensure_ascii=False, indent=2))
        return 0

    if not sessions:
        print("No active Claude instances.")
        return 0
    print(f"Active Claude instances: {len(sessions)}"
          + (f" (cwd={cwd_filter})" if cwd_filter else ""))
    print()
    print(f"  {'PID':>7}  {'sid':<8}  {'status':<6}  {'cwd':<45}  name")
    print(f"  {'-'*7}  {'-'*8}  {'-'*6}  {'-'*45}  {'-'*30}")
    for d in sessions:
        pid = d.get("pid", "?")
        sid = d.get("sessionId", "?")[:8]
        status = d.get("status", "?")
        cwd_short = (d.get("cwd") or "")[-45:]
        name = (d.get("name") or "")[:60]
        print(f"  {pid:>7}  {sid:<8}  {status:<6}  {cwd_short:<45}  {name}")
    return 0


def cmd_patch_claude(args) -> int:
    """Check/apply the Claude Code hidden /clear transcript patch."""
    # Prefer Rust delegation. If unavailable/disabled, fall back to Python.
    from gccfork_rust_dispatch import try_rust_patch_claude
    rc = try_rust_patch_claude(args)
    if rc is not None:
        return rc

    try:
        from gccfork_claude_patch import check_and_patch, format_report
    except Exception as exc:
        print(f"❌ patch helper import failed: {exc}", file=sys.stderr)
        return 2

    report = check_and_patch(
        target_path=getattr(args, "binary", None),
        auto=getattr(args, "auto", False),
        force=getattr(args, "force", False),
    )
    if getattr(args, "json", False):
        print(report.to_json())
    else:
        print(format_report(report), file=sys.stderr)
    if getattr(args, "strict", False) and (not report.ok or report.restart_required):
        return 1
    return 0 if report.ok else 1


# ─── Subcommand: reconcile-live — registry ↔ sessions/<PID>.json sync ───
def cmd_reconcile_live(args) -> int:
    """Reconcile registry from sessions/<PID>.json truth data.

    Use --dry-run to inspect differences and --apply to write changes.
    """
    from gccfork import reconcile_registry_from_live_sessions
    apply = args.apply and not args.dry_run
    result = reconcile_registry_from_live_sessions(apply=apply)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0

    tag = "🔧 applied" if result["applied"] else "🔍 dry-run"
    print(f"{tag}  checked active sessions: {result['live_count']}")
    print(f"   new: {len(result['new'])}  updated: {len(result['updated'])}  unchanged: {result['unchanged_count']}")

    if result["new"]:
        print("\n--- New entries ---")
        for item in result["new"]:
            print(f"  + {item['sid'][:8]}: {item['changes']}")

    if result["updated"]:
        print("\n--- Updates ---")
        for item in result["updated"]:
            print(f"  ~ {item['sid'][:8]}")
            if item['old_name'] != item['live_name']:
                print(f"      old name : {item['old_name'][:70]}")
                print(f"      live name: {item['live_name'][:70]}")
            for k, v in item["changes"].items():
                if k == "name":
                    continue   # already shown above
                print(f"      {k}: {v}")

    if not result["applied"]:
        print("\nApply with: gccfork reconcile-live --apply")
    return 0


# ─── Subcommand: hard-fork ──────────────────────────────────────────────
def cmd_hard_fork(args) -> int:
    from gccfork import scan_sessions, hard_fork_session_with
    cwd = resolve_cwd(args.cwd)
    sessions = scan_sessions(cwd, scope_all=True)
    sid = resolve_sid(args.sid, sessions)
    if not sid:
        print(f"❌ ambiguous or missing session ID: '{args.sid}'", file=sys.stderr)
        return 2
    src = next((x for x in sessions if x.id == sid), None)
    if src is None:
        print(f"❌ session not found: {sid}", file=sys.stderr)
        return 2
    new_id = str(uuid.uuid4())
    name = (args.name or f"{new_id[:4]}[<= {src.id[:4]}]").strip()
    with registry_lock():
        new_sess = hard_fork_session_with(src, new_id, name)
    out = {
        "old_id": src.id,
        "new_id": new_sess.id,
        "name": name,
        "jsonl_path": str(new_sess.jsonl_path),
        "size_bytes": new_sess.size_bytes,
    }
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(f"✅ hard-fork {src.id[:8]} → {new_sess.id[:8]}  '{name}'")
        print(f"   {new_sess.jsonl_path}  ({new_sess.size_bytes:,} B)")
    return 0


# ─── Subcommand: delete (move to trash) ─────────────────────────────────
def cmd_delete(args) -> int:
    from gccfork import scan_sessions, registry_remove, TRASH_DIR
    cwd = resolve_cwd(args.cwd)
    sessions = scan_sessions(cwd, scope_all=True)
    sid = resolve_sid(args.sid, sessions)
    if not sid:
        print(f"❌ ambiguous or missing session ID: '{args.sid}'", file=sys.stderr)
        return 2
    s = next((x for x in sessions if x.id == sid), None)
    if s is None:
        print(f"❌ session not found: {sid}", file=sys.stderr)
        return 2

    if not args.force:
        print(f"⚠ trash confirmation required — '{s.id[:8]}' ({s.title or '(empty)'})")
        print(f"   add --force to execute")
        return 1

    TRASH_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = TRASH_DIR / f"{ts}_{s.jsonl_path.parent.name}_{s.jsonl_path.name}"
    s.jsonl_path.rename(target)
    with registry_lock():
        registry_remove(s.id)
    print(f"🗑 deleted {s.id[:8]} → {target}")
    return 0


# ─── Subcommand: prefs ──────────────────────────────────────────────────
def cmd_prefs(args) -> int:
    from gccfork import load_prefs, pref_get, pref_set
    if args.action == "get":
        if args.key:
            v = pref_get(args.key)
            print(json.dumps(v, ensure_ascii=False) if args.json else v)
        else:
            prefs = load_prefs()
            if args.json:
                print(json.dumps(prefs, ensure_ascii=False, indent=2))
            else:
                for k, v in sorted(prefs.items()):
                    print(f"  {k}: {v}")
    elif args.action == "set":
        # Try JSON literals (true/false/number/string), then fall back to str.
        try:
            value = json.loads(args.value)
        except (json.JSONDecodeError, TypeError):
            value = args.value
        with registry_lock():  # protect prefs with the same lock
            pref_set(args.key, value)
        print(f"✅ {args.key} = {value!r}")
    return 0


# ─── Subcommand: stats ──────────────────────────────────────────────────
def cmd_stats(args) -> int:
    from gccfork import scan_sessions, cwd_to_slug
    cwd = resolve_cwd(args.cwd)
    sessions = scan_sessions(cwd, args.all)
    total_size = sum(s.size_bytes for s in sessions)
    total_turns = sum(s.turn_count for s in sessions)
    compacted = sum(1 for s in sessions if s.compact_count > 0)
    forks = {
        "hard": sum(1 for s in sessions if s.fork_type == "hard"),
        "soft": sum(1 for s in sessions if s.fork_type == "soft"),
        "auto": sum(1 for s in sessions if s.fork_type == "auto"),
        "root": sum(1 for s in sessions if not s.fork_type),
    }
    out = {
        "scope": {
            "mode": "all" if args.all else "current_cwd",
            "cwd": cwd,
            "slug": cwd_to_slug(cwd),
        },
        "session_count": len(sessions),
        "total_size_bytes": total_size,
        "total_turns": total_turns,
        "compacted_count": compacted,
        "fork_types": forks,
    }
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(f"📁 {cwd}  ·  slug={out['scope']['slug']}")
        print(f"   sessions: {out['session_count']:,}")
        print(f"   total size: {total_size / 1024 / 1024:.1f} MB")
        print(f"   total turns: {total_turns:,}")
        print(f"   compacted sessions: {compacted}")
        print(f"   fork: hard={forks['hard']}, soft={forks['soft']}, auto={forks['auto']}, root={forks['root']}")
    return 0


# ─── Dispatcher ─────────────────────────────────────────────────────────
SUBCOMMANDS = {"search", "list", "detail", "ancestry", "parent-of",
               "rename", "hard-fork", "slim", "slim-inplace", "delete", "prefs", "stats",
               "live-sessions", "reconcile-live",
               "hot-reload", "slim-and-reload", "patch-claude", "rust-status"}


def is_subcommand(argv: list[str]) -> bool:
    """Return whether sys.argv[1:] starts with a supported subcommand."""
    return bool(argv) and argv[0] in SUBCOMMANDS


def dispatch(argv: list[str]) -> int:
    """Dispatch headless CLI commands from the main gccfork entrypoint."""
    trace_sar(f"dispatch entered argv={' '.join(argv)}")
    ap = argparse.ArgumentParser(prog="gccfork", description="gccfork headless CLI")
    sp = ap.add_subparsers(dest="cmd", required=True)

    # Shared cwd/all/json argument builder.
    def add_scope(p):
        p.add_argument("--cwd", default=None, help="cwd override")
        p.add_argument("--all", action="store_true", help="scan all projects")
        p.add_argument("--json", action="store_true", help="emit JSON")

    # search
    p_search = sp.add_parser("search", help="scan full text with noise filters")
    p_search.add_argument("query", help="search query")
    p_search.add_argument("--no-filter", action="store_true",
                          help="disable noise filters")
    p_search.add_argument("--fuzzy", action="store_true",
                          help="force fuzzy matcher on")
    p_search.add_argument("--no-fuzzy", action="store_true",
                          help="force fuzzy matcher off")
    add_scope(p_search)
    p_search.set_defaults(func=cmd_search)

    # list
    p_list = sp.add_parser("list", help="list sessions")
    add_scope(p_list)
    p_list.set_defaults(func=cmd_list)

    # detail
    p_detail = sp.add_parser("detail", help="show session detail")
    p_detail.add_argument("sid", help="session ID or unique prefix")
    p_detail.add_argument("--cwd", default=None)
    p_detail.add_argument("--json", action="store_true")
    p_detail.set_defaults(func=cmd_detail)

    # ancestry / parent-of
    p_anc = sp.add_parser("ancestry", help="show ancestry chain")
    p_anc.add_argument("sid")
    p_anc.add_argument("--json", action="store_true")
    p_anc.set_defaults(func=cmd_ancestry)
    p_par = sp.add_parser("parent-of", help="show direct parent")
    p_par.add_argument("sid")
    p_par.add_argument("--json", action="store_true")
    p_par.set_defaults(func=cmd_parent_of)

    # rename
    p_rename = sp.add_parser("rename", help="rename session in registry")
    p_rename.add_argument("sid")
    p_rename.add_argument("name")
    p_rename.add_argument("--cwd", default=None)
    p_rename.set_defaults(func=cmd_rename)

    # hard-fork
    p_hf = sp.add_parser("hard-fork", help="hard fork by cloning JSONL with a new UUID")
    p_hf.add_argument("sid")
    p_hf.add_argument("--name", default=None)
    p_hf.add_argument("--cwd", default=None)
    p_hf.add_argument("--json", action="store_true")
    p_hf.set_defaults(func=cmd_hard_fork)

    # slim: fork with slimmed transcript body
    p_slim = sp.add_parser("slim", help="create a slim fork using line policy")
    p_slim.add_argument("sid")
    p_slim.add_argument("--mode",
                        choices=["strong", "medium", "weak", "strict", "balanced", "loose"],
                        default="strong",
                        help="strong=smallest, medium=readable flow, weak=preserve tool_result too; legacy strict/balanced/loose accepted")
    p_slim.add_argument("--name", default=None)
    p_slim.add_argument("--cwd", default=None)
    p_slim.add_argument("--json", action="store_true")
    p_slim.set_defaults(func=cmd_slim)

    # slim-inplace: preserve the same sid
    p_si = sp.add_parser("slim-inplace",
                         help="in-place slim with same sid, overwriting original JSONL")
    p_si.add_argument("sid")
    p_si.add_argument("--mode",
                      default="strong",
                      choices=["strong", "medium", "weak", "strict", "balanced", "loose"],
                      help="slim strength (default: strong)")
    p_si.add_argument("--keep-recent", type=int, default=50,
                      help="always preserve the last N lines (default: 50; used only when --keep-recent-turns is unset)")
    p_si.add_argument("--keep-recent-turns", type=int, default=0,
                      help="preserve last N user turns; takes precedence over line count. 0=disabled")
    p_si.add_argument("--dry-run", action="store_true",
                      help="show statistics without changing files")
    p_si.add_argument("--no-backup", action="store_true",
                      help="do not create .bak.<ts>.jsonl backup")
    p_si.add_argument("--cwd", default=None)
    p_si.add_argument("--json", action="store_true")
    p_si.set_defaults(func=cmd_slim_inplace)

    # live-sessions: show all active Claude instances
    p_ls = sp.add_parser("live-sessions",
                         help="show all active Claude instances from sessions/<PID>.json")
    p_ls.add_argument("--cwd", default=None,
                      help="filter to a specific cwd")
    p_ls.add_argument("--json", action="store_true")
    p_ls.set_defaults(func=cmd_live_sessions)

    # reconcile-live: sync registry from live sessions
    p_rl = sp.add_parser("reconcile-live",
                         help="sync registry from sessions/<PID>.json truth")
    p_rl.add_argument("--apply", action="store_true",
                      help="write changes (default is dry-run)")
    p_rl.add_argument("--dry-run", action="store_true",
                      help="force dry-run, ignoring --apply")
    p_rl.add_argument("--json", action="store_true")
    p_rl.set_defaults(func=cmd_reconcile_live)

    # delete
    p_del = sp.add_parser("delete", help="move session to trash")
    p_del.add_argument("sid")
    p_del.add_argument("--force", action="store_true")
    p_del.add_argument("--cwd", default=None)
    p_del.set_defaults(func=cmd_delete)

    # prefs
    p_pref = sp.add_parser("prefs", help="get/set preferences")
    p_pref_sub = p_pref.add_subparsers(dest="action", required=True)
    p_pref_get = p_pref_sub.add_parser("get")
    p_pref_get.add_argument("key", nargs="?", default=None)
    p_pref_get.add_argument("--json", action="store_true")
    p_pref_get.set_defaults(func=cmd_prefs)
    p_pref_set = p_pref_sub.add_parser("set")
    p_pref_set.add_argument("key")
    p_pref_set.add_argument("value", help="JSON literal or string")
    p_pref_set.add_argument("--json", action="store_true")
    p_pref_set.set_defaults(func=cmd_prefs)

    # stats
    p_stats = sp.add_parser("stats", help="scope statistics")
    add_scope(p_stats)
    p_stats.set_defaults(func=cmd_stats)

    # patch-claude: detect Claude Code updates and apply hidden /clear patch
    p_patch = sp.add_parser(
        "patch-claude",
        help="check/apply Claude Code hidden /clear transcript patch",
    )
    p_patch.add_argument("--auto", action="store_true",
                         help="auto-patch after backup when missing")
    p_patch.add_argument("--binary", default=None,
                         help="target Claude version binary path (default: latest ~/.local/share/claude/versions/*)")
    p_patch.add_argument("--force", action="store_true",
                         help="force replacement even when expected string counts differ (dangerous)")
    p_patch.add_argument("--strict", action="store_true",
                         help="exit 1 when patch is missing or Claude restart is required")
    p_patch.add_argument("--json", action="store_true")
    p_patch.set_defaults(func=cmd_patch_claude)

    # rust-status: Rust delegation diagnostics
    def cmd_rust_status(_args) -> int:
        from gccfork_rust_dispatch import status_summary
        print(status_summary())
        return 0
    p_rust = sp.add_parser("rust-status", help="diagnose Rust binary delegation status")
    p_rust.set_defaults(func=cmd_rust_status)

    # hot-reload: inject /clear + /resume <sid> into the same PID
    def add_target(p):
        g = p.add_mutually_exclusive_group(required=True)
        g.add_argument("--self", dest="self_target", action="store_true",
                       help="detect Claude from parent chain")
        g.add_argument("--sid", default=None,
                       help="explicit sid or unique prefix")

    p_hr = sp.add_parser("hot-reload",
                         help="hot-reload same PID with /clear + /resume <sid>")
    add_target(p_hr)
    p_hr.add_argument("--initial-delay", type=float, default=0.0,
	                      help="initial sleep to wait for answer completion (default: 0)")
    p_hr.add_argument("--clear-wait", type=float, default=0.0,
	                      help="extra wait after /clear ack (default: 0)")
    p_hr.add_argument("--no-clear", action="store_true",
	                      help="skip /clear injection")
    p_hr.add_argument("--no-resume", action="store_true",
	                      help="skip /resume injection")
    p_hr.add_argument("--no-preclear", action="store_true",
	                      help="compatibility no-op; control-character preclear is currently disabled")
    p_hr.add_argument("--inject-mode", choices=["keybinding", "text"], default="keybinding",
	                      help="slash command execution mode (default: keybinding; text=legacy sendText)")
    p_hr.set_defaults(curtain=False)
    p_hr.add_argument("--curtain", dest="curtain", action="store_true",
	                      help="use experimental TTY curtain (not recommended for current VSCode/Claude TUI)")
    p_hr.add_argument("--no-curtain", dest="curtain", action="store_false",
	                      help="skip TTY curtain (alt screen), for debug/logging")
    # Pre-chord patch gate — fires every invocation (mid-session
    # claude auto-updates can leave the binary unpatched between calls).
    p_hr.add_argument("--no-auto-patch-claude", dest="auto_patch_claude",
                      action="store_false",
	                      help="skip automatic Claude Code hidden /clear patch")
    p_hr.add_argument("--skip-claude-patch-check", action="store_true",
	                      help="skip Claude Code hidden /clear patch status check")
    p_hr.add_argument("--require-hidden-clear", action="store_true",
	                      help="stop hot-reload when patch is missing or restart is required")
    p_hr.add_argument("--cwd", default=None)
    p_hr.add_argument("--json", action="store_true")
    p_hr.set_defaults(func=cmd_hot_reload, auto_patch_claude=True)

    # slim-and-reload: slim-inplace + /clear + /resume
    p_sar = sp.add_parser("slim-and-reload",
	                          help="automatic slim + hot-reload (slim-inplace + /clear + /resume)")
    add_target(p_sar)
    p_sar.add_argument("--mode", default="strong",
                       choices=["strong", "medium", "weak", "strict", "balanced", "loose"],
	                       help="slim strength (default: strong)")
    p_sar.add_argument("--keep-recent", type=int, default=50,
	                       help="always preserve the last N lines (default: 50; used only when --keep-recent-turns is unset)")
    p_sar.add_argument("--keep-recent-turns", type=int, default=0,
	                       help="preserve last N user turns; takes precedence over line count. 0=disabled")
    p_sar.add_argument("--initial-delay", type=float, default=0.0,
	                       help="initial sleep (default: 0; 5 recommended from hooks)")
    p_sar.add_argument("--clear-wait", type=float, default=0.0,
	                       help="extra wait after /clear ack before slim starts (default: 0)")
    p_sar.add_argument("--resume-wait", type=float, default=0.0,
	                       help="extra wait after slim before /resume (default: 0)")
    p_sar.add_argument("--no-clear", action="store_true",
	                       help="skip /clear injection")
    p_sar.add_argument("--no-resume", action="store_true",
	                       help="skip /resume injection")
    p_sar.add_argument("--no-preclear", action="store_true",
	                       help="compatibility no-op; control-character preclear is currently disabled")
    p_sar.add_argument("--inject-mode", choices=["keybinding", "text"], default="keybinding",
	                       help="slash command execution mode (default: keybinding; text=legacy sendText)")
    p_sar.add_argument("--no-phantom-trash", action="store_true",
	                       help="skip auto-trash of empty phantom JSONL artifacts from /clear")
    p_sar.add_argument("--no-preflight", action="store_true",
	                       help="skip all preflight checks (dangerous; debug only)")
    p_sar.add_argument("--no-auto-patch-claude", dest="auto_patch_claude", action="store_false",
	                       help="skip automatic Claude Code hidden /clear patch")
    p_sar.add_argument("--skip-claude-patch-check", action="store_true",
	                       help="skip Claude Code hidden /clear patch status check")
    p_sar.add_argument("--require-hidden-clear", action="store_true",
	                       help="stop slim-and-reload when patch is missing or restart is required")
    p_sar.add_argument("--no-backup", action="store_true",
	                       help="do not create slim .bak backup")
    p_sar.set_defaults(curtain=False)
    p_sar.add_argument("--curtain", dest="curtain", action="store_true",
	                       help="use experimental TTY curtain (not recommended for current VSCode/Claude TUI)")
    p_sar.add_argument("--no-curtain", dest="curtain", action="store_false",
	                       help="skip TTY curtain (alt screen), for debug/logging")
    p_sar.add_argument("--dry-run", action="store_true",
	                       help="show statistics without changing files")
    p_sar.add_argument("--cwd", default=None)
    p_sar.add_argument("--json", action="store_true")
    p_sar.set_defaults(func=cmd_slim_and_reload)

    trace_sar("argparse setup complete")
    args = ap.parse_args(argv)
    trace_sar(f"argparse parsed cmd={args.cmd}")
    return args.func(args)
