"""gccfork_archive.py — 자식 세션을 부모 아래로 archive (병합) 모듈.

D안 채택: jsonl 을 archive 폴더로 이동 + registry 갱신 + sid lookup redirect.
외부 .md 파일에 박힌 자식 sid 참조가 dead link 가 되지 않도록 jsonl 자체는 보존.

이 모듈은 사이드카 — 메인 `gccfork` 의 mono 비대화 정책에 따라 분리됨.

## registry 새 필드 명세

자식 세션 entry 에 다음 4 필드가 추가됨:

```json
{
  "sessions": {
    "<자식_sid>": {
      "name": "...",
      "archived": true,
      "archived_into": "<부모_sid>",
      "archive_path": "/home/.../<P>/archive/<자식_sid>.jsonl",
      "archived_at": "2026-05-01T03:36:00.000Z"
    }
  }
}
```

`archived = false` 또는 키 없음 = 정상 (활성) 세션. `archived = true` = archive 됨.
`archived_into` = 자식의 직계 부모 sid (재귀 archive 시 손자도 자기 직계 부모를 가리킴).

## 옵션 (prefs `archive.*`)

10개 옵션 — `ARCHIVE_DEFAULTS` 참고.
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
    """활성 Claude 세션을 archive 하려 시도할 때 발생.

    archive_session 의 이중 안전 가드 (merge 도 이걸 거치므로 §1 의 보강).
    """
    def __init__(self, sid: str):
        self.sid = sid
        super().__init__(
            f"활성 Claude 세션 {sid[:8]} 은 archive 할 수 없음. "
            f"먼저 /quit 으로 종료 후 다시 시도하세요."
        )

# Textual UI imports — 사이드카 내부 Screen + Mixin 용.
# gccfork 가 PEP 723 venv 안에서 실행되므로 textual 항상 가능.
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static


# ── 옵션 기본값 ──────────────────────────────────────────────────────────
# 10개 옵션 — prefs 키는 모두 `archive_` 접두사 (textual widget id 호환 위해 underscore).
# `pref_get(key, default)` 로 조회.
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

# central 레이아웃에서 쓰일 archive 루트.
CENTRAL_ARCHIVE_ROOT = Path.home() / ".claude" / "gccfork-archive"


def get_archive_pref(key: str):
    """archive.* prefs 조회 — 누락 시 ARCHIVE_DEFAULTS 폴백.

    `pref_get("archive_preview_mode")` 처럼 직접 부르지 말고 이 헬퍼 통해.
    그래야 default 누락 케이스에서도 안전.
    """
    if key not in ARCHIVE_DEFAULTS:
        # 미등록 키 — 호출자 버그 방지 위해 KeyError 대신 None 반환
        return pref_get(key, None)
    return pref_get(key, ARCHIVE_DEFAULTS[key])


# ── archive 폴더 위치 ────────────────────────────────────────────────────
def _archive_dir(jsonl_path: Path, layout: Optional[str] = None) -> Path:
    """jsonl 파일이 옮겨질 archive 폴더 위치를 layout 옵션 기준으로 반환.

    - per_project (기본): jsonl 이 있는 프로젝트 폴더 안의 `archive/` 서브폴더
      예: `~/.claude/projects/<P>/archive/`
    - central: 통합 archive 루트 + 프로젝트별 서브폴더 (프로젝트명을 sanitize)
      예: `~/.claude/gccfork-archive/<P>/`
    """
    if layout is None:
        layout = str(get_archive_pref("archive_folder_layout"))
    project_dir = jsonl_path.parent
    if layout == "central":
        # 프로젝트 폴더 이름 그대로 사용 (예: -home-yooha-...-MindVault)
        return CENTRAL_ARCHIVE_ROOT / project_dir.name
    # default: per_project
    return project_dir / "archive"


# ── archive 메타 ────────────────────────────────────────────────────────
@dataclass
class ArchivedChildMeta:
    """preview 렌더링이 사용하는 archive 자식 메타.

    `archived_children_for(parent_sid)` 가 반환. jsonl 자체는 lazy load —
    `path` 만 넘기고 본문은 필요할 때 별도 함수로 읽음.
    """
    sid: str
    short_id: str
    path: Path                # archive 안의 jsonl 절대 경로
    name: Optional[str]       # custom_name 또는 None
    auto_summary: Optional[str]
    archived_at: str          # iso8601
    parent_sid: str
    size_bytes: int = 0
    turn_count: int = 0       # registry 에 저장돼있으면 사용, 없으면 -1
    fork_type: Optional[str] = None  # hard / slim / soft / auto


# ── 후손 재귀 수집 ──────────────────────────────────────────────────────
def collect_subtree(
    root_sids: Iterable[str],
    all_sessions: list[Session],
) -> list[Session]:
    """root_sids 의 모든 후손(자식·손자·…) 을 dedup 해서 반환.

    BFS — root_sids 는 결과에 **포함하지 않음**. 사용자가 "선택한 노드 자체는
    리스트에 남기고 후손만 archive" 패턴이라.

    루트도 함께 archive 시키려면 호출자가 직접 결과에 추가하거나, root_sids 를
    부모로 두고 자식부터 시작하면 됨.

    Cycle 방어: 방문 set 으로 무한 루프 방지.
    """
    result: list[Session] = []
    seen: set[str] = set(root_sids)  # root 자체는 결과 제외 + 재방문 방지

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


# ── jsonl 이동 + registry 갱신 (atomic) ──────────────────────────────────
def archive_session(session: Session, parent_sid: str) -> bool:
    """session 의 jsonl 을 archive 폴더로 이동 + registry 4 필드 추가.

    원자성: 이동 → registry 쓰기 순서. 이동은 OS rename 으로 atomic.
    registry 쓰기 실패 시 jsonl 을 원위치로 되돌리는 롤백 시도.

    재호출 안전: 이미 archived 인 세션은 (no-op) True 반환.

    안전 가드 §2: 활성 Claude 세션은 archive 거부 (ActiveSessionArchiveError).
    Claude 프로세스가 사라진 jsonl 에 계속 쓰려다 stub 을 만들어
    registry 메타가 손상되는 사고 (2026-05-04) 예방. 단 이미 archived 면 통과.
    """
    src = session.jsonl_path
    if not src.exists():
        return False

    reg_entry = registry_get(session.id)
    if reg_entry.get("archived"):
        # 이미 archive 된 세션 — no-op
        return True

    # 안전 가드 §2 — 활성 sid 차단 (이중 가드, merge §1 외에도 직접 archive 호출 차단)
    if session.id in all_active_sid_pid_map():
        raise ActiveSessionArchiveError(session.id)

    archive_dir = _archive_dir(src)
    archive_dir.mkdir(parents=True, exist_ok=True)
    dst = archive_dir / src.name

    # 같은 이름이 이미 있으면 timestamp suffix
    if dst.exists():
        ts = int(time.time())
        dst = archive_dir / f"{src.stem}.archived-{ts}{src.suffix}"

    try:
        # OS rename — 같은 파일시스템이면 atomic. 다르면 shutil.move 가 copy+delete.
        shutil.move(str(src), str(dst))
    except OSError:
        return False

    # registry 갱신. 실패 시 jsonl 원위치 롤백.
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


# ── 복원 (휴지통 패턴) ──────────────────────────────────────────────────
def restore_session(sid: str) -> bool:
    """archive 된 세션을 원위치로 복원 + registry 의 4 필드 제거.

    복원은 휴지통 패턴 (registry 에 archived=false 로 두지 않고 키 자체 제거).
    `pref_get("archive_restore_enabled") == "permanent"` 면 호출자가 미리
    거부해야 함 — 이 함수는 항상 작동.

    안전 가드 §3 fallback: registry entry 가 손상되어 archived 플래그가
    없어도, archive 폴더 직접 스캔으로 sid 의 jsonl 을 찾아 복원 시도.
    안전 가드 §4+5: dst 충돌 시 stub (lines<100) 이면 .stub.bak 으로 자동
    백업 후 archive 본문 우선 복원. 일반 충돌만 .restored-<ts> 로 회피.
    """
    entry = registry_get(sid)
    archive_path: Optional[Path] = None
    archived_flag = entry.get("archived")

    if archived_flag:
        # 정상 경로 — registry 신뢰
        archive_path = Path(entry.get("archive_path", ""))
        if not archive_path.exists():
            archive_path = None

    if archive_path is None:
        # 안전 가드 §3 — 폴더 스캔 fallback
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
            # 가장 큰 (= 진본일 확률 높음) 선택
            archive_path = max(candidates, key=lambda p: p.stat().st_size)

    if archive_path is None or not archive_path.exists():
        return False

    # 원위치 = 같은 jsonl 파일명을 가진 프로젝트 폴더
    # archive 폴더의 부모가 프로젝트 폴더 (per_project 일 때)
    # central 일 때는 PROJECTS_DIR 안의 같은 이름 프로젝트 폴더
    layout = str(get_archive_pref("archive_folder_layout"))
    if layout == "central":
        project_dir = PROJECTS_DIR / archive_path.parent.name
    else:
        project_dir = archive_path.parent.parent  # archive/ 의 부모

    project_dir.mkdir(parents=True, exist_ok=True)
    dst = project_dir / archive_path.name

    if dst.exists():
        # 안전 가드 §4+5 — stub vs 실제 충돌 자동 분기
        # archive 후 활성 Claude 가 만든 stub (수십 라인 미만) 이면 자동 백업 후
        # archive 본문 우선 복원. 진짜 충돌 (큰 파일) 만 .restored 로 회피.
        try:
            line_count = sum(1 for _ in dst.open(encoding="utf-8", errors="ignore"))
        except OSError:
            line_count = 999_999
        if line_count < 100:
            # stub 으로 판정 — 진단/식별 가치만 있으니 안전하게 백업
            ts = int(time.time())
            stub_backup = project_dir / f"{archive_path.stem}.stub-{ts}{archive_path.suffix}"
            try:
                shutil.move(str(dst), str(stub_backup))
            except OSError:
                # 백업 실패 시 fallback — 충돌 회피로
                ts = int(time.time())
                dst = project_dir / f"{archive_path.stem}.restored-{ts}{archive_path.suffix}"
        else:
            # 진짜 콘텐츠 충돌 — 기존 동작 (archive 가 .restored 로)
            ts = int(time.time())
            dst = project_dir / f"{archive_path.stem}.restored-{ts}{archive_path.suffix}"

    try:
        shutil.move(str(archive_path), str(dst))
    except OSError:
        return False

    # registry 의 4 필드 제거 — None 전달 시 키 pop 됨 (registry_set 동작)
    try:
        registry_set(
            sid,
            archived=None,
            archived_into=None,
            archive_path=None,
            archived_at=None,
        )
    except Exception:
        # registry 갱신 실패 시에도 파일은 이미 옮겨짐 — 그대로 둠
        pass

    return True


# ── 분해 (병합 역연산 — 부모의 모든 자식 일괄 복원) ─────────────────────
def unmerge_parent(parent_sid: str) -> tuple[int, int]:
    """부모에 병합된 모든 archive 자식을 일괄 분해 (원위치 복원).

    병합 (archive_session) 의 정확한 역연산. 자식들의 jsonl 을 원래 project_dir
    로 되돌리고 registry 의 4 archive 필드를 제거. 부모 entry 는 손대지 않음
    — 부모 라벨의 📦N 마커는 archived_children_count() 의 dynamic sweep
    결과라 자식이 사라지면 자동 0 → 마커 자동 소거.

    `pref_get("archive_restore_enabled") == "permanent"` 면 호출자가 미리
    거부해야 함 — 이 함수 자체는 항상 작동.

    Returns:
        (success, fail) — 시도한 자식 수 = success + fail
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
# claude SessionStart hook / last-prompt 트래커가 archived 자식의 sid 로
# main project_dir 에 metadata-only stub jsonl (~ <5KB, last-prompt /
# custom-title / agent-name 만) 을 자동 생성해서 트리에 stale entry 가
# 보이는 문제 해결용.
#
# 안전 기준 (3개 모두 만족 시만 sweep):
#   1. 파일 크기 < 5KB
#   2. registry 의 그 sid 가 archived=true
#   3. jsonl 안에 user/assistant role 메시지 0개
#
# 동작: archive/.stale-stubs/<ts>-<sid>.jsonl 로 이동 (삭제 X — 롤백 가능).

