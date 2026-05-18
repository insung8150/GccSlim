"""gccfork 데이터 계층 — jsonl 파싱 / 세션 인덱스 / registry / 부모 추론.

메인 `gccfork` 와 사이드카 (cli, autoreload, search) 가 공유하는 코드.

핵심 최적화:
  - `parse_session` 결과는 (size, mtime) 키로 `_PARSE_CACHE` 에 캐시
    → 변경되지 않은 jsonl 은 stat 한 번 + dict lookup 으로 끝
  - `_PARSE_CACHE` 는 `~/.claude/gccfork-parse-cache.pickle` 에 영속화
    → process 가 새로 시작해도 이전 파싱 결과 재사용
    → mtime/size 검증으로 stale entry 자동 무효화
  - `load_registry` / `load_legacy_registry` 도 (mtime, data) 캐시
    → scan_sessions 의 N² registry read 가 1 read 로 (가장 큰 효과)

이 모듈은 메인 / 다른 사이드카를 import 하지 않는다 (단방향).
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


# ── 상수 ────────────────────────────────────────────────────────────────
CLAUDE_ROOT = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_ROOT / "projects"
SESSIONS_DIR = CLAUDE_ROOT / "sessions"
REGISTRY_PATH = CLAUDE_ROOT / "gccfork-registry.json"
CCFORK_LEGACY_REGISTRY_PATH = CLAUDE_ROOT / "ccfork-registry.json"


# 메시지 본문에 들어가지만 "사용자 발화"로 카운트하지 않는 prefix.
# 메인의 INTERNAL_USER_PREFIXES 와 동일한 값 (settings.py 도 같은 값을 갖고 있다).
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


# ── 작은 헬퍼 ───────────────────────────────────────────────────────────
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
    """Claude 세션 파일명은 `<uuid>.jsonl` — stem 전체가 session_id."""
    match = UUID_SUFFIX_RE.search(path.stem)
    return match.group(1) if match else None


def cwd_to_slug(cwd: str) -> str:
    """Claude Code 프로젝트 폴더 이름 생성 규칙.

    `/`, `_`, 비ASCII 문자를 모두 `-`로 치환.
    예: `/home/user/project`
        → `-home-yooha-JOB-FOLD----------MindVault`
    """
    out = []
    for ch in cwd:
        if ch == "/" or ch == "_" or ord(ch) > 127:
            out.append("-")
        else:
            out.append(ch)
    return "".join(out)


def slug_to_cwd_candidates(slug: str) -> list[str]:
    """역추론 불가 (정보 손실). jsonl `cwd` 필드 신뢰."""
    return []


# ── live sessions/<PID>.json ────────────────────────────────────────────
def read_live_sessions() -> list[dict]:
    """살아있는 모든 claude 인스턴스의 sessions/<PID>.json 읽기.

    각 dict 에는 sessions json 원본 필드 + `pid_alive: True` 만 있는 것 보장.
    죽은 PID 의 stale json 은 자동 제외.
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
    """주어진 PID 의 활성 sessions/<PID>.json — 없으면 None."""
    f = SESSIONS_DIR / f"{pid}.json"
    if not f.exists() or not Path(f"/proc/{pid}").exists():
        return None
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        return d if "sessionId" in d else None
    except (OSError, json.JSONDecodeError):
        return None


def find_live_pid_by_sid(sid: str) -> Optional[int]:
    """sid → 활성 claude PID (역방향). 멀티 인스턴스 시 첫 매치 (deprecated — 멀티 안 안전).

    멀티 안전 버전을 원하면 `find_live_pids_by_sid(sid)` 사용.
    """
    pids = find_live_pids_by_sid(sid)
    return pids[0] if pids else None


