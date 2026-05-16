"""gccfork — headless CLI 사이드카 모듈.

TUI 가 떠있지 않아도 외부에서 모든 핵심 기능을 호출 가능. 각 subcommand 는
gccfork.py 의 stateless 함수를 import 해서 실행하므로 본문 수정 없이 동작.

진입점:
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

cwd 결정 우선순위:
    1) --cwd 플래그
    2) GCCFORK_CWD 환경변수
    3) $PWD / os.getcwd()

JSON 출력 (--json): 자동화 친화. 없으면 사람-친화 텍스트.

Registry 락:
    rename / hard-fork / delete 는 fcntl.flock 으로 read-modify-write 보호.
    같은 시점에 TUI 가 registry 를 갱신해도 마지막 쓰기 충돌을 막음.
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


# ─── cwd 결정 ────────────────────────────────────────────────────────────
def resolve_cwd(arg_cwd: Optional[str]) -> str:
    """--cwd > GCCFORK_CWD > $PWD / os.getcwd()."""
    if arg_cwd:
        return os.path.abspath(arg_cwd)
    env = os.environ.get("GCCFORK_CWD")
    if env:
        return os.path.abspath(env)
    return os.environ.get("PWD") or os.getcwd()


# ─── Registry 락 (multi-process 안전) ─────────────────────────────────────
_LOCK_PATH = Path.home() / ".claude" / ".gccfork-cli.lock"


@contextmanager
def registry_lock():
    """fcntl.flock 으로 ~/.claude/.gccfork-cli.lock 점유.

    TUI 가 registry 를 동시에 쓸 가능성에 대비. 실패 시 5초 대기.
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