STUB_SWEEP_SIZE_LIMIT = 5 * 1024  # 5KB
STUB_SWEEP_QUARANTINE = ".stale-stubs"


def _is_stale_stub(jsonl_path: Path, sid: str) -> bool:
    """3 가드 모두 통과하면 True (sweep 대상)."""
    try:
        size = jsonl_path.stat().st_size
    except OSError:
        return False
    if size >= STUB_SWEEP_SIZE_LIMIT:
        return False
    entry = registry_get(sid)
    if not entry.get("archived"):
        return False
    # user/assistant 메시지 카운트
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
            return False  # 진짜 대화 메시지 발견 → stub 아님
    return True


def sweep_stale_stubs(project_dir: Path) -> list[str]:
    """project_dir 의 모든 jsonl 을 검사해 stub 이면 quarantine 폴더로 이동.

    Returns:
        sweep 된 sid 목록 (8자 prefix). 빈 리스트면 sweep 0건.
    """
    if not project_dir.exists():
        return []
    quarantine_dir = project_dir / "archive" / STUB_SWEEP_QUARANTINE
    swept: list[str] = []
    ts = int(time.time())
    for jsonl in project_dir.glob("*.jsonl"):
        sid = jsonl.stem
        # 기본 sid 형태 검증 (UUID4 — 36자 + 4 dash) — 잘못된 파일명 보호
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
    """PROJECTS_DIR 아래 모든 프로젝트 디렉토리에서 sweep_stale_stubs 일괄 실행.

    Returns:
        {project_name: [swept_sid_prefixes]} — sweep 된 것만 포함.
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


# ── sid → archive jsonl 경로 lookup ─────────────────────────────────────
def find_archived_session(sid: str) -> Optional[Path]:
    """sid 가 archive 된 세션이면 그 jsonl 경로 반환, 아니면 None.

    `gccfork_sessions.find_session_by_id` 가 일반 검색 실패 시 이 함수 호출
    하도록 보강 (Phase 1 후반에 한 줄 패치).
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