def find_live_pids_by_sid(sid: str) -> list[int]:
    """sid → 활성 claude PID 들 (멀티 안전).

    sessions/<PID>.json 이 truth source — 포크_ccfork.md 핵심 원칙.
    cmdline / mtime / fd / env 같은 간접 추측 절대 사용 X.

    같은 sid 가 여러 PID 에 떠있는 케이스 (드물지만 슬림+resume 직후 race)
    에도 모두 반환하므로 destructive 액션 (kill 등) 에서 안전.
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
    """모든 활성 sid → PID 들 매핑. read_live_sessions 1회 호출로.

    같은 sid 가 여러 PID 에 떠있어도 안전. 호출자가 sid → pids 로 lookup 가능.
    """
    out: dict[str, list[int]] = {}
    for d in read_live_sessions():
        sid = d.get("sessionId")
        pid = d.get("pid")
        if not isinstance(sid, str) or not isinstance(pid, int):
            continue
        out.setdefault(sid, []).append(pid)
    return out


def _parse_parent_sid_from_name(name: Optional[str]) -> Optional[str]:
    """name 의 `[<= XXXX]` 패턴에서 부모 sid 앞 4자리 추출."""
    if not name:
        return None
    m = _PARENT_4_RE.search(name)
    return m.group(1) if m else None


def _resolve_full_sid_from_prefix(prefix4: str, sessions_pool: list[dict]) -> Optional[str]:
    """4자리 prefix → 전체 sid. live sessions 풀에서 unique 매치만 반환."""
    matches = [d["sessionId"] for d in sessions_pool
               if d.get("sessionId", "").startswith(prefix4)]
    return matches[0] if len(matches) == 1 else None


def reconcile_registry_from_live_sessions(apply: bool = False) -> dict:
    """모든 활성 sessions/<PID>.json 을 truth 로 삼아 registry 동기화.

    동기화 규칙:
      1. registry 미등록 → 새 entry 생성 (name 등 sessions json 그대로)
      2. registry name 이 비어있고 live name 이 있음 → live name 으로 채움
      3. registry name != live name → live name 으로 update (sessions 우선)
      4. parent_id 누락 + live name 의 `[<= XXXX]` 패턴에서 추출 가능 → 부모 등록
      5. pid 필드 자동 update (registry 에 pid 필드 없으면 추가)

    apply=False 면 dry-run (변경 없이 차이만 보고).
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


# ── 메시지 본문 추출 ────────────────────────────────────────────────────
def _extract_text_from_message(message: dict | None) -> str:
    """Claude `message.content`은 str 또는 content block 리스트.

    - str: 그대로 사용 (짧은 한 줄 유저 입력에 자주 쓰임)
    - list: `type == "text"` 블록의 `text` 필드 연결
    - 다른 블록(tool_use / tool_result / thinking / image)은 요약 제목 없이 스킵
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


# ── 데이터 모델 ─────────────────────────────────────────────────────────
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
    important: bool = False  # 빨간 ★ 마크 — registry 에 영속화, 클릭으로 토글

    @property
    def title(self) -> str:
        return self.custom_name or self.auto_summary or "(empty)"

    @property
    def short_id(self) -> str:
        return self.id[:8]


# ── registry I/O ────────────────────────────────────────────────────────
# scan_sessions 가 N² 번 registry_get 을 하면서 매번 파일 read + json.loads 했음.
# (mtime, data) 키로 in-process 캐시 — 같은 process 안에서 변경 없으면 read 0회.
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
    """기존 ccfork-registry.json 읽기 (read-only)."""
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
    # 새 mtime 으로 캐시 갱신 — load_registry 가 즉시 hit
    try:
        _REGISTRY_CACHE = (REGISTRY_PATH.stat().st_mtime, data)
    except OSError:
        _REGISTRY_CACHE = None


def registry_set(session_id: str, **fields) -> None:
    """쓰기는 오직 gccfork-registry.json 에만 (legacy는 건드리지 않음).

    parse cache 의 그 sid entry 자동 무효화 — registry 변경 (name/important/etc)
    이 다음 reload 시 즉시 반영되도록. 무효화 안 하면 cache hit 으로 옛 Session
    객체 반환되어 변경 안 보이는 버그 (2026-05-06 발견).
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
    # parse cache 의 해당 sid entry 무효화
    try:
        for p in list(_PARSE_CACHE.keys()):
            cached = _PARSE_CACHE.get(p)
            if cached and len(cached) >= 3 and getattr(cached[2], "id", None) == session_id:
                invalidate_parse_cache(p)
                break
    except Exception:
        pass


def registry_get(session_id: str) -> dict:
    """gccfork 우선 + ccfork legacy fallback (merge)."""
    own = load_registry()["sessions"].get(session_id, {}) or {}
    legacy = load_legacy_registry()["sessions"].get(session_id, {}) or {}
    if not legacy:
        return own
    merged = dict(legacy)
    merged.update(own)
    return merged