# ─── 세션 ID 단축 → 전체 ID 매핑 ─────────────────────────────────────────
def resolve_sid(short: str, sessions) -> Optional[str]:
    """`3a35` 또는 `f46da6c8` 같은 prefix 를 받아 매치되는 session.id 반환.

    1) 정확 매치 우선 (full UUID 찍었을 때)
    2) prefix 매치 (8자든 4자든 길이 무관, unique 면 OK)
    0개 또는 2개 이상 → None (모호)
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
    """파일명 기준 sid resolve. 전체 jsonl 파싱 없이 대상 세션 파일만 찾는다."""
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
    """slim-and-reload fast path용 최소 Session. 전체 parse_session 생략."""
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


# ─── Session → JSON ──────────────────────────────────────────────────────
def session_to_dict(s) -> dict:
    """Session 객체 → JSON 직렬화 가능 dict."""
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
    """본문 전체 스캔 — 5개 노이즈 필터 적용 (디폴트 prefs 또는 --no-filter)."""
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

    # fuzzy 매처 — prefs 의 deep_include_fuzzy 플래그 또는 --fuzzy CLI 강제
    # --no-fuzzy 가 가장 우선 (사용자가 명시적으로 끔).
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
    print(f"📁 스코프: {sc['mode']}  ·  {sc['cwd']}")
    print(f"   slug: {sc['slug']}  ·  세션 {sc['session_count']}개")
    flt = "ON (디폴트, 5개 노이즈 제외)" if f["noise_filter"] else "OFF (--no-filter, 옛 동작)"
    fz = "rapidfuzz" if f["fuzzy"] else "off"
    print(f"🔍 query: '{out['query']}'  ·  noise filter {flt}  ·  fuzzy {fz}")
    print(f"⏱  {out['elapsed_sec']}s  ·  매치 {len(out['matched'])} / 비매치 {len(out['unmatched'])}\n")
    print(f"━━ ✅ 매치 ({len(out['matched'])}개) ━━")
    for e in out["matched"]:
        title = (e.get("title") or "")[:60]
        print(f"  {e['id'][:8]}  {e.get('mtime', '')[:16]}  t{e.get('turn_count', 0):>4}  {title}")
    print(f"\n━━ ❌ 비매치 ({len(out['unmatched'])}개) ━━")
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
        print(f"📁 {sc['cwd']}  ·  세션 {sc['session_count']}개")
        for s in out["sessions"]:
            t = (s.get("title") or "")[:60]
            print(f"  {s['id'][:8]}  {(s.get('mtime') or '')[:16]}  t{s.get('turn_count', 0):>4}  {t}")
    return 0


# ─── Subcommand: detail ─────────────────────────────────────────────────
def cmd_detail(args) -> int:
    from gccfork import scan_sessions, cwd_to_slug
    cwd = resolve_cwd(args.cwd)
    sessions = scan_sessions(cwd, scope_all=True)  # detail 은 전체 검색
    sid = resolve_sid(args.sid, sessions)
    if not sid:
        print(f"❌ 세션 ID '{args.sid}' 모호하거나 없음", file=sys.stderr)
        return 2
    s = next((x for x in sessions if x.id == sid), None)
    if s is None:
        print(f"❌ 세션 not found: {sid}", file=sys.stderr)
        return 2
    out = session_to_dict(s)
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        for k, v in out.items():
            print(f"  {k:>16}: {v}")
    return 0


# ─── Subcommand: ancestry / parent-of (기존 main() 내 함수 재사용) ────────
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
        print(f"❌ 세션 ID '{args.sid}' 모호하거나 없음", file=sys.stderr)
        return 2
    name = args.name.strip()
    if not name:
        print("❌ 이름이 비어있음", file=sys.stderr)
        return 2
    with registry_lock():
        registry_set(sid, name=name)
    print(f"✅ rename {sid[:8]} → '{name}'")
    return 0


# ─── Subcommand: slim — 본문 슬림화 fork ──────────────────────────────
def cmd_slim(args) -> int:
    """🔻 슬림 fork — 모드별 화이트리스트로 라인 보존/스텁/제거.

    예: gccfork slim 4c03 --mode medium --name "테스트"
    """
    from gccfork import scan_sessions, slim_fork_session_with, registry_get
    from gccfork_settings import SLIM_MODE_ALIASES
    cwd = resolve_cwd(args.cwd)
    sessions = scan_sessions(cwd, scope_all=True)
    sid = resolve_sid(args.sid, sessions)
    if not sid:
        print(f"❌ 세션 ID '{args.sid}' 모호하거나 없음", file=sys.stderr)
        return 2
    src = next((x for x in sessions if x.id == sid), None)
    if src is None:
        print(f"❌ 세션 not found: {sid}", file=sys.stderr)
        return 2
    # 옛 키 (strict/balanced/loose) → 새 키 (strong/medium/weak) 마이그레이션
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
        print(f"🔻 슬림 fork ({args.mode})  {src.id[:8]} → {new_sess.id[:8]}  '{name}'")
        print(f"   원본 {src.size_bytes:,}B → 슬림 {new_sess.size_bytes:,}B  ({out['ratio_pct']}%)")
        if stats:
            print(f"   KEEP {stats.get('kept', 0)} / STUB {stats.get('stubbed', 0)} / DROP {stats.get('dropped', 0)}")
        print(f"   {new_sess.jsonl_path}")
    return 0


# ─── Subcommand: slim-inplace — 같은 sid 유지 in-place 슬림 ──────────────
def cmd_slim_inplace(args) -> int:
    """🔻 in-place 슬림 — 같은 sid 유지, 원본 jsonl 을 atomic 으로 덮음.

    fork 와 달리 새 jsonl 을 만들지 않고 registry 에도 등록하지 않음.
    같은 세션을 재실행 (resume) 했을 때 컨텍스트 토큰 절약 효과.
    backup=True (기본) 면 .bak.<ts>.jsonl 자동 생성.
    --keep-recent N 으로 마지막 N 라인 보호 (활성 세션 안전 가드).
    """
    # Rust 우선 위임 — Phase D (2026-05-06) 부터 anti-frag/dynamic-cap 도 Rust 직접 처리
    # 실패/비활성 시 None → Python fallback
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
        print(f"❌ 세션 ID '{args.sid}' 모호하거나 없음", file=sys.stderr)
        return 2
    src = next((x for x in sessions if x.id == sid), None)
    if src is None:
        print(f"❌ 세션 not found: {sid}", file=sys.stderr)
        return 2
    mode = SLIM_MODE_ALIASES.get(args.mode, args.mode)
    if mode not in {"strong", "medium", "weak"}:
        print(f"❌ unknown mode '{args.mode}' (strong/medium/weak)", file=sys.stderr)
        return 2

    # 단편화 방지 — settings 의 slim_default_anti_fragmentation pref 따름
    # /slim 명령에서 사용자가 별도 옵션 명시 안 하면 settings 사용
    from gccfork import pref_get
    anti_frag = bool(getattr(args, "anti_fragmentation", None)) \
        if hasattr(args, "anti_fragmentation") and args.anti_fragmentation is not None \
        else bool(pref_get("slim_default_anti_fragmentation", False))

    result = slim_fork_session_with(
        src,
        src.id,           # in_place 면 무시됨 (session.id 사용)
        "",               # custom_name 무관
        mode,
        in_place=True,
        backup=not args.no_backup,
        keep_recent_lines=args.keep_recent,
        keep_recent_turns=getattr(args, "keep_recent_turns", 0) or 0,
        dry_run=args.dry_run,
        anti_fragmentation=anti_frag,
    )
    # in_place 분기는 항상 dict 반환
    assert isinstance(result, dict), "in_place 분기는 dict 를 반환해야 함"

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        old_kb = result["old_size"] / 1024
        new_kb = result["new_size"] / 1024
        saved = 100 - result["ratio_pct"]
        tag = "🔍 dry-run" if args.dry_run else "🔻 in-place 슬림"
        print(f"{tag} ({result['mode']})  sid={result['sid'][:8]}")
        print(f"   대상       : {result['path']}")
        print(f"   마커 idx   : {result['marker_idx']}  "
              f"(마커 전 {result['pre_kept']:,} 라인 보존)")
        if result.get("recent_kept"):
            unit = "턴" if (getattr(args, "keep_recent_turns", 0) or 0) > 0 else "라인"
            n = (getattr(args, "keep_recent_turns", 0) or 0) or args.keep_recent
            print(f"   recent KEEP: 마지막 {n} {unit} 보호 "
                  f"({result['recent_kept']:,} 라인)")
        print(f"   verdict    : KEEP={result['kept']:,}  STUB={result['stubbed']:,}  "
              f"DROP={result['dropped']:,}  REBIND={result['rebinded']:,}")
        print(f"   부피       : {old_kb:,.1f} KB → {new_kb:,.1f} KB ({saved:.1f}% 절약)")
        if not args.dry_run and result.get("backup"):
            print(f"   백업       : {result['backup']}")
    return 0


# ─── Subcommand: live-sessions — 모든 활성 claude 인스턴스 보기 ──────────
# ─── Hot-reload 헬퍼 — 자기 PID/sid 감지 + inject 사이드카 write ────────
INJECT_DIR = Path.home() / ".claude" / "gccfork-inject-requests"
INJECT_STATUS_DIR = Path.home() / ".claude" / "gccfork-inject-status"


def find_self_claude_pid_and_sid() -> tuple[Optional[int], Optional[str]]:
    """현재 process 부모 체인에서 가장 가까운 claude 의 PID + sid 반환.

    PPID 따라가면서 /proc/<pid>/comm == "claude" 인 첫 PID 찾고,
    sessions/<PID>.json 에서 sessionId 추출.
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
    """sid → (claude_pid, shell_pid). sessions/<PID>.json 전수 스캔."""
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
    """VSCode bridge 가 sendText + curtain off 를 끝냈다는 status ack 대기."""
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
        # bridge 가 요청을 아직 pick 하지 못한 경우 다음 polling 에서 뒤늦게 실행되지 않도록 정리.
        for p in (out, tmp):
            try:
                p.unlink()
            except OSError:
                pass
        raise


