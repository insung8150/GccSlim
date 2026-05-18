"""gccfork_merge — True Merge (Phase 6 / model B) 사이드카 모듈.

병합 = 새 세션 N (UUID4) 탄생 + 모든 stitching variant 미리 생성 + active jsonl 은 pref 결정.

원칙:
  P1. 새 sid (UUID4)
  P2. 공통분모 (uuid 기준 prefix) + per-session 고유 추출
  P3. 5개 variant 모두 .merged/<N.sid>/method-*.jsonl 로 생성
  P4. active <project_dir>/<N.sid>.jsonl 은 pref `merge_stitching_method` 따라 copy
  P5. 자식 sid 영원 보존 (archived=true 마킹만, jsonl 은 archive/ 로)
  P6. 양방향 추적 (find_archived_session ↔ archived_children_for)
  P7. 분해 = 정확한 역연산 (N 흔적 0 + 원본 복귀)
  P8. variant jsonl 의 모든 sessionId == N.sid
  P9. linear method 의 parentUuid chain 무결성
  P10. 공통 조상 없으면 NoCommonAncestorError

기존 gccfork_archive 의 archive_session / restore_session 재사용.
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

# Textual UI imports — MergeConfirmScreen + Mixin 용.
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, RadioButton, RadioSet, Static


# ── 상수 ─────────────────────────────────────────────────────────────────
STITCHING_METHODS: tuple[str, ...] = (
    "linear",        # common + 각자 고유 부분을 selection 순서대로 chain
    "interleave",    # common + 모든 고유 메시지를 timestamp 정렬해 chain
    "parallel",      # common + 각 고유 부분을 원래 parentUuid 유지 (분기 그대로)
    "common-only",   # common 만 (드래프트/통합본 미생성)
    "as-sections",   # common + 섹션 구분 system 메시지 + 각 고유
)

MERGE_DEFAULTS: dict[str, object] = {
    "merge_stitching_method": "interleave",
    # 병합 직후 N (통합 jsonl) 에 자동 strong slim in-place 적용 — 통합본이
    # 무거워서 첫 resume 시 auto-compact 트리거되는 사고 (ca09 21 MB 케이스)
    # 예방. 사용자가 모달에서 토글 가능, default ON.
    "merge_auto_slim_after": True,
    "merge_auto_slim_mode": "strong",
}


class NoCommonAncestorError(ValueError):
    """선택된 세션들이 공통 조상 (registry parent_id chain) 을 공유하지 않을 때."""


class ActiveSessionInMergeError(ValueError):
    """병합 sources 에 현재 실행 중인 Claude 세션 sid 가 포함될 때.

    활성 세션을 archive 하면 Claude 프로세스가 사라진 jsonl 에 계속 쓰려다
    stub 을 만들어 registry 메타가 손상됨 (2026-05-04 사고 회고). 이 에러는
    그 시나리오를 미연 차단.
    """
    def __init__(self, active_sids: list[str]):
        self.active_sids = active_sids
        msg = (
            f"활성 Claude 세션 {len(active_sids)}개가 병합 대상에 포함됨. "
            f"먼저 해당 세션을 종료(/quit) 후 다시 시도하세요. "
            f"sids: {[s[:8] for s in active_sids]}"
        )
        super().__init__(msg)


# ── jsonl I/O ────────────────────────────────────────────────────────────
def _read_jsonl_messages(path: Path) -> list[dict]:
    """jsonl 의 모든 라인을 dict 리스트로 반환. 빈 라인 / 깨진 라인 skip."""
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
    """msgs 를 jsonl 로 atomic write (tmp → rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for m in msgs:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
    tmp.replace(path)


def _replace_sid(msg: dict, new_sid: str) -> dict:
    """메시지 dict copy + sessionId 필드만 new_sid 로 치환."""
    out = dict(msg)
    out["sessionId"] = new_sid
    return out


def _origin_prefix(orig_sid: str, timestamp: Optional[str]) -> str:
    """interleave 모드 출신 표시 prefix. 형식: '[<sid8> HH:MM] '."""
    sid8 = (orig_sid or "")[:8] or "????????"
    hhmm = ""
    if timestamp and len(timestamp) >= 16:
        # ISO8601 'YYYY-MM-DDTHH:MM:SS...' → 'HH:MM'
        hhmm = timestamp[11:16]
    return f"[{sid8} {hhmm}] " if hhmm else f"[{sid8}] "


def _inject_origin_prefix(msg: dict, orig_sid: str) -> dict:
    """user/assistant 메시지의 본문 텍스트 앞에 출신 prefix 주입.

    - role != user/assistant → 그대로 반환 (system, metadata 등은 손대지 않음)
    - content 가 string → prepend
    - content 가 list[block] → 첫 'text' 타입 block 의 text 에 prepend.
                              text block 이 없으면 (tool_use 만) 새 text block 을
                              맨 앞에 삽입
    원본 dict 안 건드리도록 message/content 만 새로 만든다 (shallow chain 보호).
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
        # content 가 None 이거나 예상 못한 타입 → 손 안 댐
        return msg

    out = dict(msg)
    out["message"] = new_body
    return out


# ── content-based diff key ──────────────────────────────────────────────
# UUID/timestamp 같은 메타데이터를 제외하고 "사람이 읽는 본문" 만으로
# 메시지를 식별. 하드복제(내용 동일, sid/uuid 신규)도 prefix 매칭됨.
_META_KEYS_STRIP = {
    "uuid", "parentUuid", "sessionId", "requestId", "timestamp",
    "id", "tool_use_id", "cache_control",
}


def _strip_meta(obj):
    """dict/list 재귀 순회하며 메타데이터 키 제거. 본문 텍스트는 보존."""
    if isinstance(obj, dict):
        return {
            k: _strip_meta(v) for k, v in obj.items()
            if k not in _META_KEYS_STRIP
        }
    if isinstance(obj, list):
        return [_strip_meta(x) for x in obj]
    return obj


def _msg_content_key(msg: dict) -> str:
    """메시지의 sha256 hash (메타데이터 제외, 본문만 정규화 후).

    같은 내용이면 같은 key → UUID 가 달라도 prefix 매칭됨.
    하드복제 / fork / 동일 promp 재실행 모두 동일 key.
    """
    cleaned = _strip_meta(msg)
    canonical = json.dumps(cleaned, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ── 공통/고유 분리 ──────────────────────────────────────────────────────
def extract_common_and_unique(
    sessions: list[Session],
) -> tuple[list[dict], dict[str, list[dict]]]:
    """선택된 세션들에서 공통 prefix + per-session 고유 부분 추출.

    공통 = 모든 세션의 i 번째 라인의 *content key* (메타데이터 제외 sha256)
           가 일치하는 한 가장 긴 prefix.
    고유 = 공통 이후의 각자 tail.

    UUID 가 아닌 content 기반 비교라서 하드복제(같은 내용, 신규 sid/uuid)
    도 정상 prefix 매칭됨.

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
    """sid 부터 root 까지 parent_id chain (자기 포함, 자손→조상 순)."""
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
    """모든 세션의 ancestor chain 교집합 중 가장 자손 가까운 것 (= LCA).

    Returns:
        LCA sid 또는 공통 조상 없으면 None.
        (단일 세션이면 그 sid 자체 반환.)
    """
    if not sessions:
        return None
    chains = [_ancestor_chain(s.id) for s in sessions]
    # chains[0] 은 자손→조상 순 — 자손 쪽부터 walk 하며 다른 모든 chain 안에 있는 첫 sid
    for candidate in chains[0]:
        if all(candidate in chain for chain in chains[1:]):
            return candidate
    return None


# ── stitching helper ────────────────────────────────────────────────────
def _last_anchor_uuid(msgs: list[dict]) -> Optional[str]:
    """msgs 역순으로 walk 해서 첫 non-None uuid 반환. 없으면 None.

    Chain stitching 의 anchor 용. common 의 마지막 메시지가 metadata
    (agent-name / permission-mode / custom-title 등 uuid 없는 system event)
    이면 `common[-1].get("uuid")` 가 None 이 되어 다음 unique 메시지의
    parentUuid 를 None 으로 set → claude resume 이 chain root 로 처리해서
    이전 history 가 화면에 표시 안 되는 버그 발생.

    이 helper 로 metadata 를 건너뛰고 마지막 user/assistant/tool/system
    (uuid 가 있는 실제 conversational 메시지) 의 uuid 를 anchor 로 사용.
    """
    for m in reversed(msgs):
        u = m.get("uuid")
        if u:
            return u
    return None


# ── stitching 5종 ───────────────────────────────────────────────────────
def _stitch_linear(
    common: list[dict],
    unique_by_sid: dict[str, list[dict]],
    sids_in_order: list[str],
    new_sid: str,
) -> list[dict]:
    """common + 각 세션 고유를 순서대로 chain (parentUuid 재연결).

    last_uuid 갱신 시 None (metadata 메시지) 는 skip — 그래야 다음 sid 의
    첫 메시지가 metadata uuid=None 을 가리키지 않고 진짜 chain anchor 를 유지.
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
    """common + 모든 고유 메시지를 timestamp 오름차순으로 chain.

    각 user/assistant 메시지 본문 앞에 '[<sid8> HH:MM] ' 출신 prefix 주입
    (origin 표시 — claude UI 에서 어느 분기 출신인지 즉시 보임).
    last_uuid 갱신 시 None (metadata 메시지) 는 skip — chain 무결성 보존.
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
    """common + 각 고유 부분을 원래 parentUuid 유지 (분기 그대로)."""
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
    """공통 prefix 만 (고유 부분 모두 drop)."""
    return [_replace_sid(m, new_sid) for m in common]


def _stitch_as_sections(
    common: list[dict],
    unique_by_sid: dict[str, list[dict]],
    sids_in_order: list[str],
    new_sid: str,
) -> list[dict]:
    """common + 섹션 구분자 (synthetic system message) + 각 고유 부분."""
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
            "message": {"role": "system", "content": f"──── 분기 {sid[:8]} ────"},
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


# ── 헬퍼 — N 의 project_dir / variants 폴더 ──────────────────────────────
def _project_dir_for(sessions: list[Session]) -> Path:
    """선택된 세션들의 공통 project_dir (모두 같다고 가정)."""
    return sessions[0].jsonl_path.parent


def _variants_dir(project_dir: Path, new_sid: str) -> Path:
    return project_dir / ".merged" / new_sid


def _active_path(project_dir: Path, new_sid: str) -> Path:
    return project_dir / f"{new_sid}.jsonl"


# ── 공개 API ────────────────────────────────────────────────────────────
def merge_into_new_session(
    sessions: list[Session],
    name: Optional[str] = None,
) -> str:
    """모델 B 병합 — 새 sid N 탄생, variants 5개 생성, archive 자식 마킹.

    Args:
        sessions: 병합 대상 (≥2 권장, 1개면 단순 archive 와 동일 효과)
        name: N 의 custom_name. None 이면 자동 생성.

    Returns:
        N 의 sid (UUID4 문자열)

    Raises:
        NoCommonAncestorError: 선택된 세션들이 registry 상 공통 조상이 없을 때.
        ValueError: sessions 가 비어있을 때.
    """
    if not sessions:
        raise ValueError("merge_into_new_session: sessions 비어있음")

    # 안전 가드 §1 — 활성 sid 차단 (2026-05-04 사고 예방)
    # 활성 세션을 sources 로 넣으면 archive 후 Claude 프로세스가 stub 만듦 →
    # registry 메타 손상 → unmerge 시 일부 자식 검출 실패. 미연 차단.
    active_map = all_active_sid_pid_map()
    active_in_sources = [s.id for s in sessions if s.id in active_map]
    if active_in_sources:
        raise ActiveSessionInMergeError(active_in_sources)

    # 공통 조상 확인 (단 1개 세션이면 자기 자신이 LCA — 통과)
    lca = find_lca(sessions)
    if lca is None and len(sessions) > 1:
        raise NoCommonAncestorError(
            f"공통 조상 없음 — 무관한 세션들을 합칠 수 없습니다 "
            f"(sids: {[s.id[:8] for s in sessions]})"
        )

    # 새 sid 부여
    new_sid = str(uuid.uuid4())
    project_dir = _project_dir_for(sessions)
    variants_dir = _variants_dir(project_dir, new_sid)
    variants_dir.mkdir(parents=True, exist_ok=True)

    # ── Fold-merge 로 5 variant 생성 (구 stitcher 대치) ──
    # 기존 LCA + content-key 기반 → fold-merge 의 uuid 교집합 기반.
    # 슬림된 jsonl 도 안전 처리, 누락 0 / 중복 0 보장.
    from gccfork_merge_fold import (
        split_common_and_unique as _fold_split,
        STITCHERS as _FOLD_STITCHERS,
    )

    sources_in_order = [s.jsonl_path for s in sessions]
    common, unique_by_path = _fold_split(sources_in_order)

    # NoCommonAncestorError 호환 — common 0 + sessions > 1 이면 진짜 공통 조상 없음
    if not common and len(sessions) > 1:
        raise NoCommonAncestorError(
            f"uuid 교집합 0 — 공통 메시지 없음 "
            f"(sids: {[s.id[:8] for s in sessions]})"
        )

    for method, stitcher in _FOLD_STITCHERS.items():
        msgs = stitcher(common, unique_by_path, new_sid)
        _write_jsonl_messages(variants_dir / f"method-{method}.jsonl", msgs)

    sids_in_order = [s.id for s in sessions]

    # registry: N entry — parent_id 는 LCA 가 selection 의 부모면 LCA, 아니면 None
    # (단순화: selection 중 하나라도 LCA 와 같으면 LCA 가 자기 자신 — 그땐 LCA 의 부모)
    n_parent_id: Optional[str] = lca
    if lca and lca in sids_in_order:
        # lca 가 selection 중 하나 → 그 lca 의 parent_id 가 N 의 parent
        lca_entry = registry_get(lca)
        n_parent_id = lca_entry.get("parent_id") if lca_entry else None

    auto_name = name or f"🗂 merged: {len(sessions)}개"
    registry_set(
        new_sid,
        parent_id=n_parent_id,
        name=auto_name,
        merged_from=sids_in_order,
        merged_at=datetime.now(timezone.utc).isoformat(),
        is_merged=True,
    )

    # active jsonl 결정 + copy
    sync_active_jsonl(new_sid, project_dir=project_dir)

    # 자식들 archive (기존 archive_session 재사용 — N 으로 archived_into 설정)
    for s in sessions:
        archive_session(s, parent_sid=new_sid)

    return new_sid


def sync_active_jsonl(
    new_sid: str,
    project_dir: Optional[Path] = None,
) -> bool:
    """pref `merge_stitching_method` 에 따라 active jsonl 을 variant 에서 copy.

    Args:
        new_sid: 병합 결과 N 의 sid
        project_dir: 알고 있으면 전달, None 이면 archived children 통해 추론.

    Returns:
        True = 성공, False = 못 찾음.
    """
    method = str(pref_get("merge_stitching_method") or MERGE_DEFAULTS["merge_stitching_method"])
    if method not in STITCHING_METHODS:
        method = MERGE_DEFAULTS["merge_stitching_method"]

    if project_dir is None:
        # children 의 archive_path 로 추론
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
    """N 의 active jsonl 이 어떤 variant 와도 동일하면 pristine, 아니면 dirty.

    pristine = 병합 직후 또는 method 전환만 한 상태 (사용자 추가 작업 0)
    dirty    = claude --resume 등으로 신규 메시지 추가됨

    Returns:
        True = pristine (안전하게 전체 삭제 가능)
        False = dirty (보존 분해 필요 — 신규 작업 손실 위험)
    """
    children = archived_children_for(new_sid)
    if not children:
        # 자식 없으면 비교 기준 없음 — pristine 으로 간주 (안전)
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
    """active 라인 수 - 가장 큰 variant 라인 수 (대략적 신규 턴 카운트).

    대략적 (variant 별 라인 수가 다르고, 사용자가 method 전환 후 추가 작업
    했을 수도 있어 정확한 값은 jsonl 비교가 필요). UI 표시용 추정치.
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
    """병합 역연산 — pristine/dirty 자동 분기 (mode='auto') 또는 강제.

    mode:
      "auto"     — pristine → 전체 삭제 (역연산), dirty → N 보존 (자식만 복귀)
      "delete"   — 항상 전체 삭제 (dirty 신규 턴 손실 위험)
      "preserve" — 항상 보존 (pristine 이어도 N 을 독립 세션으로 살림)

    공통 작업 (모든 mode):
      - archived_children_for(N) → 각 자식 restore_session()
      - .merged/<N.sid>/ 변형 폴더 삭제 (보관 가치 없음)
      - .merged/ 빈 디렉토리면 정리 (cosmetic)

    mode='deleted' 추가 작업:
      - N 의 active jsonl 삭제
      - registry 에서 N entry 통째 제거 (sid 도 사라짐)

    mode='preserved' 추가 작업:
      - N 의 active jsonl 유지 (사용자 신규 작업 보존)
      - registry: is_merged / merged_from / merged_at 만 제거
      - sid + parent_id + custom_name 유지 → 트리에 독립 세션으로 보임
      - 외부 참조 (`claude --resume <N.sid>`, .md 링크) 안 깨짐

    Returns:
        True = 성공, False = N 이 존재하지 않음 등.
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

    # 자식 복원 (모든 mode 공통)
    for c in children:
        restore_session(c.sid)

    # .merged/<N.sid>/ 폴더 삭제 (모든 mode 공통 — 더 이상 의미 없음)
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
        # active jsonl 삭제 + registry 통째 제거
        if project_dir is not None:
            active = _active_path(project_dir, new_sid)
            if active.exists():
                try:
                    active.unlink()
                except OSError:
                    pass
        registry_remove(new_sid)
    else:
        # 보존 — active jsonl 유지, registry 의 merge 흔적만 제거
        # 안전 가드 §6 — entry 가 빈 dict 되지 않도록 핵심 필드 명시 보존
        # (예전: is_merged/merged_from/merged_at 만 None pop 시 entry 가 비어버림)
        existing = n_entry or {}
        keep_name = existing.get("name") or "🗂 (분리됨)"
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


# ── MergeConfirmScreen (병합 확인 모달 — model B) ───────────────────────
# 디자인 철학 (CLAUDE.md §1~§5) 준수:
#   - 색: $accent X% 사다리만
#   - 보더: round $accent Y% 만
#   - 헤더: 좌측 정렬 + bold + accent
#   - 8-grid 여백, 위젯 4단계
class MergeConfirmScreen(ModalScreen[Optional[dict]]):
    """🗂 병합 확인 모달 — 분석 미리보기 + method 선택 + 이름 입력.

    dismiss(None)              # 취소
    dismiss({"method": ..., "name": ...})  # 확인
    """

    BINDINGS = [
        Binding("escape", "cancel_screen", "취소", show=False),
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

    # method 옵션 라벨 — 사용자에게 짧게 보이는 한글 + 영문 method id
    _METHOD_LABELS: tuple[tuple[str, str], ...] = (
        ("interleave",   "interleave  — 공통 + 고유 timestamp 정렬 + 출신 prefix [sid HH:MM] (기본)"),
        ("linear",       "linear  — 공통 + 각자 고유 순차 chain"),
        ("parallel",     "parallel  — 공통 + 분기 그대로 유지"),
        ("common-only",  "common-only  — 공통만 (고유 drop)"),
        ("as-sections",  "as-sections  — 공통 + 섹션 구분자 + 각 고유"),
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
                yield Static("[b]🗂 병합 — true merge (새 sid 통합)[/]",
                             id="merge-title", markup=True)
                yield Static(f"[dim]v{self.gccfork_version}[/]",
                             id="merge-meta", markup=True)

            with Vertical(id="merge-scroll"):
                # 1. 이름 (최상단)
                with Vertical(classes="merge-section"):
                    yield Static("📛 새 세션 이름", classes="merge-section-title")
                    yield Input(value=self.suggested_name, id="merge-name-input")

                # 2. 병합 후 옵션
                with Vertical(classes="merge-section"):
                    yield Static("⚙ 병합 후 옵션", classes="merge-section-title")
                    yield Checkbox(
                        "🔻 병합 후 자동 슬림 (strong, in-place) — 통합본 가벼워짐, 권장",
                        value=self._default_auto_slim,
                        id="merge-auto-slim-cb",
                    )

                # 3. 선택 세션 목록
                with Vertical(classes="merge-section"):
                    yield Static("📋 선택된 세션", classes="merge-section-title")
                    for t in self.targets[:8]:
                        title = (t.title or "(이름 없음)")[:50].replace("\n", " ")
                        yield Static(f"  • {t.short_id}  {title}")
                    if len(self.targets) > 8:
                        yield Static(f"  …외 {len(self.targets) - 8}개 더")

                # 4. 분석 결과
                with Vertical(classes="merge-section"):
                    yield Static("🔍 메시지 분석", classes="merge-section-title")
                    yield Static(f"  공통 prefix: [b]{self.common_count}[/b]개 메시지", markup=True)
                    for t in self.targets[:6]:
                        n = self.unique_counts.get(t.id, 0)
                        yield Static(f"  {t.short_id} 고유: [b]{n}[/b]개", markup=True)

                # 5. method 선택
                with Vertical(classes="merge-section"):
                    yield Static("🔧 stitching 방법 선택", classes="merge-section-title")
                    with RadioSet(id="merge-method-set"):
                        for method, label in self._METHOD_LABELS:
                            rb = RadioButton(label, value=(method == self.default_method),
                                             id=f"method-{method}")
                            yield rb

                # 6. 동작 설명
                with Vertical(classes="merge-section"):
                    yield Static("ℹ 동작", classes="merge-section-title")
                    yield Static("  • 새 sid (UUID4) 부여 → 5 variant 모두 미리 생성 (.merged/<N>/)")
                    yield Static("  • 선택한 method 가 active jsonl — 설정에서 차후 전환 가능")
                    yield Static("  • 원본은 archive 보관 (sid 영원, 분해 시 완전 복원)")
                    yield Static("  • 자동 슬림 ON → 첫 resume 시 auto-compact 회피")

            with Horizontal(id="merge-btn-row"):
                yield Button("Esc 취소", id="btn-merge-cancel")
                yield Static("", classes="merge-spacer")
                yield Button(f"🗂 병합 실행 ({len(self.targets)}개)",
                             id="btn-merge-confirm", variant="primary")

    def on_mount(self) -> None:
        # 첫 포커스: 취소 — destructive 보호
        try:
            self.query_one("#btn-merge-cancel", Button).focus()
        except Exception:
            pass

    def action_cancel_screen(self) -> None:
        self.dismiss(None)

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        # RadioButton id = "method-<name>" → method 추출
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


# ── UnmergePreserveConfirmScreen (dirty N 분해 시 보존 확인) ─────────────
class UnmergePreserveConfirmScreen(ModalScreen[Optional[str]]):
    """N 에 신규 작업 (dirty) 가 있을 때 띄우는 분해 모드 선택 모달.

    dismiss(None)         # 취소
    dismiss("preserve")   # N 보존 분해 (default, 권장)
    dismiss("delete")     # 강제 전체 삭제 (신규 작업 손실)
    """

    BINDINGS = [Binding("escape", "cancel_screen", "취소", show=False)]

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
                yield Static("[b]🔧 분해 — 신규 작업 감지[/]",
                             id="unmp-title", markup=True)
                yield Static(f"[dim]v{self.gccfork_version}[/]",
                             id="unmp-meta", markup=True)

            with Vertical(id="unmp-body"):
                with Vertical(classes="unmp-section"):
                    yield Static("⚠ 상황", classes="unmp-section-title")
                    yield Static(
                        f"  N ({self.new_sid[:8]}) 에 병합 후 [b]신규 메시지 약 {self.new_turn_count}개[/b] 추가됨",
                        markup=True,
                    )
                    yield Static("  → 그냥 삭제하면 그 작업이 영구 손실됩니다.")

                with Vertical(classes="unmp-section"):
                    yield Static("✅ 권장: 보존 분해", classes="unmp-section-title")
                    yield Static("  • 자식 (B/C/...) 는 원위치 복귀")
                    yield Static("  • N 의 active jsonl 그대로 유지 (신규 작업 보존)")
                    yield Static("  • N 은 트리에 [b]독립 세션[/b] 으로 보임 (sid 영원, 외부 참조 안 깨짐)", markup=True)
                    yield Static("  • .merged/<N>/ 변형 폴더만 정리")

                with Vertical(classes="unmp-section"):
                    yield Static("⚠ 강제 삭제", classes="unmp-section-title")
                    yield Static("  • N 의 active jsonl 삭제 — [b]신규 작업 영구 손실[/b]", markup=True)
                    yield Static("  • registry entry 통째 제거 — N.sid 외부 참조 dead link")

            with Horizontal(id="unmp-btn-row"):
                yield Button("Esc 취소", id="btn-unmp-cancel")
                yield Static("", classes="unmp-spacer")
                yield Button("⚠ 강제 삭제", id="btn-unmp-delete")
                yield Button("✅ 보존 분해 (권장)", id="btn-unmp-preserve",
                             variant="primary")

    def on_mount(self) -> None:
        # 기본 포커스: 보존 (권장) — Enter 시 안전한 동작
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


# ── MergeMixin (UI 통합용 — 차후 본체에서 결합) ──────────────────────────
class MergeMixin:
    """App 측 액션 메서드. 본체 (gccfork) 가 이 mixin 을 결합해서 사용.

    필요 메서드 (App 측):
      - self.sessions
      - self._multi_selected_ids
      - self.notify
      - self.push_screen / reload_sessions / refresh_list
    """

    def action_merge_selected(self) -> None:
        """🗂 병합 model B — MergeConfirmScreen 띄우고 confirm 후 실행."""
        sel_ids: set[str] = set(getattr(self, "_multi_selected_ids", set()))
        if len(sel_ids) < 2:
            try:
                self.notify("병합은 2개 이상 선택해야 합니다.", severity="warning")
            except Exception:
                pass
            return
        all_sessions = list(getattr(self, "sessions", []))
        targets = [s for s in all_sessions if s.id in sel_ids]
        if len(targets) < 2:
            return

        # 분석 미리보기 (실제 변경 X)
        try:
            common, unique = extract_common_and_unique(targets)
        except Exception:
            common, unique = [], {}
        common_count = len(common)
        unique_counts = {sid: len(msgs) for sid, msgs in unique.items()}

        # 공통조상 사전 검증 (모달 띄우기 전에 거부)
        try:
            lca = find_lca(targets)
            if lca is None:
                self.notify(
                    "병합 거부 — 선택된 세션들이 공통 조상을 공유하지 않습니다.",
                    severity="error",
                )
                return
        except Exception:
            pass

        # 자동 이름 후보
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
            # 사용자가 모달에서 method 변경했으면 pref 도 갱신 (다음 병합 디폴트)
            try:
                pref_set("merge_stitching_method", method)
            except Exception:
                pass
            # auto_slim 토글도 pref 에 기억 — 다음 병합 시 같은 값이 default
            try:
                pref_set("merge_auto_slim_after", bool(auto_slim))
            except Exception:
                pass
            try:
                new_sid = merge_into_new_session(targets, name=name)
            except ActiveSessionInMergeError as e:
                # 안전 가드 §1 — 활성 sid 차단 (2026-05-04 사고 예방)
                try:
                    self.notify(f"⛔ 병합 차단: {e}", severity="error", timeout=8.0)
                except Exception:
                    pass
                return
            except NoCommonAncestorError as e:
                try:
                    self.notify(f"병합 거부: {e}", severity="error")
                except Exception:
                    pass
                return
            except Exception as e:
                try:
                    self.notify(f"병합 실패: {e}", severity="error")
                except Exception:
                    pass
                return
            try:
                self.notify(
                    f"🗂 병합 완료: {new_sid[:8]} (method={method}, 자식 {len(targets)}개)"
                )
            except Exception:
                pass

            # 병합 후 자동 슬림 (체크박스 ON 시) — N 의 active jsonl 에 in-place strong slim
            if auto_slim:
                try:
                    # gccfork main 에서 함수 import (circular 회피용 lazy)
                    from gccfork import slim_fork_session_with
                    from gccfork_sessions import parse_session
                    project_dir = _project_dir_for(targets)
                    n_path = _active_path(project_dir, new_sid)
                    n_session = parse_session(n_path) if n_path.exists() else None
                    if n_session is None:
                        raise RuntimeError("새 통합 jsonl 을 파싱하지 못함")
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
                            f"🔻 자동 슬림 완료: {size_before // 1024}K → "
                            f"{size_after // 1024}K (-{pct:.1f}%)",
                            timeout=6.0,
                        )
                    except Exception:
                        pass
                except Exception as exc:
                    try:
                        self.notify(
                            f"⚠ 자동 슬림 실패 (병합 자체는 성공): {exc}",
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

        # default_auto_slim — pref 우선, 없으면 MERGE_DEFAULTS
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
                self.notify(f"병합 모달 띄우기 실패: {exc}", severity="error")
            except Exception:
                pass

    def action_unmerge_selected_v2(self) -> None:
        """🔧 분해 — model B unmerge_new_session.

        활성 조건 (호출자 가드):
          - 단일 선택
          - 그 세션 entry 의 is_merged == True

        분기:
          - pristine (병합 직후 + method 전환만) → 즉시 전체 삭제 (notify 만)
          - dirty (claude --resume 등으로 신규 메시지 추가) → 보존/삭제/취소
            모달 띄움
        """
        sel_ids: set[str] = set(getattr(self, "_multi_selected_ids", set()))
        if len(sel_ids) != 1:
            try:
                self.notify("분해는 단일 선택일 때만 가능합니다.", severity="warning")
            except Exception:
                pass
            return
        target_sid = next(iter(sel_ids))
        entry = registry_get(target_sid)
        if not entry.get("is_merged"):
            # 안전 가드 §7 — hard fork 사본인지 식별해 친절 메시지
            forked_from = entry.get("forked_from_merged")
            if forked_from:
                msg = (
                    f"이 세션은 병합 결과의 하드분기 사본입니다. "
                    f"분리는 원본 [{forked_from[:4]}] 에서만 가능합니다."
                )
            else:
                msg = "선택한 세션은 병합 결과가 아닙니다 (is_merged=false)."
            try:
                self.notify(msg, severity="warning")
            except Exception:
                pass
            return

        # pristine 검출 — 신규 작업 있는지
        try:
            pristine = is_merge_pristine(target_sid)
        except Exception:
            pristine = True   # 검출 실패 시 안전 default = pristine 으로 처리

        if pristine:
            # 즉시 전체 삭제
            self._do_unmerge_v2(target_sid, mode="delete")
            return

        # dirty — 모달 띄움
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
                return   # 취소
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
                self.notify(f"분해 모달 띄우기 실패: {exc}", severity="error")
            except Exception:
                pass

    def _do_unmerge_v2(self, target_sid: str, mode: str) -> None:
        """unmerge_new_session 호출 + notify + UI refresh."""
        try:
            ok = unmerge_new_session(target_sid, mode=mode)
        except Exception as exc:
            try:
                self.notify(f"🔧 분해 실패: {exc}", severity="error")
            except Exception:
                pass
            return
        try:
            if ok:
                if mode == "preserve":
                    self.notify(
                        f"🔧 보존 분해 완료: 자식 복귀 + N({target_sid[:8]}) 독립 세션으로 유지"
                    )
                else:
                    self.notify(f"🔧 분해 완료: {target_sid[:8]} → 원본 세션들 복귀")
            else:
                self.notify(f"🔧 분해 실패: {target_sid[:8]}", severity="error")
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