def registry_remove(session_id: str) -> None:
    """gccfork-registry에서만 제거. legacy는 건드리지 않음."""
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


# ── 부모 추론 / 색상 ────────────────────────────────────────────────────
# scan_sessions 후처리에서 자동 추론된 parent를 등록하는 런타임 override.
# registry(영구)에 쓰지 않고 메모리에만 보관 — jsonl에서 재계산 가능하므로.
_RUNTIME_PARENT_OVERRIDE: dict[str, str] = {}


def _parent_for(session_id: str) -> Optional[str]:
    """registry(own + legacy fallback) 우선, 없으면 런타임 자동 추론 맵."""
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
# 사용자 지정 6-distinct 팔레트 — 1=🔴 2=🟢 3=🔵 4=🟨 5=🟣 6=🟠 순서.
_DISTINCT_6_INDICES = [6, 8, 9, 2, 10, 7]

# refresh_list 시점에 App 이 갱신하는 root_id → color_index 맵.
_ROOT_COLOR_MAP: dict[str, int] = {}


def _set_root_color_map(roots: list[str]) -> None:
    """루트 목록에 색 인덱스 영구 할당. **한 번 배정된 색은 절대 변경되지 않음.**

    - 1순위: registry 에 이미 색이 저장돼 있으면 그대로 사용
    - 2순위: 새 루트면 "다음 빈 슬롯" 배정 후 registry 에 영구 저장
        - 6-distinct 6슬롯 중 비어있는 가장 낮은 것
        - 6개 다 차면 12색 풀의 비어있는 가장 낮은 것
        - 12개 다 차면 sha256 폴백 (충돌 허용)
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
    "terminal.ansiYellow",         # 🟧 (orange → yellow 대체)
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
    """VSCode 터미널 패널 컬러 태그용 ThemeColor ID."""
    return _VSCODE_TERMINAL_COLORS[_color_index_for_session(session_id)]


# ── 캐시 + 파서 + 스캐너 ────────────────────────────────────────────────
# (path, size, mtime) 키로 parse 결과 캐시.
# value: (size, mtime, Session, frozenset[uuid]).
# 변경되지 않은 jsonl 은 stat 한 번 + dict lookup 으로 끝 → 풀파싱 회피.
_PARSE_CACHE: dict[Path, tuple[int, float, "Session", frozenset]] = {}

# 디스크 영속 캐시 — process 종료 후에도 살아남음.
# 새 process 시작 시 mtime/size 검증해서 살아있는 항목만 in-memory 로 복원.
_DISK_CACHE_FILE = CLAUDE_ROOT / "gccfork-parse-cache.pickle"
# 캐시 변경 여부 — atexit 시 변경 없으면 save skip (디스크 write 절약).
_PARSE_CACHE_DIRTY = False


def invalidate_parse_cache(path: Optional[Path] = None) -> None:
    """파싱 캐시 무효화. path 지정 시 해당 항목만, None 이면 전체."""
    global _PARSE_CACHE_DIRTY
    if path is None:
        if _PARSE_CACHE:
            _PARSE_CACHE_DIRTY = True
        _PARSE_CACHE.clear()
    else:
        if _PARSE_CACHE.pop(path, None) is not None:
            _PARSE_CACHE_DIRTY = True


def prune_parse_cache() -> int:
    """존재하지 않는 path 항목 정리 (멀티 삭제 후 stale 정리). 정리한 개수 반환."""
    global _PARSE_CACHE_DIRTY
    stale = [p for p in _PARSE_CACHE if not p.exists()]
    for p in stale:
        _PARSE_CACHE.pop(p, None)
    if stale:
        _PARSE_CACHE_DIRTY = True
    return len(stale)


def _load_disk_cache() -> int:
    """프로세스 시작 시 디스크 캐시 → in-memory 복원.

    각 entry 의 (size, mtime) 가 현재 파일과 일치해야 살림. 그 외는 모두 버림.
    pickle 파일이 없거나 깨졌으면 조용히 무시.

    Returns: 복원된 entry 개수.
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
    """프로세스 종료 시 디스크에 캐시 저장. atexit 으로 등록.

    `_PARSE_CACHE_DIRTY` 가 False 면 save skip (변경 없음 = 디스크 그대로).
    """
    if not _PARSE_CACHE_DIRTY:
        return
    try:
        _DISK_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        # 원자적 write — temp 파일에 쓴 뒤 rename.
        tmp = _DISK_CACHE_FILE.with_suffix(".pickle.tmp")
        with tmp.open("wb") as fh:
            pickle.dump(dict(_PARSE_CACHE), fh, protocol=pickle.HIGHEST_PROTOCOL)
        tmp.replace(_DISK_CACHE_FILE)
    except Exception:
        pass