# ── 부모의 archive 자식 목록 ─────────────────────────────────────────────
def archived_children_for(parent_sid: str) -> list[ArchivedChildMeta]:
    """registry 에서 archived_into == parent_sid 인 자식들을 모두 반환.

    preview 렌더링이 사용. jsonl 본문은 lazy load 라 여기서는 메타만.
    sort 는 prefs `archive.child_sort_order` 기준.

    안전 가드 §3: registry 에 archived 플래그가 손상된 자식도 검출하도록
    parent 의 merged_from 리스트 + archive 폴더 스캔 으로 fallback. 이중
    검증으로 unmerge 시 자식 누락 방지 (2026-05-04 ca09 누락 사고 회고).
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
            # 파일 없어진 stale entry — 스킵 (UI 에서 정리 옵션 별도 추가 가능)
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

    # 안전 가드 §3 fallback — parent.merged_from 의 sid 들이 registry 손상으로
    # archived 플래그를 잃었어도 archive 폴더에 jsonl 이 있으면 검출.
    # 발견되면 archive_path 없는 ArchivedChildMeta 라도 만들어서 unmerge 가
    # restore_session(sid) 호출 시 폴더 스캔으로 다시 찾도록 함.
    parent_entry = sessions.get(parent_sid) or {}
    merged_from = parent_entry.get("merged_from") or []
    if merged_from:
        # archive 폴더 후보 — per_project 와 central 둘 다 시도
        archive_dirs: list[Path] = []
        # per_project: 부모의 jsonl 있을 만한 곳 (active_path 추정 어려우므로
        # PROJECTS_DIR 의 모든 프로젝트 폴더의 archive/ 를 후보로)
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

    # 정렬
    sort_order = str(get_archive_pref("archive_child_sort_order"))
    if sort_order == "alphabetic":
        out.sort(key=lambda m: (m.name or m.short_id).lower())
    elif sort_order == "branch_order":
        # 분기 시점 = archived_at 오름차순
        out.sort(key=lambda m: m.archived_at)
    else:
        # mtime — 최근이 위
        def _mtime(m: ArchivedChildMeta) -> float:
            try:
                return m.path.stat().st_mtime
            except OSError:
                return 0.0
        out.sort(key=_mtime, reverse=True)
    return out


# ── 부모 노드의 archive 자식 카운트 (라벨용) ─────────────────────────────
def archived_children_count(parent_sid: str) -> int:
    """부모 노드 라벨 `📦 N archived` 표시용 — 빠른 카운트."""
    reg = load_registry()
    sessions = reg.get("sessions", {}) or {}
    return sum(
        1
        for entry in sessions.values()
        if entry.get("archived") and entry.get("archived_into") == parent_sid
    )


# ── archive 모드 자식 모두 (전체 archive 화면용) ────────────────────────
def all_archived_sessions() -> list[ArchivedChildMeta]:
    """archive 화면 (=휴지통과 비슷) 에서 모든 archive 자식을 한 곳에 보여줄 때.

    parent_sid 별로 group 화 하는 건 호출자 책임.
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