def build_command_steps(command: str, *, preclear: bool = True) -> list[dict]:
    """slash command (`/clear`, `/resume <sid>` 등) inject step 시퀀스 빌더.

    기존 textarea 잔재가 있으면 `/clear` 앞에 붙어 slash command 가 user text 로
    들어간다. ESC 는 답변 abort 위험이 있고 Backspace 다발은 bracketed paste 에
    묻힐 수 있어 Ctrl+U(입력줄 삭제)를 먼저 보낸 뒤 command 를 실행한다.
    """
    if not preclear:
        return [{"text": command, "delayMs": 0, "addNewLine": True}]
    return [
        {"text": "\x15", "delayMs": 0, "addNewLine": False},  # Ctrl+U: clear input line
        {"text": command, "delayMs": 50, "addNewLine": True},
    ]


def _split_control_sequence(sequence: str) -> list[dict]:
    """VSCode sendText 로 chord 키를 개별 keypress 처럼 전달한다."""
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
    """Claude keybinding 경로로 /clear, /resume sid 를 타이핑 없이 실행하게 한다.

    실행부는 validation 경고와 별개로 action 문자열을 그대로 `/${action[8:]}`
    로 넘긴다. 따라서 `command:resume <sid>`가 실제 `/resume <sid>`가 된다.
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
    """--self / --sid 분기 → (claude_pid, shell_pid, sid)."""
    trace_sar("resolve target start")
    # decorator 가 미리 호출했으면 같은 결과 재사용 (print 중복 방지)
    cached = getattr(args, "_resolved_cache", None)
    if cached is not None:
        trace_sar("resolve target from cache")
        return cached
    if getattr(args, "self_target", False):
        cpid, sid = find_self_claude_pid_and_sid()
        if not cpid or not sid:
            print("❌ --self: 부모 체인에서 claude 못 찾음 (claude 안 Bash 또는 hook 안에서만 동작)",
                  file=sys.stderr)
            return None, None, None
        spid = get_ppid(cpid)
        return cpid, spid, sid
    if getattr(args, "sid", None):
        # sid prefix 도 허용 — sessions 스캔으로 풀 sid 복원
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
            print(f"❌ 세션 ID '{args.sid}' 모호하거나 없음", file=sys.stderr)
            return None, None, None
        cpid, spid = find_claude_for_sid(full)
        if not cpid or not spid:
            print(f"❌ sid {full[:8]} 활성 claude 인스턴스 없음 (sessions/<PID>.json 부재)",
                  file=sys.stderr)
            return None, None, None
        trace_sar(f"resolve target done sid={full[:8]} claude_pid={cpid} shell_pid={spid}")
        return cpid, spid, full
    print("❌ --self 또는 --sid 중 하나 필요", file=sys.stderr)
    return None, None, None


# ─── Subcommand: hot-reload — /clear + /resume <sid> 만 ──────────────────
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
    """Bridge 트랜잭션으로 slash command 주입.

    curtain 은 CLI 전체가 아니라 bridge 의 sendText 실제 처리 구간에만 걸고,
    ALT_OFF 전에는 ~/.claude/sessions/<pid>.json 의 sid/status 변화를 관찰한다.
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
            print(f"⚠ {label} — Claude hidden-clear patch 상태:", file=sys.stderr)
            print(format_report(report), file=sys.stderr)
            if getattr(args, "require_hidden_clear", False):
                return 5
        else:
            try:
                trace_sar(f"{label}: claude patch check ok")
            except Exception:
                pass
    except Exception as exc:
        print(f"⚠ {label} — Claude hidden-clear patch 점검 실패: {exc}",
              file=sys.stderr)
        if getattr(args, "require_hidden_clear", False):
            return 5
    return 0


