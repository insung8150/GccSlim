"""gccfork_unfold — 🪚 해제 (segmented compression) 사이드카 모듈.

Claude Code 의 auto-compact 마커 (isCompactSummary / "Compacted from") 를
segment boundary 로 사용해 각 segment 를 독립적으로 풀 슬림. 마지막
segment 만 keep-recent 보호 (현재 작업 영역).

배경 (2026-05-04 ca09 실험으로 검증):
  - 단순 슬림 1번 :  21.9 MB → 20.5 MB  (-6.3%)
  - 🪚 해제      :  21.9 MB →  5.27 MB (-75.9%)
  - **12배 효율적**

이유: 옛 segment 는 keep-recent 보호 영역이 0 이라 strong slim 의
진짜 위력이 발휘됨. 가장 자주 발생하는 큰 tool_result 들이 모두 drop.

원칙:
  P1. **활성 세션 거부** — 현재 PID 가 쓰고 있으면 ActiveSessionUnfoldError
  P2. **백업 자동** — `.bak.<ts>.unfold.jsonl` 으로 슬림 직전 상태 보존
  P3. **boundary 보존** — compact marker 라인 자체는 KEEP (검증 가능)
  P4. **마지막 segment 보호** — keep_recent_turns 옵션으로 작업 영역 안전
  P5. **idempotent** — 압축 0개면 no-op (안내 후 종료)
  P6. **atomic** — tmp → os.replace, 실패 시 백업으로 롤백
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Textual imports — required by UnfoldConfirmScreen (modal class) below.
# Re-introduced 2026-05-07 after the `🪚 해제` button raised NameError on
# ModalScreen at module import time. The Phase E (2026-05-06) Python-archive
# pass had moved the algorithm body out of this file and inadvertently took
# these imports with it; the modal class itself stayed and still needs them.
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, RadioButton, RadioSet, Static

from gccfork_sessions import (
    Session,
    all_active_sid_pid_map,
)


# ── 에러 ─────────────────────────────────────────────────────────────────
class ActiveSessionUnfoldError(ValueError):
    """활성 Claude 세션을 해제하려 시도. /quit 후 다시 시도하라는 안내."""
    def __init__(self, sid: str):
        self.sid = sid
        super().__init__(
            f"활성 Claude 세션 {sid[:8]} 은 🪚 해제 할 수 없음. "
            f"먼저 /quit 으로 종료 후 다시 시도하세요."
        )


class NoCompactionFoundError(ValueError):
    """jsonl 에 auto-compact 마커가 0개 — 해제할 게 없음."""
    pass


# ── boundary 검출 ────────────────────────────────────────────────────────
@dataclass
class CompactBoundary:
    """검출된 auto-compact 이벤트 메타."""
    line_idx: int            # jsonl 안의 0-based 라인 번호
    timestamp: str           # 메시지의 timestamp (없으면 빈 문자열)
    detect_method: str       # "isCompactSummary" | "continuation_text" | "compacted_text"
    summary: str             # 첫 80자 미리보기 (UI 표시용)


_CONT_PATTERNS = (
    "This session is being continued from a previous conversation",
    "Previous conversation that ran out of context",
)
_COMPACTED_PATTERNS = (
    "Compacted from",
    "compacted summary",
)


def _extract_text(obj: dict) -> str:
    """메시지 dict 에서 사용자에게 보일 텍스트 추출 (첫 줄만 빠르게).

    content 가 list 일 때 각 blk 의 text 가 또 list 인 경우 (예: tool_result
    의 content) 도 안전하게 string 으로 평탄화.
    """
    msg = obj.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for blk in content:
            if isinstance(blk, dict):
                t = blk.get("text") or blk.get("content") or ""
                if isinstance(t, str):
                    parts.append(t)
                elif isinstance(t, list):
                    # nested — 재귀 평탄화
                    for sub in t:
                        if isinstance(sub, dict):
                            st = sub.get("text") or sub.get("content") or ""
                            if isinstance(st, str):
                                parts.append(st)
                        elif isinstance(sub, str):
                            parts.append(sub)
            elif isinstance(blk, str):
                parts.append(blk)
        return "\n".join(parts)
    return ""


def detect_compact_boundaries(jsonl_path: Path) -> list[CompactBoundary]:
    """auto-compact 이벤트 라인 검출. 시간순 정렬.

    엄격 모드 — `isCompactSummary: True` 플래그만 신뢰.
    텍스트 매칭 (CONT / Compacted) 은 false positive (search 결과 등에서
    우연히 phrase 가 포함되는 경우) 가 빈번해 제거됨 (2026-05-05 발견).

    Claude Code 가 진짜 auto-compact 시 isCompactSummary 를 항상 set 함 →
    이 한 가지로 충분.
    """
    out: list[CompactBoundary] = []
    if not jsonl_path.exists():
        return out

    with jsonl_path.open("r", encoding="utf-8", errors="ignore") as fh:
        for i, raw in enumerate(fh):
            try:
                obj = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("isCompactSummary") is not True:
                continue

            ts = obj.get("timestamp") or ""
            text_preview = _extract_text(obj).replace("\n", " ").strip()[:80]
            out.append(CompactBoundary(
                line_idx=i,
                timestamp=ts,
                detect_method="isCompactSummary",
                summary=text_preview,
            ))

    out.sort(key=lambda b: b.line_idx)
    return out


# ── segment 분할 + 슬림 ─────────────────────────────────────────────────
@dataclass
class SegmentStats:
    """한 segment 의 슬림 전후 통계."""
    idx: int                 # 0-based segment 번호
    is_last: bool            # 마지막 segment 여부 (보호 대상)
    line_start: int          # 원본 jsonl 의 시작 라인 (inclusive)
    line_end: int            # 끝 라인 (exclusive)
    bytes_before: int
    bytes_after: int
    lines_before: int
    lines_after: int
    drop_count: int
    keep_count: int

    @property
    def reduction_pct(self) -> float:
        if self.bytes_before == 0:
            return 0.0
        return (1 - self.bytes_after / self.bytes_before) * 100



# ─── Phase E archive (unfold_python.py 로 이동) ───────────────
# unfold_session + _slim_segment_lines + format_unfold_summary
# 호출은 _call_rust_unfold_inplace() (Rust subprocess) 로.

class UnfoldResult:
    """🪚 해제 작업 결과."""
    boundaries: list[CompactBoundary]
    segments: list[SegmentStats]
    bytes_before: int
    bytes_after: int
    backup_path: Optional[Path]
    elapsed_sec: float

    @property
    def total_reduction_pct(self) -> float:
        if self.bytes_before == 0:
            return 0.0
        return (1 - self.bytes_after / self.bytes_before) * 100


class UnfoldConfirmScreen(ModalScreen):
    """🪚 해제 확인 모달 — boundary 개수 표시 + 전체/취소.

    UI:
      ┌─ 🪚 해제 ──────────────────────────────┐
      │ 세션: ca09 (22.2 MB)                   │
      │ auto-compact 마커 N개 발견             │
      │                                        │
      │ ◉ 전체 해제 (옛 segment 모두 풀 슬림)  │
      │ ○ 마지막 빼고                          │
      │ ○ 취소                                 │
      │                                        │
      │ [취소]                       [실행]    │
      └────────────────────────────────────────┘
    """

    DEFAULT_CSS = """
    UnfoldConfirmScreen {
        align: center middle;
    }
    #unfold-box {
        width: 92;
        height: auto;
        max-height: 90%;
        border: round $accent 35%;
        padding: 0;
        background: $surface;
    }
    #unfold-header {
        padding: 1 2;
        height: auto;
        border-bottom: hkey $accent 20%;
    }
    #unfold-title {
        color: $accent;
        text-style: bold;
    }
    #unfold-meta {
        color: $foreground 60%;
    }
    #unfold-body {
        padding: 1 2;
        height: auto;
    }
    #unfold-body RadioSet {
        background: transparent;
        border: none;
        height: auto;
    }
    #unfold-body RadioButton {
        background: $accent 5%;
        margin: 0 0 1 0;
        padding: 0 1;
        height: auto;
    }
    #unfold-body RadioButton:hover {
        background: $accent 10%;
    }
    #unfold-body RadioButton.-selected {
        background: $accent 16%;
    }
    #bundle-options {
        background: $accent 3%;
        border: round $accent 20%;
        padding: 1 2;
        margin: 0 0 1 0;
        height: auto;
    }
    #bundle-options.-disabled {
        background: $surface;
        border: round $accent 8%;
        opacity: 40%;
    }
    #bundle-options Static.opt-label {
        color: $foreground 70%;
        margin: 0 0 1 0;
    }
    #bundle-options.-disabled Static.opt-label {
        color: $foreground 35%;
    }
    #bundle-options RadioSet {
        margin: 0 0 1 0;
        height: auto;
    }
    #bundle-options RadioButton {
        background: $accent 5%;
        margin: 0 1 0 0;
        padding: 0 1;
    }
    #bundle-options.-disabled RadioButton {
        background: $accent 3%;
    }
    .desc {
        color: $foreground 55%;
        padding: 0 0 0 4;
    }
    #unfold-btn-row {
        padding: 1 2;
        height: auto;
        border-top: hkey $accent 20%;
    }
    #unfold-btn-row Button {
        margin: 0 1;
    }
    .spacer { width: 1fr; }
    """

    def __init__(self, session: Session, boundaries: list[CompactBoundary],
                 allow_bundle: bool = True) -> None:
        super().__init__()
        self.session = session
        self.boundaries = boundaries
        self.allow_bundle = allow_bundle

    def compose(self) -> ComposeResult:
        sz = self.session.jsonl_path.stat().st_size
        sz_mb = sz / 1024 / 1024
        n = len(self.boundaries)
        # 예상 절감 추정 (이전 검증 결과 기반)
        est_inplace_mb = sz_mb * 0.24  # -76%
        est_inplace_protect_mb = sz_mb * 0.27  # -73% (S3 보호)
        est_bundle_mb = sz_mb * 0.04  # -96%

        with Vertical(id="unfold-box"):
            with Vertical(id="unfold-header"):
                yield Static(f"🪚 해제 — {self.session.id[:8]}", id="unfold-title")
                yield Static(
                    f"{sz_mb:.1f} MB · auto-compact 마커 {n}개 · 모드 선택",
                    id="unfold-meta",
                )
            with Vertical(id="unfold-body"):
                with RadioSet(id="unfold-mode"):
                    yield RadioButton(
                        f"in-place 풀 해제  (예상: {sz_mb:.1f}MB → {est_inplace_mb:.1f}MB · -76%)",
                        value=True,
                        id="rb-all",
                    )
                    yield RadioButton(
                        f"in-place + 마지막 보호  (예상: {sz_mb:.1f}MB → {est_inplace_protect_mb:.1f}MB · -73%)",
                        id="rb-except-last",
                    )
                    yield RadioButton(
                        f"🪚 번들 모드 (새 sid)  (예상: {sz_mb:.1f}MB → {est_bundle_mb:.2f}MB · -96%, 인식률 96.8%)",
                        id="rb-bundle",
                    )

                # 모드별 설명
                yield Static(
                    "  • 풀 해제: 같은 sid 유지, 마커 보존 — Resume 시 마지막 segment 만 active context\n"
                    "  • + 마지막 보호: 같은 sid, 최근 5턴 KEEP — 작업 중단 없이 청소 (default)\n"
                    "  • 🪚 번들: 새 sid 트리 자식 등장, 모든 옛 작업 archive injection\n"
                    "      → 96.8% 인식 (Opus 1M context 검증), 컨텍스트 40% / 활성 60% 여유",
                    classes="desc",
                )

                # 번들 모드 세부 옵션
                with Vertical(id="bundle-options"):
                    yield Static("🪚 번들 모드 세부 (위에서 번들 선택 시 적용):",
                                 classes="opt-label")
                    yield Static("번들 크기 (turn 묶을 token 단위):", classes="opt-label")
                    with RadioSet(id="bundle-size"):
                        yield RadioButton("12K (작은 번들 — 빠른 head 도달)", id="bs-12k")
                        yield RadioButton("18K (권장)", value=True, id="bs-18k")
                        yield RadioButton("25K (큰 번들 — 풍부한 컨텍스트)", id="bs-25k")
                    yield Static("최근 turn 보호 수:", classes="opt-label")
                    with RadioSet(id="recent-keep"):
                        yield RadioButton("3 turn", id="rk-3")
                        yield RadioButton("5 turn (권장)", value=True, id="rk-5")
                        yield RadioButton("10 turn (작업 중간)", id="rk-10")

            with Horizontal(id="unfold-btn-row"):
                yield Button("취소", id="btn-unfold-cancel")
                yield Static("", classes="spacer")
                yield Button("🪚 실행", id="btn-unfold-go", variant="warning")

    def on_mount(self) -> None:
        # 초기 상태 — 번들 모드 미선택이라 세부 옵션 비활성화
        self._update_bundle_options_state("rb-all")

    def _update_bundle_options_state(self, choice_id: Optional[str]) -> None:
        """번들 모드 세부 옵션의 활성/비활성 상태 토글."""
        try:
            box = self.query_one("#bundle-options", Vertical)
        except Exception:
            return
        is_bundle = (choice_id == "rb-bundle")
        try:
            box.set_class(not is_bundle, "-disabled")
        except Exception:
            pass
        # RadioSet / RadioButton 자체도 disabled 속성 설정
        try:
            for rb in self.query("#bundle-options RadioButton"):
                rb.disabled = not is_bundle
        except Exception:
            pass

    def on_radio_set_changed(self, event) -> None:
        # 메인 모드 라디오 변경 감지 → 세부 옵션 활성화 토글
        if getattr(event.radio_set, "id", None) == "unfold-mode":
            pressed = event.radio_set.pressed_button
            choice = pressed.id if pressed else None
            self._update_bundle_options_state(choice)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn-unfold-cancel":
            self.dismiss(None)
            return
        if bid == "btn-unfold-go":
            mode_set = self.query_one("#unfold-mode", RadioSet)
            pressed = mode_set.pressed_button
            choice = (pressed.id if pressed else "rb-all")

            # 번들 모드 세부 옵션
            bundle_size = 18_000
            recent_keep = 5
            try:
                bs_set = self.query_one("#bundle-size", RadioSet)
                bs_pressed = bs_set.pressed_button
                if bs_pressed:
                    bundle_size = {
                        "bs-12k": 12_000, "bs-18k": 18_000, "bs-25k": 25_000,
                    }.get(bs_pressed.id, 18_000)
            except Exception:
                pass
            try:
                rk_set = self.query_one("#recent-keep", RadioSet)
                rk_pressed = rk_set.pressed_button
                if rk_pressed:
                    recent_keep = {
                        "rk-3": 3, "rk-5": 5, "rk-10": 10,
                    }.get(rk_pressed.id, 5)
            except Exception:
                pass

            self.dismiss({
                "choice": choice,
                "bundle_size": bundle_size,
                "recent_keep": recent_keep,
            })