# ── Preview 통합 렌더 — 4 가지 모드 ──────────────────────────────────────
def _read_archive_jsonl_preview(
    path: Path,
    max_bytes: Optional[int] = None,
) -> str:
    """archive 의 jsonl 을 읽어 사용자 보기 좋은 텍스트로 변환.

    각 라인의 user/assistant 메시지를 추출 + 짧게 정리. 시스템/도구 메시지는 skip.
    `max_bytes` 가 주어지면 그 만큼만 읽고 잘림 표시.
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
        return "  (파일 읽기 실패)"

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
        # 시스템 inject 메시지는 skip — `<` 로 시작하는 태그성 내용
        if body.startswith("<system-reminder>") or body.startswith("<command-name>"):
            continue
        prefix = "👤" if role == "user" else "🤖"
        out_lines.append(f"  {prefix} {body[:600]}")
        out_lines.append("")  # 사이 한 줄
    if truncated:
        out_lines.append("  …(생략됨, lazy load 모드)")
    return "\n".join(out_lines) if out_lines else "  (대화 내용 없음)"


def _format_child_header(
    meta: ArchivedChildMeta,
    fmt: Optional[str] = None,
) -> str:
    """자식 섹션 헤더 — opt 8 (simple/verbose) 따라."""
    if fmt is None:
        fmt = str(get_archive_pref("archive_section_header_format"))
    name = meta.name or meta.auto_summary or "(이름 없음)"
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
        turn = f"{meta.turn_count}턴" if meta.turn_count >= 0 else "?턴"
        archived_at = meta.archived_at[:19] if meta.archived_at else "?"
        return f"▶ {fork_emoji}{meta.short_id}  {name}  ·  {turn}  ·  {size_kb}KB  ·  {archived_at}"
    # simple (default)
    return f"▶ {fork_emoji}{meta.short_id}  {name}"


def _render_tail_sections(
    parent: Session,
    children: list[ArchivedChildMeta],
    width: int = 80,
) -> str:
    """B안 (기본) — 자식 섹션을 끝에 붙임. 각 자식별 헤더 + 본문.

    lazy_load (opt 6): ON 이면 자식별 처음 5KB 만 표시 + 잘림 표시.
    OFF 면 전체.
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
    """archive jsonl 을 (ts, role, body) 튜플 list 로 반환.

    interleave 렌더에 사용. 실패/빈 메시지는 skip. ts 가 없는 라인도 포함
    (빈 문자열 ts → 정렬 시 맨 앞으로). _read_archive_jsonl_preview 와 동일
    필터 로직 (system/tool/메타 제외).
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
    """A안 — 시간순 인터리브.

    여러 자식 archive jsonl 의 메시지를 timestamp 기준으로 섞어 단일
    timeline 으로 출력. (부모 본문은 preview 위쪽에 이미 표시되므로 자식들
    사이의 chronological 통합만 담당.)

    각 메시지에 자식 short_id + 색깔 점을 prefix 로 붙여 어느 자식 출처인지
    구분 (opt 7 child_color_distinction).
    """
    lazy = bool(get_archive_pref("archive_lazy_load"))
    color_dist = bool(get_archive_pref("archive_child_color_distinction"))
    max_bytes = 5 * 1024 if lazy else None

    sep = "━" * min(width, 78)
    out: list[str] = []
    out.append("")
    out.append(sep)
    out.append(f"📦 ARCHIVED CHILDREN ({len(children)}) — 시간순 인터리브")
    out.append(sep)
    out.append("")

    # (child_meta, ts, role, body) 튜플 모두 모아서 ts 정렬
    flat: list[tuple[str, ArchivedChildMeta, str, str]] = []
    for meta in children:
        for ts, role, body in _read_archive_jsonl_messages(meta.path, max_bytes=max_bytes):
            flat.append((ts, meta, role, body))
    flat.sort(key=lambda x: x[0])

    if not flat:
        out.append("  (자식들에 표시할 메시지 없음)")
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
        out.append(f"  …(각 자식 첫 {max_bytes // 1024}KB 까지만, lazy load)")
    return "\n".join(out)


def _render_headers_only(
    parent: Session,
    children: list[ArchivedChildMeta],
    width: int = 80,
) -> str:
    """C안 — 헤더 + 짧은 미리보기 (첫 user/assistant 1쌍).

    완전한 collapsible 은 TextArea 로 불가 → 대신 각 자식의 첫 user 메시지
    + 첫 assistant 메시지 (각 200자) 를 'snippet' 으로 보여줘 사용자가 어떤
    대화였는지 즉시 파악 가능. tail_sections 보다 훨씬 짧음.
    """
    sep = "━" * min(width, 78)
    out: list[str] = []
    out.append("")
    out.append(sep)
    out.append(f"📦 ARCHIVED CHILDREN ({len(children)}) — 헤더 + 미리보기")
    out.append(sep)
    out.append("")
    for meta in children:
        out.append(_format_child_header(meta))
        if meta.auto_summary:
            summary = meta.auto_summary.replace("\n", " ")[:120]
            out.append(f"   ↳ {summary}")

        # 짧은 snippet — 첫 user + 첫 assistant 메시지
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
    out.append("  (전체 본문은 설정 → preview_mode 를 tail_sections / interleave 로 변경)")
    return "\n".join(out)


def _render_split(
    parent: Session,
    children: list[ArchivedChildMeta],
    width: int = 80,
) -> str:
    """D안 — 카드형 (자식별 강한 시각 구분).

    진짜 위젯 split 은 메인 TUI 큰 변경이라 보류. 대신 각 자식을 box-drawing
    문자로 둘러싼 'card' 형태로 표시 → 시각적으로 또렷이 분리. tail_sections
    의 ━ 단순 구분선보다 훨씬 강한 분리감.
    """
    lazy = bool(get_archive_pref("archive_lazy_load"))
    max_bytes = 5 * 1024 if lazy else None
    inner_w = min(width, 78) - 2

    out: list[str] = []
    out.append("")
    out.append(f"📦 ARCHIVED CHILDREN ({len(children)}) — 카드 split")
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
            # box 안에 padding — 폭 초과 시 잘라서 다음 줄로 넘기지 않고 그대로 둠
            # (TextArea 가 wrap 처리하므로 OK)
            content = line.rstrip()
            out.append("│" + content.ljust(inner_w)[:inner_w] + "│")

        out.append(bot)
        out.append("")

    return "\n".join(out)


def build_archived_children_section(
    parent: Session,
    width: int = 80,
) -> str:
    """preview 끝에 붙일 archive 자식 섹션 텍스트 빌드 — dispatcher.

    `archive_preview_mode` (opt 1) 에 따라 4가지 함수 중 하나 호출.
    archive 자식 0개면 빈 문자열 반환 (=> 영향 없음).
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