# 모듈 import 시점에 디스크 캐시 로드 + atexit save 등록.
# 메인이 처음 `from gccfork_sessions import ...` 할 때 한 번 실행.
_load_disk_cache()
atexit.register(_save_disk_cache)


def parse_session(
    jsonl_path: Path,
    keep_edge: int = 3,
    uuid_sink: Optional[set] = None,
) -> Optional[Session]:
    """Claude Code 세션 jsonl 1개를 파싱해 `Session`을 반환.

    Claude 포맷의 각 줄은 self-contained JSON 이벤트:
      {
        "sessionId": "...", "type": "user"|"assistant"|"summary"|"system",
        "message": {"role": "...", "content": str | list[block]},
        "uuid": "...", "parentUuid": "...", "cwd": "...", "version": "...",
        "isSidechain": bool, "isMeta": bool, "isCompactSummary": bool,
        "timestamp": "..."
      }

    `uuid_sink`가 주어지면 이 세션의 모든 message uuid를 담아 반환.

    **캐시**: (size, mtime) 가 일치하면 풀파싱 생략 + cached uuid set 을
    `uuid_sink` 에 부어준다.
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
    live_turn_count = 0  # 마지막 isCompactSummary 이후 user 턴

    # 캐시용 — 항상 모든 line uuid 를 모은다 (uuid_sink 가 None 이어도).
    # 다음 호출이 uuid_sink 를 줄 수 있으니까.
    all_uuids: set[str] = set()

    try:
        with jsonl_path.open("r", encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line:
                    continue

                if '"isCompactSummary":true' in line or '"isCompactSummary": true' in line:
                    compact_count += 1
                    live_turn_count = 0  # 압축 직후부터 라이브 카운트 재시작

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
    """메타-only 빠른 파싱 — 첫 화면 표시용.

    - stat (size, mtime, sid 는 파일명에서)
    - **첫 5KB**: cwd, version, sessionId, first_parent_uuid 같은 헤더 필드
    - **마지막 ~10KB**: last user msg → auto_summary, live_turn 근사

    `turn_count = -1` 로 표시 (= 미백필 마커). UI 는 "…" 같은 placeholder 로 그림.
    `uuid_sink` / `compact_count` 미채움. 부모 자동 추론은 백필 후에야 정확.

    캐시에 hit 이 있으면 풀파싱 결과 그대로 반환 (cache 가 우선).
    캐시 miss 일 때만 메타-only 로 빠르게 채움. 캐시에 저장하지 않음
    (풀파싱 결과로 덮어쓰기 위해).
    """
    try:
        stat = jsonl_path.stat()
    except OSError:
        return None
    size = stat.st_size
    mtime_ts = stat.st_mtime

    # 캐시 hit 이면 풀파싱 결과 그대로 반환 (turn_count 등 정확).
    cached = _PARSE_CACHE.get(jsonl_path)
    if cached is not None and cached[0] == size and cached[1] == mtime_ts:
        return cached[2]

    mtime = datetime.fromtimestamp(mtime_ts)
    session_id = extract_session_id_from_path(jsonl_path)

    cwd: Optional[str] = None
    originator: Optional[str] = None
    first_parent_uuid: Optional[str] = None
    auto_summary: Optional[str] = None

    HEAD_BYTES = 5 * 1024     # 5KB head — 보통 첫 5라인이면 cwd/version 다 잡힘
    TAIL_BYTES = 16 * 1024    # 16KB tail — 마지막 user 메시지 1~2개 가져오기 위함

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
                # 첫 real user/assistant 라인의 parentUuid 기록
                typ = obj.get("type")
                if not first_real_uuid_seen and typ in {"user", "assistant"}:
                    if not (obj.get("isSidechain") or obj.get("isMeta")):
                        first_real_uuid_seen = True
                        parent_uuid = obj.get("parentUuid")
                        if isinstance(parent_uuid, str) and parent_uuid:
                            first_parent_uuid = parent_uuid

            # ── TAIL ────────────────────────────────────────────────────
            # seek 으로 EOF 부근 읽고 마지막 user 메시지 추출.
            if size > HEAD_BYTES:
                tail_start = max(HEAD_BYTES, size - TAIL_BYTES)
                fh.seek(tail_start)
                tail_bytes = fh.read()
            else:
                tail_bytes = head_bytes  # 전체가 head 안에 들어감
            tail_text = tail_bytes.decode("utf-8", errors="ignore")
            tail_lines = tail_text.splitlines()
            # 마지막 라인은 부분일 수 있으니 첫 라인도 부분일 수 있음 → 양끝 1개 버림
            if len(tail_lines) > 2:
                tail_lines = tail_lines[1:]
            # 뒤에서부터 user 메시지 찾기
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
        turn_count=-1,            # ← 미백필 마커
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
    """같은 session.id가 여러 파일에 있을 때 더 신뢰할 파일 우선순위.

    1. 파일명 suffix UUID와 실제 session.id가 일치하는 파일
    2. 더 최근 mtime
    3. 더 큰 파일
    """
    path_matches = int(extract_session_id_from_path(session.jsonl_path) == session.id)
    return (path_matches, session.mtime.timestamp(), session.size_bytes)


def _session_jsonl_paths(current_cwd: Optional[str], scope_all: bool) -> Iterator[Path]:
    """스캔 대상 jsonl 경로 이터레이터.

    - scope_all: 모든 프로젝트 폴더의 세션 (`projects/*/*.jsonl`)
    - scope_current: 현재 cwd의 슬러그 폴더만 (`projects/<slug>/*.jsonl`)

    `.bak.<timestamp>.jsonl` 백업 파일은 제외 — slim_fork_session_with
    (in_place=True, backup=True) 가 만드는 자동 백업은 본 리스트에 등장하면
    안 됨. 휴지통 이동 후에도 백업이 본 폴더에 남아 sid 가 부활하는 버그 방지.
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
    """Claude 세션 폴더를 스캔해 Session 리스트 반환.

    후처리로 `first_parent_uuid` ↔ 다른 세션의 message uuid set을 교차 매칭
    해서 `parent_id` / `fork_type`을 자동 채운다. registry에 이미 부모 정보가
    기록된 세션(하드 분기 등)은 건드리지 않음.
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

    # 부모 자동 추론 — registry(own + legacy)에 이미 parent_id가 있으면 존중.
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
    """메타-only 빠른 스캔 — 첫 화면 표시용.

    캐시 hit 인 jsonl 은 풀파싱 결과 그대로 (정확). cache miss 인 것만
    `parse_session_meta_only` 로 메타만 채움. 부모 자동 추론은 registry
    parent_id 기반 1차만 — uuid_to_sessions 인덱스 없음 (백필 후 재실행).

    백필 worker 가 끝난 뒤 `scan_sessions` 를 다시 호출해서 정확한 트리로 재배치.
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

    # 1단계 부모 추론만: registry 에 명시된 parent_id 사용.
    _RUNTIME_PARENT_OVERRIDE.clear()
    for session in deduped.values():
        if session.parent_id:
            _RUNTIME_PARENT_OVERRIDE[session.id] = session.parent_id

    out = list(deduped.values())
    out.sort(key=lambda item: item.mtime, reverse=True)
    return out


def reinfer_parents_from_cache(sessions: list[Session]) -> None:
    """모든 세션이 풀파싱 끝난 뒤 호출 — 캐시의 uuid set 으로 부모 자동 추론.

    `_PARSE_CACHE` 에 저장된 uuid frozenset 을 읽어 uuid_to_sessions 인덱스를
    만들고, parent_id 가 없는 세션의 first_parent_uuid 와 매칭. 결과는
    `session.parent_id` + `_RUNTIME_PARENT_OVERRIDE` 에 반영.
    """
    by_id: dict[str, Session] = {s.id: s for s in sessions}

    # uuid → session id list (cache 의 frozenset 활용)
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
    # 활성 jsonl 에서 못 찾으면 archive 폴더도 검색 (lazy import — cycle 방지).
    try:
        from gccfork_archive import find_archived_session
    except ImportError:
        return None
    archive_path = find_archived_session(session_id)
    if archive_path is None:
        return None
    return parse_session(archive_path)