def cmd_hot_reload(args) -> int:
    """🚀 같은 PID hot-reload — /clear + /resume <sid> inject.

    같은 process 안에서 jsonl 다시 read = 메모리 갱신.
    `/clear` 가 SessionStart 마커 박고 `/resume` 이 마커 후만 read 하는 메커니즘.
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
            print(f"❌ inject {clear_slash} 실패: {exc}", file=sys.stderr)
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
            print(f"❌ inject {resume_slash} 실패: {exc}", file=sys.stderr)
            return 4
        print(f"  inject {resume_slash} {sid[:8]}  → {rid} (ack)", file=sys.stderr)

    if args.json:
        print(json.dumps({
            "sid": sid, "claude_pid": cpid, "shell_pid": spid,
            "cleared": not args.no_clear, "resumed": not args.no_resume,
        }, ensure_ascii=False))
    else:
        print("✓ hot-reload 완료", file=sys.stderr)
    return 0


def _repair_last_prompt_leaf(jsonl_path: Path) -> dict | None:
    """slim+reload 후 last-prompt.leafUuid 가 마지막 정상 user/assistant uuid 와
    어긋났을 때 강제 갱신. chord /clear → /resume 직후 claude TUI 가 마지막 user
    메시지에 leaf 를 박아 freeze 시키는 케이스를 잡는다. 갱신 없으면 None."""
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


# ─── Subcommand: slim-and-reload — slim + /clear + /resume 통합 ──────────
def cmd_slim_and_reload(args) -> int:
    """🚀 자동 슬림 + hot-reload — slim-inplace + /clear + /resume <sid> 통합.

    흐름:
      1. (initial-delay)  답변 끝까지 대기
      2. inject /clear     SessionStart 마커 박기
      3. (clear-wait)
      4. slim-inplace      jsonl atomic 슬림 + .bak
      5. (resume-wait)
      6. inject /resume    같은 PID 가 슬림된 jsonl reload
    """
    # Rust 우선 위임 — Phase D (2026-05-06) 부터 anti-frag/dynamic-cap 도 Rust 직접 처리
    # 실패/비활성 시 None → Python fallback
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
        print(f"❌ 세션 not found: {sid}", file=sys.stderr)
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

    # 🛡️ preflight — 7개 체크 병렬 실행. ok=False 면 spawn 거절.
    if not getattr(args, "no_preflight", False):
        from gccfork_preflight import run_preflight, spawn_lock_holder
        trace_sar("preflight start")
        pf = run_preflight(sid=sid, jsonl_path=src.jsonl_path, claude_pid=cpid)
        trace_sar("preflight done")
        print(f"  {pf.summary()}", file=sys.stderr)
        if not pf.ok:
            print(f"❌ preflight fail — slim 진입 거절 (--no-preflight 로 강제 가능)",
                  file=sys.stderr)
            return 3
        # spawn lock 점유 — 같은 sid 동시 spawn 가드. 이 함수 끝까지 유지.
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

    # ★ 1. 사전 백업 — /clear 가 jsonl 에 라인 추가하기 **전** 깨끗한 상태 보존.
    #    /clear 후 atomic rename 시 백업하면 race + 마커 포함된 더러운 백업이라
    #    복구 가치가 떨어짐. 사전 복사가 race-safe.
    pre_backup_path = None
    if not args.no_backup and not args.dry_run:
        import shutil
        ts = int(time.time())
        pre_backup_path = src.jsonl_path.parent / f"{src.jsonl_path.stem}.bak.{ts}.jsonl"
        try:
            trace_sar("pre-clear backup start")
            shutil.copy2(src.jsonl_path, pre_backup_path)
            trace_sar("pre-clear backup done")
            print(f"  ★ pre-clear 백업: {pre_backup_path.name} "
                  f"({pre_backup_path.stat().st_size:,} bytes)", file=sys.stderr)
        except OSError as exc:
            print(f"❌ 사전 백업 실패: {exc}", file=sys.stderr)
            if _spawn_lock is not None:
                try: _spawn_lock.__exit__(None, None, None)
                except Exception: pass
            return 4

    # 2. /clear inject — bridge 가 sendText + curtain + sid 변화 관찰까지
    #    동기 트랜잭션으로 처리한다. 더 이상 고정 sleep 으로 입력 echo 를 추측하지 않는다.
    preclear = not getattr(args, "no_preclear", False)
    cid = None
    clear_inject_time = None  # 자동 phantom 휴지통 이동 시 mtime 가드
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
            print(f"❌ inject {clear_slash} 실패: {exc}", file=sys.stderr)
            # inject /clear 가 timeout/실패해도 leaf repair 만은 시도 — chord 가
            # claude TUI 에 닿아 transcript 오염을 남겼다면 leaf freeze 도 발생했을
            # 가능성이 있으므로 화면 정상화를 위해 last-prompt 만 갱신.
            try:
                lr = _repair_last_prompt_leaf(src.jsonl_path)
                if lr:
                    print(f"  ⚓ last-prompt leaf 갱신 (inject 실패 후): "
                          f"{lr['old'][:8] if lr['old'] else '(none)'} → {lr['new'][:8]} "
                          f"({lr['orphans']} 매달림)", file=sys.stderr)
            except Exception as e2:
                print(f"  ⚠ leaf repair 실패 (무시): {e2}", file=sys.stderr)
            if _spawn_lock is not None:
                try: _spawn_lock.__exit__(None, None, None)
                except Exception: pass
            return 7
        print(f"  inject {clear_slash}  → {cid}{' (preclear)' if preclear else ''} (ack)", file=sys.stderr)

    import threading
    slim_result_holder: dict = {"result": None, "error": None}

    # 단편화 방지 옵션 — settings 의 slim_default_anti_fragmentation pref 사용
    from gccfork import pref_get as _pg
    anti_frag = bool(getattr(args, "anti_fragmentation", None)) \
        if hasattr(args, "anti_fragmentation") and args.anti_fragmentation is not None \
        else bool(_pg("slim_default_anti_fragmentation", False))

    def _slim_worker():
        try:
            slim_result_holder["result"] = slim_fork_session_with(
                src, src.id, "", mode,
                in_place=True,
                backup=False,           # 사전 백업했으므로 slim 단계 백업 안 만듦
                keep_recent_lines=args.keep_recent,
                keep_recent_turns=getattr(args, "keep_recent_turns", 0) or 0,
                dry_run=args.dry_run,
                defer_commit=True,      # atomic os.replace 미룸 — main 이 나중에 처리
                anti_fragmentation=anti_frag,
            )
        except Exception as exc:
            slim_result_holder["error"] = exc

    print(f"  slim-inplace 시작...", file=sys.stderr)
    trace_sar("slim worker start")
    slim_thread = threading.Thread(target=_slim_worker, name="slim-worker")
    slim_thread.start()

    # main thread — clear-wait sleep (background slim 진행 중)
    if not args.no_clear and args.clear_wait > 0:
        print(f"  sleep {args.clear_wait}s (clear-wait, slim 병렬)", file=sys.stderr)
        time.sleep(args.clear_wait)

    # background slim 끝까지 대기
    slim_thread.join()
    trace_sar("slim worker joined")
    if slim_result_holder["error"] is not None:
        print(f"❌ slim 실패: {slim_result_holder['error']}", file=sys.stderr)
        if _spawn_lock is not None:
            try: _spawn_lock.__exit__(None, None, None)
            except Exception: pass
        return 5
    result = slim_result_holder["result"]
    assert isinstance(result, dict), "defer_commit 분기는 dict 를 반환해야 함"

    # 3. atomic commit — slim 의 tmp_path 를 jsonl 위치로 os.replace.
    #    /clear 처리가 완료된 시점에 갈아치움 → /clear 마커가 옛 inode 에 박혔다면
    #    사라지지만, /resume 이 새 jsonl 의 마지막 SessionStart 마커 (이전 turn 의)
    #    이후를 read 하므로 작동.
    if not args.dry_run and result.get("tmp_path") and result.get("new_path"):
        try:
            trace_sar("atomic commit start")
            os.replace(result["tmp_path"], result["new_path"])
            result["committed"] = True
            trace_sar("atomic commit done")
        except Exception as exc:
            print(f"❌ atomic commit 실패: {exc}", file=sys.stderr)
            try:
                Path(result["tmp_path"]).unlink()
            except Exception:
                pass
            if _spawn_lock is not None:
                try: _spawn_lock.__exit__(None, None, None)
                except Exception: pass
            return 6

    # 결과 dict 에 사전 백업 경로 주입 (--json 출력에서도 참조 가능하도록)
    if pre_backup_path is not None:
        result["backup"] = str(pre_backup_path)

    if not args.dry_run:
        old_kb = result["old_size"] / 1024
        new_kb = result["new_size"] / 1024
        saved = 100 - result["ratio_pct"]
        print(f"  slim 완료: {old_kb:,.1f}KB → {new_kb:,.1f}KB ({saved:.1f}% 절약)",
              file=sys.stderr)
        print(f"    KEEP={result['kept']:,}  STUB={result['stubbed']:,}  "
              f"DROP={result['dropped']:,}  REBIND={result['rebinded']:,}",
              file=sys.stderr)
        if result.get("backup"):
            print(f"    backup: {result['backup']}", file=sys.stderr)

    # 3. resume-wait
    if args.resume_wait > 0 and not args.no_resume:
        time.sleep(args.resume_wait)

    # 4. /resume inject (preclear 시퀀스로 race 회피)
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
            print(f"❌ inject {resume_slash} 실패: {exc}", file=sys.stderr)
            if _spawn_lock is not None:
                try: _spawn_lock.__exit__(None, None, None)
                except Exception: pass
            return 8
        print(f"  inject {resume_slash} {sid[:8]}  → {rid} (ack)", file=sys.stderr)

    # 5. 🗑 phantom 자동 휴지통 이동 — /clear 부산물 (10라인 빈 jsonl) 식별 + trash.
    #    패턴 deterministic: clear_inject_time 이후 새로 생긴 jsonl + 라인 ≤ 10 +
    #    type=assistant 0개 + 이번 sid 아님. 영구 삭제 X = gccfork 휴지통 이동만.
    #    /resume inject 후 추가 wait 불필요 — phantom jsonl 은 /clear 시점에 한 번
    #    박히고 그 후 write 안 됨 (claude 가 옛 sid 로 conversation switch). race 없음.
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
                        continue  # 이번 실행이 만든 게 아님
                    if jsonl.stat().st_size > 500_000:
                        continue  # 너무 크면 진짜 conversation 가능성
                    line_count = 0
                    has_assistant = False
                    with jsonl.open() as f:
                        for line in f:
                            line_count += 1
                            if line_count > 30:
                                break  # 안전 가드 — 진짜 conversation
                            try:
                                if json.loads(line).get("type") == "assistant":
                                    has_assistant = True
                                    break
                            except (json.JSONDecodeError, ValueError):
                                pass
                    if line_count > 30 or has_assistant:
                        continue  # phantom 아님
                    # 패턴 일치 → 휴지통 이동
                    if move_session_to_trash(jsonl.stem, jsonl):
                        phantoms_trashed.append(jsonl.stem)
                        print(f"  🗑 phantom 휴지통: {jsonl.stem[:8]} "
                              f"({line_count} 라인, assistant=0)", file=sys.stderr)
                except OSError:
                    continue
            trace_sar(f"phantom trash scan done trashed={len(phantoms_trashed)}")
        except Exception as exc:
            print(f"  ⚠ phantom trash 실패 (무시): {exc}", file=sys.stderr)

    # 6. ⚓ last-prompt.leafUuid 자동 갱신 — slim+reload 후 freeze 된 leaf 가
    #    마지막 정상 응답을 못 가리켜 화면이 "이전 대화" 로 돌아간 것처럼 보이는
    #    현상을 잡는다. 마지막 user/assistant uuid 로 leaf 를 강제 갱신.
    leaf_repair: dict | None = None
    if not args.dry_run:
        try:
            trace_sar("leaf repair start")
            leaf_repair = _repair_last_prompt_leaf(src.jsonl_path)
            if leaf_repair:
                print(f"  ⚓ last-prompt leaf 갱신: "
                      f"{leaf_repair['old'][:8] if leaf_repair['old'] else '(none)'}"
                      f" → {leaf_repair['new'][:8]}"
                      f" ({leaf_repair['orphans']} 매달림 메시지)", file=sys.stderr)
            trace_sar(f"leaf repair done updated={leaf_repair is not None}")
        except Exception as exc:
            print(f"  ⚠ leaf repair 실패 (무시): {exc}", file=sys.stderr)

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
        print("✓ slim-and-reload 완료" + (" (dry-run)" if args.dry_run else ""),
              file=sys.stderr)

    # spawn lock 해제 — preflight 가 점유했던 것
    if _spawn_lock is not None:
        try:
            _spawn_lock.__exit__(None, None, None)
        except Exception:
            pass
    return 0


def cmd_live_sessions(args) -> int:
    """모든 활성 claude 인스턴스의 PID/sid/cwd/status 한 눈에 보기.

    sessions/<PID>.json 의 truth source 그대로 표시 (registry 영향 X).
    멀티 세션 환경에서 어느 sid 가 진짜 활성인지 즉시 확인 가능.
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
        print("활성 claude 인스턴스 없음.")
        return 0
    print(f"활성 claude 인스턴스 {len(sessions)}개"
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
    """Claude Code /clear transcript 숨김 패치 상태 확인/자동 적용."""
    # Rust 우선 위임 — 실패/비활성 시 None → Python fallback
    from gccfork_rust_dispatch import try_rust_patch_claude
    rc = try_rust_patch_claude(args)
    if rc is not None:
        return rc

    try:
        from gccfork_claude_patch import check_and_patch, format_report
    except Exception as exc:
        print(f"❌ patch helper import 실패: {exc}", file=sys.stderr)
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


# ─── Subcommand: reconcile-live — registry ↔ sessions/<PID>.json 동기화 ──
def cmd_reconcile_live(args) -> int:
    """sessions/<PID>.json (truth) 기반으로 registry 동기화.

    --dry-run 으로 차이만 보고. --apply 로 실제 갱신.
    """
    from gccfork import reconcile_registry_from_live_sessions
    apply = args.apply and not args.dry_run
    result = reconcile_registry_from_live_sessions(apply=apply)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0

    tag = "🔧 적용" if result["applied"] else "🔍 dry-run"
    print(f"{tag}  활성 세션 {result['live_count']}개 검사")
    print(f"   신규 등록: {len(result['new'])}  갱신: {len(result['updated'])}  변화 없음: {result['unchanged_count']}")

    if result["new"]:
        print("\n--- 신규 등록 후보 ---")
        for item in result["new"]:
            print(f"  + {item['sid'][:8]}: {item['changes']}")

    if result["updated"]:
        print("\n--- 갱신 후보 ---")
        for item in result["updated"]:
            print(f"  ~ {item['sid'][:8]}")
            if item['old_name'] != item['live_name']:
                print(f"      old name : {item['old_name'][:70]}")
                print(f"      live name: {item['live_name'][:70]}")
            for k, v in item["changes"].items():
                if k == "name":
                    continue   # 위에서 표시
                print(f"      {k}: {v}")

    if not result["applied"]:
        print("\n실제 적용은 --apply 추가:  gccfork reconcile-live --apply")
    return 0


# ─── Subcommand: hard-fork ──────────────────────────────────────────────
def cmd_hard_fork(args) -> int:
    from gccfork import scan_sessions, hard_fork_session_with
    cwd = resolve_cwd(args.cwd)
    sessions = scan_sessions(cwd, scope_all=True)
    sid = resolve_sid(args.sid, sessions)
    if not sid:
        print(f"❌ 세션 ID '{args.sid}' 모호하거나 없음", file=sys.stderr)
        return 2
    src = next((x for x in sessions if x.id == sid), None)
    if src is None:
        print(f"❌ 세션 not found: {sid}", file=sys.stderr)
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


# ─── Subcommand: delete (휴지통 이동) ────────────────────────────────────
def cmd_delete(args) -> int:
    from gccfork import scan_sessions, registry_remove, TRASH_DIR
    cwd = resolve_cwd(args.cwd)
    sessions = scan_sessions(cwd, scope_all=True)
    sid = resolve_sid(args.sid, sessions)
    if not sid:
        print(f"❌ 세션 ID '{args.sid}' 모호하거나 없음", file=sys.stderr)
        return 2
    s = next((x for x in sessions if x.id == sid), None)
    if s is None:
        print(f"❌ 세션 not found: {sid}", file=sys.stderr)
        return 2

    if not args.force:
        print(f"⚠ 휴지통 이동 확인 필요 — '{s.id[:8]}' ({s.title or '(empty)'})")
        print(f"   --force 플래그 추가하면 즉시 실행")
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
        # JSON 리터럴 시도 (true/false/숫자/문자열) 후 fallback to str
        try:
            value = json.loads(args.value)
        except (json.JSONDecodeError, TypeError):
            value = args.value
        with registry_lock():  # prefs 도 같은 락으로 보호
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
        print(f"   세션: {out['session_count']:,}")
        print(f"   누적 크기: {total_size / 1024 / 1024:.1f} MB")
        print(f"   누적 턴: {total_turns:,}")
        print(f"   압축된 세션: {compacted}")
        print(f"   fork: hard={forks['hard']}, soft={forks['soft']}, auto={forks['auto']}, root={forks['root']}")
    return 0


# ─── Dispatcher ─────────────────────────────────────────────────────────
SUBCOMMANDS = {"search", "list", "detail", "ancestry", "parent-of",
               "rename", "hard-fork", "slim", "slim-inplace", "delete", "prefs", "stats",
               "live-sessions", "reconcile-live",
               "hot-reload", "slim-and-reload", "patch-claude", "rust-status"}


def is_subcommand(argv: list[str]) -> bool:
    """sys.argv[1:] 첫 인자가 subcommand 인지."""
    return bool(argv) and argv[0] in SUBCOMMANDS


def dispatch(argv: list[str]) -> int:
    """gccfork.py main() 가 sys.argv[1] 이 subcommand 면 호출."""
    trace_sar(f"dispatch entered argv={' '.join(argv)}")
    ap = argparse.ArgumentParser(prog="gccfork", description="gccfork headless CLI")
    sp = ap.add_subparsers(dest="cmd", required=True)

    # 공통 — cwd / all / json 인자 빌더
    def add_scope(p):
        p.add_argument("--cwd", default=None, help="cwd override")
        p.add_argument("--all", action="store_true", help="전체 프로젝트 스코프")
        p.add_argument("--json", action="store_true", help="JSON 출력")

    # search
    p_search = sp.add_parser("search", help="본문 전체 스캔 (5개 노이즈 필터 적용)")
    p_search.add_argument("query", help="검색어")
    p_search.add_argument("--no-filter", action="store_true",
                          help="5개 노이즈 필터 비활성 (옛 동작)")
    p_search.add_argument("--fuzzy", action="store_true",
                          help="fuzzy 매처 강제 활성 (prefs 와 무관)")
    p_search.add_argument("--no-fuzzy", action="store_true",
                          help="fuzzy 매처 강제 비활성 (prefs/--fuzzy 보다 우선)")
    add_scope(p_search)
    p_search.set_defaults(func=cmd_search)

    # list
    p_list = sp.add_parser("list", help="세션 목록")
    add_scope(p_list)
    p_list.set_defaults(func=cmd_list)

    # detail
    p_detail = sp.add_parser("detail", help="세션 상세")
    p_detail.add_argument("sid", help="세션 ID (앞 4자 prefix 가능)")
    p_detail.add_argument("--cwd", default=None)
    p_detail.add_argument("--json", action="store_true")
    p_detail.set_defaults(func=cmd_detail)

    # ancestry / parent-of
    p_anc = sp.add_parser("ancestry", help="조상 체인")
    p_anc.add_argument("sid")
    p_anc.add_argument("--json", action="store_true")
    p_anc.set_defaults(func=cmd_ancestry)
    p_par = sp.add_parser("parent-of", help="직계 부모")
    p_par.add_argument("sid")
    p_par.add_argument("--json", action="store_true")
    p_par.set_defaults(func=cmd_parent_of)

    # rename
    p_rename = sp.add_parser("rename", help="세션 이름 변경 (registry)")
    p_rename.add_argument("sid")
    p_rename.add_argument("name")
    p_rename.add_argument("--cwd", default=None)
    p_rename.set_defaults(func=cmd_rename)

    # hard-fork
    p_hf = sp.add_parser("hard-fork", help="하드 분기 (jsonl 복제 + 새 UUID)")
    p_hf.add_argument("sid")
    p_hf.add_argument("--name", default=None)
    p_hf.add_argument("--cwd", default=None)
    p_hf.add_argument("--json", action="store_true")
    p_hf.set_defaults(func=cmd_hard_fork)

    # slim — 본문 슬림화 fork
    p_slim = sp.add_parser("slim", help="🔻 슬림 fork (라인 화이트리스트로 슬림화)")
    p_slim.add_argument("sid")
    p_slim.add_argument("--mode",
                        choices=["strong", "medium", "weak", "strict", "balanced", "loose"],
                        default="strong",
                        help="strong=가장작음 / medium=흐름가독성 / weak=tool_result 도 보존 (옛 strict/balanced/loose 도 허용)")
    p_slim.add_argument("--name", default=None)
    p_slim.add_argument("--cwd", default=None)
    p_slim.add_argument("--json", action="store_true")
    p_slim.set_defaults(func=cmd_slim)

    # slim-inplace — 같은 sid 유지 in-place 슬림
    p_si = sp.add_parser("slim-inplace",
                         help="🔻 in-place 슬림 (같은 sid, 원본 jsonl 덮음)")
    p_si.add_argument("sid")
    p_si.add_argument("--mode",
                      default="strong",
                      choices=["strong", "medium", "weak", "strict", "balanced", "loose"],
                      help="슬림 강도 (default: strong)")
    p_si.add_argument("--keep-recent", type=int, default=50,
                      help="마지막 N 라인 무조건 보존 (default: 50; --keep-recent-turns 미지정시만 사용)")
    p_si.add_argument("--keep-recent-turns", type=int, default=0,
                      help="마지막 N 턴 (user 메시지 기준) 보존 — 라인보다 우선. 0=비활성")
    p_si.add_argument("--dry-run", action="store_true",
                      help="실제 변경 없이 통계만 표시")
    p_si.add_argument("--no-backup", action="store_true",
                      help="백업 .bak.<ts>.jsonl 생성 안 함")
    p_si.add_argument("--cwd", default=None)
    p_si.add_argument("--json", action="store_true")
    p_si.set_defaults(func=cmd_slim_inplace)

    # live-sessions — 모든 활성 claude 인스턴스 보기
    p_ls = sp.add_parser("live-sessions",
                         help="모든 활성 claude 인스턴스 (sessions/<PID>.json truth) 보기")
    p_ls.add_argument("--cwd", default=None,
                      help="특정 cwd 만 필터")
    p_ls.add_argument("--json", action="store_true")
    p_ls.set_defaults(func=cmd_live_sessions)

    # reconcile-live — registry 를 live sessions 기준으로 동기화
    p_rl = sp.add_parser("reconcile-live",
                         help="registry 를 sessions/<PID>.json (truth) 기반으로 동기화")
    p_rl.add_argument("--apply", action="store_true",
                      help="실제 갱신 (기본은 dry-run)")
    p_rl.add_argument("--dry-run", action="store_true",
                      help="강제 dry-run (--apply 무시)")
    p_rl.add_argument("--json", action="store_true")
    p_rl.set_defaults(func=cmd_reconcile_live)

    # delete
    p_del = sp.add_parser("delete", help="휴지통 이동")
    p_del.add_argument("sid")
    p_del.add_argument("--force", action="store_true")
    p_del.add_argument("--cwd", default=None)
    p_del.set_defaults(func=cmd_delete)

    # prefs
    p_pref = sp.add_parser("prefs", help="설정 get/set")
    p_pref_sub = p_pref.add_subparsers(dest="action", required=True)
    p_pref_get = p_pref_sub.add_parser("get")
    p_pref_get.add_argument("key", nargs="?", default=None)
    p_pref_get.add_argument("--json", action="store_true")
    p_pref_get.set_defaults(func=cmd_prefs)
    p_pref_set = p_pref_sub.add_parser("set")
    p_pref_set.add_argument("key")
    p_pref_set.add_argument("value", help="JSON 리터럴 또는 문자열")
    p_pref_set.add_argument("--json", action="store_true")
    p_pref_set.set_defaults(func=cmd_prefs)

    # stats
    p_stats = sp.add_parser("stats", help="스코프 통계")
    add_scope(p_stats)
    p_stats.set_defaults(func=cmd_stats)

    # patch-claude — Claude Code 업데이트 감지 + hidden /clear patch
    p_patch = sp.add_parser(
        "patch-claude",
        help="Claude Code /clear transcript 숨김 패치 상태 확인/자동 적용",
    )
    p_patch.add_argument("--auto", action="store_true",
                         help="미패치 상태면 백업 후 자동 패치")
    p_patch.add_argument("--binary", default=None,
                         help="대상 Claude version binary 경로 (기본: 최신 ~/.local/share/claude/versions/*)")
    p_patch.add_argument("--force", action="store_true",
                         help="예상 문자열 개수가 달라도 강제 치환 (위험)")
    p_patch.add_argument("--strict", action="store_true",
                         help="패치 누락 또는 Claude 재시작 필요 상태면 exit 1")
    p_patch.add_argument("--json", action="store_true")
    p_patch.set_defaults(func=cmd_patch_claude)

    # rust-status — Rust 위임 진단
    def cmd_rust_status(_args) -> int:
        from gccfork_rust_dispatch import status_summary
        print(status_summary())
        return 0
    p_rust = sp.add_parser("rust-status", help="Rust 바이너리 위임 상태 진단")
    p_rust.set_defaults(func=cmd_rust_status)

    # hot-reload — /clear + /resume <sid> 자동 inject (같은 PID hot-reload)
    def add_target(p):
        g = p.add_mutually_exclusive_group(required=True)
        g.add_argument("--self", dest="self_target", action="store_true",
                       help="부모 체인에서 claude 자동 감지 (claude 안 Bash / hook 안)")
        g.add_argument("--sid", default=None,
                       help="명시 sid (앞 4자 prefix 가능)")

    p_hr = sp.add_parser("hot-reload",
                         help="🚀 같은 PID hot-reload (/clear + /resume <sid>)")
    add_target(p_hr)
    p_hr.add_argument("--initial-delay", type=float, default=0.0,
                      help="시작 sleep — 답변 끝까지 대기 (default: 0)")
    p_hr.add_argument("--clear-wait", type=float, default=0.0,
                      help="/clear ack 후 추가 대기 (default: 0)")
    p_hr.add_argument("--no-clear", action="store_true",
                      help="/clear inject 생략")
    p_hr.add_argument("--no-resume", action="store_true",
                      help="/resume inject 생략")
    p_hr.add_argument("--no-preclear", action="store_true",
                      help="호환용 no-op: 제어문자 preclear 는 현재 비활성")
    p_hr.add_argument("--inject-mode", choices=["keybinding", "text"], default="keybinding",
                      help="slash command 실행 방식 (default: keybinding; text=기존 sendText)")
    p_hr.set_defaults(curtain=False)
    p_hr.add_argument("--curtain", dest="curtain", action="store_true",
                      help="실험용 TTY curtain 사용 (현재 VSCode/Claude TUI에서는 비추천)")
    p_hr.add_argument("--no-curtain", dest="curtain", action="store_false",
                      help="🛡️ TTY curtain (alt screen) 생략 — 디버그/log 용")
    # Pre-chord patch gate — fires every invocation (mid-session
    # claude auto-updates can leave the binary unpatched between calls).
    p_hr.add_argument("--no-auto-patch-claude", dest="auto_patch_claude",
                      action="store_false",
                      help="Claude Code hidden /clear 패치 자동 적용 생략")
    p_hr.add_argument("--skip-claude-patch-check", action="store_true",
                      help="Claude Code hidden /clear 패치 상태 점검 생략")
    p_hr.add_argument("--require-hidden-clear", action="store_true",
                      help="패치 누락/재시작 필요 시 hot-reload 중단")
    p_hr.add_argument("--cwd", default=None)
    p_hr.add_argument("--json", action="store_true")
    p_hr.set_defaults(func=cmd_hot_reload, auto_patch_claude=True)

    # slim-and-reload — slim-inplace + /clear + /resume 통합
    p_sar = sp.add_parser("slim-and-reload",
                          help="🚀 자동 슬림 + hot-reload (slim-inplace + /clear + /resume)")
    add_target(p_sar)
    p_sar.add_argument("--mode", default="strong",
                       choices=["strong", "medium", "weak", "strict", "balanced", "loose"],
                       help="슬림 강도 (default: strong)")
    p_sar.add_argument("--keep-recent", type=int, default=50,
                       help="마지막 N 라인 무조건 보존 (default: 50; --keep-recent-turns 미지정시만 사용)")
    p_sar.add_argument("--keep-recent-turns", type=int, default=0,
                       help="마지막 N 턴 (user 메시지 기준) 보존 — 라인보다 우선. 0=비활성")
    p_sar.add_argument("--initial-delay", type=float, default=0.0,
                       help="시작 sleep (default: 0; hook 사용 시 5 권장)")
    p_sar.add_argument("--clear-wait", type=float, default=0.0,
                       help="/clear ack 후 슬림 시작까지 추가 대기 (default: 0)")
    p_sar.add_argument("--resume-wait", type=float, default=0.0,
                       help="슬림 후 /resume 전 추가 대기 (default: 0)")
    p_sar.add_argument("--no-clear", action="store_true",
                       help="/clear inject 생략")
    p_sar.add_argument("--no-resume", action="store_true",
                       help="/resume inject 생략")
    p_sar.add_argument("--no-preclear", action="store_true",
                       help="호환용 no-op: 제어문자 preclear 는 현재 비활성")
    p_sar.add_argument("--inject-mode", choices=["keybinding", "text"], default="keybinding",
                       help="slash command 실행 방식 (default: keybinding; text=기존 sendText)")
    p_sar.add_argument("--no-phantom-trash", action="store_true",
                       help="🗑 /clear 부산물 (빈 phantom jsonl) 자동 휴지통 이동 생략")
    p_sar.add_argument("--no-preflight", action="store_true",
                       help="🛡️ preflight 사전 검증 7개 모두 skip (위험 — 디버그 용)")
    p_sar.add_argument("--no-auto-patch-claude", dest="auto_patch_claude", action="store_false",
                       help="Claude Code hidden /clear 패치 자동 적용 생략")
    p_sar.add_argument("--skip-claude-patch-check", action="store_true",
                       help="Claude Code hidden /clear 패치 상태 점검 생략")
    p_sar.add_argument("--require-hidden-clear", action="store_true",
                       help="패치 누락/재시작 필요 시 slim-and-reload 중단")
    p_sar.add_argument("--no-backup", action="store_true",
                       help="슬림 백업 .bak 생성 안 함")
    p_sar.set_defaults(curtain=False)
    p_sar.add_argument("--curtain", dest="curtain", action="store_true",
                       help="실험용 TTY curtain 사용 (현재 VSCode/Claude TUI에서는 비추천)")
    p_sar.add_argument("--no-curtain", dest="curtain", action="store_false",
                       help="🛡️ TTY curtain (alt screen) 생략 — 디버그/log 용")
    p_sar.add_argument("--dry-run", action="store_true",
                       help="실제 변경 없이 통계만")
    p_sar.add_argument("--cwd", default=None)
    p_sar.add_argument("--json", action="store_true")
    p_sar.set_defaults(func=cmd_slim_and_reload)

    trace_sar("argparse setup complete")
    args = ap.parse_args(argv)
    trace_sar(f"argparse parsed cmd={args.cmd}")
    return args.func(args)