# ── ArchiveConfirmScreen — 사용자 확인 모달 ─────────────────────────────
# textual 없으면 이 클래스 정의 자체가 실패하므로 모듈 import 도 실패 — 의도된 동작
# (메인 gccfork 는 textual 이 있는 PEP 723 venv 안에서 실행되므로 항상 OK).
# 단위 테스트는 textual venv 에서 돌리거나, 데이터 함수만 별도 import 하는
# 미니 스크립트로.
class ArchiveConfirmScreen(ModalScreen[bool]):
    """archive 이동 confirm 모달 — ForkNameScreen 톤.

    표시 내용:
      - 직접 선택한 세션 N개 + 함께 끌려가는 자손 M개
      - 자손 sid + 이름 미리보기 (앞 6개)
      - ★ 중요 표시 포함 시 별도 경고 라인 (opt 3 가 confirm 일 때)

    Esc 는 BINDINGS 로 처리 — App 의 quit 까지 propagate 안 됨.
    """

    BINDINGS = [
        Binding("escape", "cancel_screen", "취소", show=False),
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
                yield Static("[b]🗂 Archive 병합[/]", id="arc-title", markup=True)
                yield Static(
                    f"[dim]v{self.gccfork_version}[/]",
                    id="arc-meta", markup=True,
                )

            with Vertical(id="arc-scroll"):
                # 요약 섹션
                with Vertical(classes="arc-section"):
                    yield Static(
                        "[b][INFO][/] 요약",
                        classes="arc-section-title", markup=True,
                    )
                    yield Static(
                        f"직접 선택: [b]{len(self.directly_selected)}개[/b]  ·  "
                        f"끌려가는 자손: [b]{len(self.descendants)}개[/b]  ·  "
                        f"총 [b]{self.total}개[/b]",
                        markup=True,
                    )
                    if self.important_count > 0:
                        yield Static(
                            f"[red]★[/red] 중요 표시 포함: [b]{self.important_count}개[/b]",
                            markup=True,
                        )

                # 직접 선택 섹션
                with Vertical(classes="arc-section"):
                    yield Static(
                        "[b][SELECTED][/] 직접 선택한 세션",
                        classes="arc-section-title", markup=True,
                    )
                    for s in self.directly_selected[:6]:
                        title = (s.title or "(이름 없음)")[:50].replace("\n", " ")
                        star = "★ " if s.important else ""
                        yield Static(f"  {star}{s.short_id}  {title}")
                    if len(self.directly_selected) > 6:
                        yield Static(f"  …외 {len(self.directly_selected) - 6}개 더")

                # 자손 섹션 (있을 때만)
                if self.descendants:
                    with Vertical(classes="arc-section"):
                        yield Static(
                            "[b][DESCENDANTS][/] 함께 끌려가는 자손",
                            classes="arc-section-title", markup=True,
                        )
                        for s in self.descendants[:6]:
                            title = (s.title or "(이름 없음)")[:50].replace("\n", " ")
                            star = "★ " if s.important else ""
                            yield Static(f"  ↳ {star}{s.short_id}  {title}")
                        if len(self.descendants) > 6:
                            yield Static(f"  …외 {len(self.descendants) - 6}개 더")

                # 동작 설명
                with Vertical(classes="arc-section"):
                    yield Static(
                        "[b][WHAT HAPPENS][/] 동작",
                        classes="arc-section-title", markup=True,
                    )
                    yield Static("  • jsonl 파일은 archive/ 폴더로 이동 (보존)")
                    yield Static("  • registry 에 archived 표시 → 트리에서 부모 아래로 통합")
                    yield Static("  • sid 직접 호출 시 archive 자동 lookup (외부 .md 참조 안 깨짐)")
                    yield Static("  • 복원 가능 (설정에서 휴지통 패턴 활성화 시)")

            with Horizontal(id="arc-btn-row"):
                yield Button("Esc 취소", id="btn-arc-cancel")
                yield Static("", id="arc-btn-spacer")
                yield Button(
                    f"Archive ({self.total}개)",
                    id="btn-arc-confirm", variant="primary",
                )
        # CopyMenuOverlay 는 메인의 클래스라 사이드카에서 못 import — 여기선 생략.

    def on_mount(self) -> None:
        # 첫 포커스: 취소 — 무심코 Enter 쳐도 destructive 액션 발동 안 함
        self.query_one("#btn-arc-cancel", Button).focus()

    def action_cancel_screen(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn-arc-confirm":
            self.dismiss(True)
        elif bid == "btn-arc-cancel":
            self.dismiss(False)


# ── UnmergeConfirmScreen (분해 확인 모달) ──────────────────────────────
class UnmergeConfirmScreen(ModalScreen[bool]):
    """분해 (unmerge) confirm 모달 — ArchiveConfirmScreen 의 짝.

    부모 1개 + 그 부모에 병합된 자식 N개 표시 → 확인 시 일괄 분해.
    """

    BINDINGS = [
        Binding("escape", "cancel_screen", "취소", show=False),
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
    /* 본문 SelectableTextArea — drag-select + 우클릭 복사 가능 */
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
        # NOTE: `self.children` 은 textual Screen 의 built-in 속성 — shadow 불가.
        # 그래서 `archived_children` 으로 분리.
        self.archived_children = archived_children
        self.gccfork_version = gccfork_version
        self.total = len(archived_children)

    def compose(self) -> ComposeResult:
        # 본문 내용을 SelectableTextArea (drag-select + 우클릭 복사) 로 일체화.
        # SelectableTextArea 는 main 모듈에 있어 lazy import — circular import 방지.
        try:
            from gccfork import SelectableTextArea
        except Exception:
            SelectableTextArea = None  # type: ignore

        # 본문 plain text 빌드 — markup 없이 (TextArea 는 raw text)
        p = self.parent_session
        p_title = (p.title or "(이름 없음)")[:60].replace("\n", " ")
        lines: list[str] = []
        lines.append("[PARENT] 분해 대상 부모")
        lines.append(f"  📦{self.total}  {p.short_id}  {p_title}")
        lines.append("")
        lines.append(f"[CHILDREN] 원위치로 복귀할 자식 — {self.total}개")
        for c in self.archived_children[:8]:
            name = (c.name or "(이름 없음)")[:50].replace("\n", " ")
            lines.append(f"  ↩ {c.short_id}  {name}")
        if len(self.archived_children) > 8:
            lines.append(f"  …외 {len(self.archived_children) - 8}개 더")
        lines.append("")
        lines.append("[WHAT HAPPENS] 동작")
        lines.append("  • 자식 jsonl 들이 archive/ → 원래 project_dir 로 이동 (역연산)")
        lines.append("  • registry 의 archive 4 필드 (archived/archived_into/archive_path/archived_at) 제거")
        lines.append("  • 부모의 📦 마커 자동 소거 (자식 카운트 0)")
        lines.append("  • 자손이 더 깊이 archive 된 경우 그 단계는 보존 (한 단계만 분해)")
        body_text = "\n".join(lines)

        with Vertical(id="unm-box"):
            with Horizontal(id="unm-header"):
                yield Static("[b]GccForK[/]", id="unm-brand", markup=True)
                yield Static("[b]🔧 분해 (병합 해제)[/]", id="unm-title", markup=True)
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
                    # fallback: SelectableTextArea import 실패 시 Static 으로 (선택 불가)
                    yield Static(body_text, id="unm-body")

            with Horizontal(id="unm-btn-row"):
                yield Button("Esc 취소", id="btn-unm-cancel")
                yield Static("", id="unm-btn-spacer")
                yield Button(
                    f"분해 ({self.total}개)",
                    id="btn-unm-confirm", variant="primary",
                )

    def on_mount(self) -> None:
        # 첫 포커스: 취소 — 무심코 Enter 쳐도 destructive 액션 발동 안 함
        self.query_one("#btn-unm-cancel", Button).focus()

    def action_cancel_screen(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn-unm-confirm":
            self.dismiss(True)
        elif bid == "btn-unm-cancel":
            self.dismiss(False)


# ── ArchiveMixin — App 에 결합되는 액션 메서드 ──────────────────────────
class ArchiveMixin:
    """App 클래스에 mixin 으로 결합되어 archive 액션을 제공.

    필요 메서드 (App 측):
      - self.sessions (list[Session])
      - self._multi_selected_ids (set[str])
      - self.notify(msg, severity=...)
      - self.push_screen(screen, callback)
      - self.reload_sessions()
      - self._update_multi_action_visibility()
      - self.refresh_list()
      - GCCFORK_VERSION 글로벌 (없으면 "")
    """

    def action_archive_selected(self) -> None:
        """멀티 선택된 세션 + 자손을 archive 폴더로 이동.

        흐름:
        1. 멀티 선택 sid 모음 → Session 객체 변환
        2. 후손 재귀 수집 (`collect_subtree`)
        3. ★ 중요 처리 분기 (opt 3):
             - auto_include: 그냥 진행
             - confirm: 별도 모달 없이 ArchiveConfirmScreen 안에서 경고만 표시
             - reject: 중요 포함 시 거부 + notify
        4. ArchiveConfirmScreen 띄움
        5. confirm 시 모두 archive_session 호출
        """
        sel_ids: set[str] = set(getattr(self, "_multi_selected_ids", set()))
        if not sel_ids:
            try:
                self.notify("선택된 세션이 없습니다.", severity="warning")
            except Exception:
                pass
            return

        all_sessions: list[Session] = list(getattr(self, "sessions", []))
        directly_selected = [s for s in all_sessions if s.id in sel_ids]
        if not directly_selected:
            return

        descendants = collect_subtree([s.id for s in directly_selected], all_sessions)
        # 직접 선택과 후손은 서로 disjoint — collect_subtree 가 root_sids 제외하고 반환

        all_targets = directly_selected + descendants
        important_count = sum(1 for s in all_targets if s.important)

        # opt 3: ★ 중요 처리
        important_handling = str(get_archive_pref("archive_important_handling"))
        if important_count > 0 and important_handling == "reject":
            try:
                self.notify(
                    f"★ 중요 표시된 세션 {important_count}개가 포함됨 — 설정상 거부됨. "
                    "★ 떼고 다시 시도하세요.",
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
                self.notify(f"archive 모달 띄우기 실패: {exc}", severity="error")
            except Exception:
                pass

    def _do_archive_batch(
        self,
        directly_selected: list[Session],
        descendants: list[Session],
    ) -> None:
        """ArchiveConfirmScreen 통과 후 실제 이동.

        부모 sid 결정 규칙:
          - 직접 선택한 세션 → 그 세션의 parent_id (없으면 빈 문자열)
          - 자손 → 직속 부모의 sid (재귀 archive 시에도 부모-자식 관계 유지)
        """
        moved = 0
        failed = 0

        # 자손은 자기 직속 부모를 archived_into 로 가져야 트리 구조 보존
        for sess in descendants:
            parent_sid = sess.parent_id or ""
            if archive_session(sess, parent_sid):
                moved += 1
            else:
                failed += 1

        # 직접 선택은 자기 부모 (없으면 root, 빈 문자열) 로
        for sess in directly_selected:
            parent_sid = sess.parent_id or ""
            if archive_session(sess, parent_sid):
                moved += 1
            else:
                failed += 1

        try:
            if failed:
                self.notify(
                    f"Archive: {moved}개 이동, {failed}개 실패",
                    severity="warning",
                )
            else:
                self.notify(f"🗂 Archive: {moved}개 이동 완료")
        except Exception:
            pass

        # 멀티 선택 클리어 + 리로드
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
        """단일 선택된 부모 세션의 모든 archive 자식을 일괄 분해 (병합 역연산).

        활성 조건 (호출자가 가드):
          - 멀티 선택 1개
          - 그 세션에 archived_children > 0 (📦 마커)

        흐름:
        1. 선택 세션 → archived_children_for(sid) 로 자식 목록 조회
        2. permanent 모드 가드 (전체 거부)
        3. UnmergeConfirmScreen 띄움
        4. confirm 시 unmerge_parent() 호출 → 결과 notify + reload
        """
        if str(get_archive_pref("archive_restore_enabled")) == "permanent":
            try:
                self.notify("분해 비활성화 (설정상 영구 archive 모드)", severity="warning")
            except Exception:
                pass
            return

        sel_ids: set[str] = set(getattr(self, "_multi_selected_ids", set()))
        if len(sel_ids) != 1:
            try:
                self.notify("분해는 부모 1개 단일 선택일 때만 가능합니다.", severity="warning")
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
                self.notify("선택한 세션에 병합된 자식이 없습니다 (📦 0).", severity="warning")
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
                    self.notify(f"🔧 분해 완료: {success}개 자식이 원위치로 복귀")
                else:
                    self.notify(
                        f"🔧 분해 부분 실패: 성공 {success} / 실패 {fail}",
                        severity="warning",
                    )
            except Exception:
                pass
            try:
                # 분해 후 multi 선택 해제 (선택했던 부모는 이제 평범한 세션)
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
                self.notify(f"분해 모달 띄우기 실패: {exc}", severity="error")
            except Exception:
                pass

    def action_restore_archived(self, sid: str) -> None:
        """archive 화면에서 복원 액션 호출용. opt 4 가 permanent 면 거부.

        UI 측에서 호출 — Phase 4 의 archive view 모달이 사용.
        """
        if str(get_archive_pref("archive_restore_enabled")) == "permanent":
            try:
                self.notify("복원 비활성화 (설정상 영구 archive 모드)", severity="warning")
            except Exception:
                pass
            return
        if restore_session(sid):
            try:
                self.notify(f"복원됨: {sid[:8]}")
            except Exception:
                pass
            try:
                self.reload_sessions()
            except Exception:
                pass
        else:
            try:
                self.notify(f"복원 실패: {sid[:8]}", severity="error")
            except Exception:
                pass


# ── 모듈 export ─────────────────────────────────────────────────────────
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
