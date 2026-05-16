"""gccfork — 🔬 깊이 본문 전체 스캔(deep search) 사이드카 모듈.

본 파일은 `gccfork.py` 의 검색 기능 일체를 분리한 것. main 에서:

    from gccfork_search import DeepModeIndicator, DeepSearchMixin

으로 import 하고, App 클래스에 mixin 으로 적용:

    class GCCForkApp(DeepSearchMixin, App):
        ...

App `__init__` 끝에서 `self._init_deep_search_state()` 한 번 호출.

검색 기능 4종:
  1. exact substring (lowercase)
  2. 공백 무시 substring  ("마커검출" ↔ "마커 검출")
  3. 토큰 AND  ("머신 러닝" → 양쪽 다 있는 라인)
  4. fuzzy partial_ratio ≥ 80  (rapidfuzz, 영문 오타 허용)

UI 효과:
  - Knight Rider 진행 배너
  - 매치 세션 라인 전체 옅은 빨간 bg
  - preview 에 ±2턴(총 5턴) 발췌 + alternating bg
  - 압축 전 turn 은 "🚨 압축전" + 빨간 bg (resume 후 LLM 미기억 경고)
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections import defaultdict
from typing import Optional

from collections import defaultdict

from rich.cells import cell_len
from rich.style import Style

from textual import events
from textual._text_area_theme import TextAreaTheme
from textual.color import Color
from textual.containers import VerticalScroll
from textual.widgets import Input, Static, TextArea


class DeepBlockTextArea(TextArea):
    """🔬 매치 블록 TextArea — drag-select + 우클릭 복사 메뉴 위임.

    SelectableTextArea (main 의 클래스) 를 mixin 에서 직접 import 하면 순환
    의존이 생기므로, 동일 동작을 여기에 작은 사본으로 둠.
    """
    ALLOW_SELECT = False

    async def _on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button == 3:
            event.prevent_default()
            if hasattr(self.app, "prepare_context_copy"):
                self.app.prepare_context_copy(self.id, self.selected_text)
            if hasattr(self.app, "remember_active_copy_widget"):
                self.app.remember_active_copy_widget(self.id)
            event.stop()
            return
        if hasattr(self.app, "remember_active_copy_widget"):
            self.app.remember_active_copy_widget(self.id)
        await super()._on_mouse_down(event)

    async def _on_mouse_up(self, event: events.MouseUp) -> None:
        if event.button == 3:
            event.prevent_default()
            if hasattr(self.app, "open_copy_context_menu"):
                self.app.open_copy_context_menu(
                    widget_id=self.id,
                    text=self.selected_text,
                    screen_x=int(event.screen_x or 0),
                    screen_y=int(event.screen_y or 0),
                )
            event.stop()
            return
        await super()._on_mouse_up(event)


# ─── helper — main 의 동일 함수 사본 (순환 import 회피용) ─────────────────
def _extract_text_from_message(message) -> str:
    """Claude `message.content` → 텍스트 합쳐 반환 (str | list[block])."""
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


# ─── 위젯 — 🔬 깊이 인디케이터 ───────────────────────────────────────────
class DeepModeIndicator(Static, can_focus=True):
    """🔬 깊이 인디케이터 — 클릭/Enter/Space 로 deep 모드 토글.

    동작 흐름:
      1. 클릭 → deep 모드 ON (붉은색 시각 변화) + 입력란 포커스
      2. 사용자가 검색어 입력 (incremental 필터 동작 안 함)
      3. Enter → 본문 전체 스캔 시작
      4. 다시 클릭 → deep 모드 OFF (원래 일반 필터 복귀)
    """

    async def _on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button != 1:
            return
        event.prevent_default()
        event.stop()
        self.focus()

    async def _on_mouse_up(self, event: events.MouseUp) -> None:
        if event.button != 1:
            return
        event.prevent_default()
        event.stop()
        if hasattr(self.app, "toggle_deep_search"):
            self.app.toggle_deep_search()

    async def _on_key(self, event: events.Key) -> None:
        if event.key not in {"enter", "space"}:
            return
        event.prevent_default()
        event.stop()
        if hasattr(self.app, "toggle_deep_search"):
            self.app.toggle_deep_search()


# ─── Mixin — App 에 적용해 검색 메서드 결합 ────────────────────────────
class DeepSearchMixin:
    """🔬 깊이 검색 — GCCForkApp 에 mixin 으로 적용.

    Mixin 자체 state 는 `_init_deep_search_state()` 호출 시 초기화.
    main 의 `__init__` 마지막에 한 번 호출하면 됨.
    """

    # State (__init__ 가 없으니 attribute 만 외부에서 설정)
    _deep_mode: bool
    _deep_scan_done: bool
    _deep_search_query: str
    _deep_search_results: set[str]
    _deep_search_in_progress: bool

    def _init_deep_search_state(self) -> None:
        """App `__init__` 끝에서 한 번 호출."""
        self._deep_mode = False
        self._deep_scan_done = False
        self._deep_search_query = ""
        self._deep_search_results = set()
        self._deep_search_in_progress = False
        # 블록 캐시 — (sid, query) → blocks list. 같은 세션 재선택 시 즉시 lookup.
        # 새 스캔 / 모드 종료 시 비움.
        self._deep_blocks_cache: dict[tuple[str, str], list] = {}

    # ─── 모드 토글 ───────────────────────────────────────────────────
    def toggle_deep_search(self) -> None:
        """🔬 깊이 인디케이터 클릭 — 모드 ON/OFF 토글.

        OFF→ON: 시각만 바뀜(붉은색) + 입력란 포커스. 스캔은 안함.
        ON→OFF: 일반 필터로 복귀.
        """
        if self._deep_search_in_progress:
            return
        if self._deep_mode:
            self._exit_deep_mode()
        else:
            self._enter_deep_mode()

    def _enter_deep_mode(self) -> None:
        self._deep_mode = True
        self._deep_scan_done = False
        self._deep_search_results = set()
        self._deep_blocks_cache = {}  # 새 모드 진입 — 캐시 초기화
        # 이전 스캔이 예외로 죽어 in_progress 가 stuck 상태일 수 있음 → 항상 리셋
        self._deep_search_in_progress = False
        try:
            inp = self.query_one("#filter-input", Input)
            inp.add_class("deep-scan")
            ind = self.query_one("#filter-mode-indicator", DeepModeIndicator)
            ind.add_class("deep-scan")
            inp.focus()
        except Exception:
            pass
        self.refresh_list()
        self.notify("🔬 깊이 모드 — 검색어 입력 후 Enter 로 본문 전체 스캔")

    def _exit_deep_mode(self) -> None:
        self._deep_mode = False
        self._deep_scan_done = False
        self._deep_search_query = ""
        self._deep_search_results = set()
        self._deep_blocks_cache = {}  # 모드 종료 — 캐시 비우기
        self._deep_search_in_progress = False  # 다음 진입 시 깨끗한 상태
        try:
            inp = self.query_one("#filter-input", Input)
            inp.remove_class("deep-scan")
            ind = self.query_one("#filter-mode-indicator", DeepModeIndicator)
            ind.remove_class("deep-scan")
        except Exception:
            pass
        self._hide_deep_preview()
        self.refresh_list()

    # ─── Textual 이벤트 핸들러 ────────────────────────────────────────
    def on_input_submitted(self, event: Input.Submitted) -> None:
        """필터 입력 Enter — deep 모드일 때만 본문 스캔 시작."""
        if event.input.id != "filter-input":
            return
        if not self._deep_mode:
            return
        query = event.value.strip()
        if not query:
            self.notify("검색어를 입력한 뒤 Enter 를 누르세요.", severity="warning")
            return
        if self._deep_search_in_progress:
            return
        self._start_deep_search(query)

    # ─── 백그라운드 스캔 ──────────────────────────────────────────────
    def _start_deep_search(self, query: str) -> None:
        """백그라운드에서 모든 세션 jsonl 본문 다중 매칭. Knight Rider UI."""
        self._deep_search_in_progress = True
        self._deep_search_query = query
        self._deep_blocks_cache = {}  # 새 query — 캐시 무효화
        candidates = list(self.sessions)
        n = len(candidates)
        # 5개 노이즈 필터 prefs 스냅샷 — 워커 시작 시점 값으로 고정
        from gccfork_settings import get_deep_prefs_snapshot, get_scannable_text
        prefs = get_deep_prefs_snapshot()
        self._show_progress_banner(f"🔬 본문 전체 스캔 — {n}개 세션 / '{query}' / 다중 매처")
        self._start_scanner_animation()

        def _worker() -> None:
            matched: set[str] = set()
            prebuilt: dict = {}
            try:
                # fuzzy 매처는 6번째 노이즈 필터 — 디폴트 OFF (false positive 큼).
                fuzz = None
                if prefs.get("deep_include_fuzzy", False):
                    from rapidfuzz import fuzz as _fuzz
                    fuzz = _fuzz
                qlow = query.lower()
                qstripped = re.sub(r"\s+", "", qlow)
                qtokens = [t for t in qlow.split() if len(t) >= 2]

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

                # 매치되면 그 자리에서 발췌 블록도 빌드 → 캐시에 저장.
                for idx, sess in enumerate(candidates, 1):
                    try:
                        p = sess.jsonl_path
                        if not p.exists():
                            continue
                        with p.open("r", encoding="utf-8", errors="ignore") as fh:
                            for raw in fh:
                                # 5개 노이즈 필터 적용 — get_scannable_text 가 ''
                                # 반환하면 그 라인은 skip (attachment/file-history/
                                # tool_result/tool_use/system 등 카테고리 OFF 시).
                                try:
                                    obj = json.loads(raw)
                                except json.JSONDecodeError:
                                    continue
                                scannable = get_scannable_text(obj, prefs)
                                if scannable and line_match(scannable):
                                    matched.add(sess.id)
                                    break
                        if sess.id in matched:
                            try:
                                blocks = self._build_deep_match_blocks(sess, query, max_hits=6)
                                if blocks:
                                    prebuilt[(sess.id, query)] = blocks
                            except Exception:
                                pass
                    except OSError:
                        continue
                    except Exception:
                        continue
                    if idx % 5 == 0:
                        try:
                            self.call_from_thread(
                                self._update_stream_text,
                                f"  진행 {idx}/{n}  ·  매치 {len(matched)}개  (다중 매처)",
                            )
                        except Exception:
                            pass
            except Exception as exc:
                # rapidfuzz import 실패 등 워커 전체 예외도 finally 에서 종료 통보
                try:
                    self.call_from_thread(
                        self.notify,
                        f"🔬 스캔 오류: {type(exc).__name__}: {exc}",
                        severity="error",
                    )
                except Exception:
                    pass
            finally:
                # 항상 _finish_deep_search 호출 → in_progress 플래그 해제 + 리스트 갱신.
                # 예외로 matched 가 비어도 scan_done=True 로 마쳐 사용자가 재시도 가능.
                try:
                    self.call_from_thread(self._finish_deep_search, query, matched, prebuilt)
                except Exception:
                    pass

        threading.Thread(target=_worker, daemon=True).start()

    def _finish_deep_search(self, query: str, matched: set[str], prebuilt: dict | None = None) -> None:
        self._deep_search_in_progress = False
        self._stop_scanner_animation()
        self._hide_progress_banner()
        if not self._deep_mode or self._deep_search_query != query:
            return
        self._deep_search_results = matched
        self._deep_scan_done = True
        if prebuilt:
            self._deep_blocks_cache.update(prebuilt)
        self.notify(f"🔬 본문 스캔 완료 — {len(matched)}개 매치", timeout=4)
        self.refresh_list()

    # ─── 발췌 추출 + preview 하이라이트 ───────────────────────────────
    def _extract_deep_match_snippet(
        self, session, query: str, max_hits: int = 6,
    ) -> tuple[str, list[tuple[int, int, bool]]]:
        """매치된 turn 의 ±2턴(총 5턴) 발췌 블록.

        반환: (전체 텍스트, [(start_row, end_row, is_pre_compact), ...])

        매치 정책: 워커와 동일한 다중 매처.
        압축 경계: jsonl 의 마지막 isCompactSummary 이전 turn = pre-compact.
        """
        if not query or not session.jsonl_path.exists():
            return "", []

        from gccfork_settings import get_deep_prefs_snapshot, get_scannable_text
        prefs = get_deep_prefs_snapshot()
        fuzz = None
        if prefs.get("deep_include_fuzzy", False):
            from rapidfuzz import fuzz as _fuzz
            fuzz = _fuzz
        qlow = query.lower()
        qstripped = re.sub(r"\s+", "", qlow)
        qtokens = [t for t in qlow.split() if len(t) >= 2]

        def line_matches(line_low: str) -> bool:
            if not line_low: return False
            if qlow in line_low: return True
            if qstripped and qstripped in re.sub(r"\s+", "", line_low): return True
            if len(qtokens) >= 2 and all(t in line_low for t in qtokens): return True
            if fuzz is not None and len(line_low) < 1500:
                if fuzz.partial_ratio(qlow, line_low) >= 80: return True
            return False

        turns: list[dict] = []
        last_compact_turn_idx = -1
        cur_turn: Optional[dict] = None
        try:
            with session.jsonl_path.open("r", encoding="utf-8", errors="ignore") as fh:
                for raw in fh:
                    try:
                        obj = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    # 5개 노이즈 필터 — scannable=='' 면 매치 후보 아님
                    scannable = get_scannable_text(obj, prefs)
                    line_has_match = line_matches(scannable) if scannable else False
                    if obj.get("isCompactSummary") is True:
                        last_compact_turn_idx = len(turns) - 1
                        continue
                    typ = obj.get("type")
                    is_meta = obj.get("isMeta") or obj.get("isSidechain")
                    msg = obj.get("message") or {}
                    role = msg.get("role") or typ
                    text = _extract_text_from_message(msg)
                    if role == "user" and not is_meta:
                        cur_turn = {
                            "idx": len(turns),
                            "lines": [("user", text)] if text else [],
                            "match": False,
                        }
                        turns.append(cur_turn)
                    elif role == "assistant" and not is_meta and cur_turn is not None and text:
                        cur_turn["lines"].append(("assistant", text))
                    if line_has_match and cur_turn is not None:
                        cur_turn["match"] = True
        except OSError:
            return "", []

        match_turns = [t["idx"] for t in turns if t["match"]]
        if not match_turns:
            return "", []
        match_turns = match_turns[:max_hits]

        # ±2턴 윈도우(총 5턴) 만들고 겹치는 것 병합
        windows: list[tuple[int, int, int]] = []
        for mi in match_turns:
            lo = max(0, mi - 2)
            hi = min(len(turns) - 1, mi + 2)
            if windows and lo <= windows[-1][1] + 1:
                prev_lo, _, prev_mi = windows[-1]
                windows[-1] = (prev_lo, hi, prev_mi)
            else:
                windows.append((lo, hi, mi))

        # 렌더 — 줄바꿈 보존, 메시지당 6줄 / 500자 cap, PAD_WIDTH 200 cell
        lines: list[str] = []
        block_meta: list[tuple[int, int, bool]] = []
        type_marks = {"user": "🧑", "assistant": "🤖"}
        MAX_LINES_PER_MSG = 6
        MAX_CHARS_PER_MSG = 500
        PAD_WIDTH = 200

        def pad(line: str) -> str:
            cur = cell_len(line)
            if cur >= PAD_WIDTH:
                return line
            return line + " " * (PAD_WIDTH - cur)

        for bi, (lo, hi, primary_mi) in enumerate(windows, 1):
            is_pre = (last_compact_turn_idx >= 0 and primary_mi <= last_compact_turn_idx)
            header = f"─── 매치 #{bi}/{len(windows)} (turn {primary_mi+1}{' · 🚨 압축전' if is_pre else ''}) ─────"
            block_start = len(lines)
            lines.append(pad(header))
            for ti in range(lo, hi + 1):
                t = turns[ti]
                marker = "▸" if ti == primary_mi else " "
                for role, text in t["lines"]:
                    mk = type_marks.get(role, "·")
                    label = f"{marker} [t{ti+1:>3}] {mk} "
                    indent = " " * cell_len(label)
                    msg = text[:MAX_CHARS_PER_MSG]
                    if len(text) > MAX_CHARS_PER_MSG:
                        msg = msg + "…"
                    msg_lines = msg.split("\n")
                    truncated = msg_lines[:MAX_LINES_PER_MSG]
                    omitted = len(msg_lines) - MAX_LINES_PER_MSG
                    for i, ml in enumerate(truncated):
                        prefix = label if i == 0 else indent
                        lines.append(pad(prefix + ml.rstrip()))
                    if omitted > 0:
                        lines.append(pad(f"{indent}… ({omitted}줄 더)"))
                    marker = " "
            lines.append(pad(""))
            block_end = len(lines) - 1
            block_meta.append((block_start, block_end, is_pre))
        if lines and lines[-1].strip() == "":
            lines.pop()
        return "\n".join(lines), block_meta

    def _apply_deep_snippet_highlight(self, preview: TextArea, text: str, query: str) -> None:
        """preview 안의 발췌 블록에서 검색어 substring 하이라이트."""
        if not query:
            return
        try:
            if preview.theme != "gccfork-preview-ai":
                app_theme = self.current_theme
                surface = Color.parse(app_theme.surface or app_theme.background or "#202020")
                foreground = Color.parse(app_theme.foreground or "#f3f3f3")
                error_color = Color.parse(getattr(app_theme, "error", None) or "#e01b24")
                deep_match_bg = surface.blend(error_color, 0.55)
                theme = TextAreaTheme(
                    name="gccfork-preview-deep",
                    syntax_styles={
                        "deep-search-match": Style(
                            bgcolor=deep_match_bg.rich_color,
                            color=foreground.rich_color,
                            bold=True,
                        ),
                    },
                )
                preview.register_theme(theme)
                preview.theme = "gccfork-preview-deep"
            qlow = query.lower()
            qlen = len(query)
            lines = text.split("\n")
            in_block = False
            highlights = preview._highlights or defaultdict(list)
            for row, line in enumerate(lines):
                if "🔬 본문 매치 발췌" in line:
                    in_block = True
                    continue
                if in_block and line.startswith("───"):
                    in_block = False
                    continue
                if not in_block:
                    continue
                line_lower = line.lower()
                pos = 0
                while True:
                    idx = line_lower.find(qlow, pos)
                    if idx < 0:
                        break
                    s = len(line[:idx].encode("utf-8"))
                    e = len(line[:idx + qlen].encode("utf-8"))
                    highlights[row].append((s, e, "deep-search-match"))
                    pos = idx + qlen
            preview._highlights = highlights
            preview._line_cache.clear()
            preview.refresh()
        except Exception:
            pass

    # ─── Option D 렌더 — DeepBlockTextArea per-block (drag-select 지원) ──
    def _render_deep_preview(self, session, query: str) -> bool:
        """deep-preview VerticalScroll 안에 블록당 DeepBlockTextArea mount.

        캐시 hit → 즉시 mount, miss → 빌드 후 캐시 저장.
        반환: True = 렌더 성공 (매치 있어서 보여줌), False = 매치 없음.
        """
        try:
            container = self.query_one("#deep-preview", VerticalScroll)
            normal = self.query_one("#preview-text")
        except Exception:
            return False

        # 캐시 우선 — (sid, query) 같은 조합이면 빌드 생략 (jsonl 재파싱 X)
        cache_key = (session.id, query)
        blocks = self._deep_blocks_cache.get(cache_key)
        if blocks is None:
            blocks = self._build_deep_match_blocks(session, query, max_hits=6)
            self._deep_blocks_cache[cache_key] = blocks
        if not blocks:
            return False

        normal.styles.display = "none"
        container.styles.display = "block"
        try:
            for ch in list(container.children):
                ch.remove()
        except Exception:
            pass

        # 검색어 하이라이트용 theme — 한 번만 만들고 모든 블록 위젯에 공유
        match_theme = self._make_deep_block_theme()

        for block in blocks:
            css_class = "deep-block deep-block-pre-compact" if block["is_pre"] else (
                "deep-block deep-block-dim" if block["dim"] else "deep-block deep-block-normal"
            )
            widget = DeepBlockTextArea(
                block["text"],
                classes=css_class,
                read_only=True,
                soft_wrap=True,
                compact=True,
                show_line_numbers=False,
                show_cursor=False,
                highlight_cursor_line=False,
            )
            try:
                container.mount(widget)
                if match_theme is not None:
                    widget.register_theme(match_theme)
                    widget.theme = match_theme.name
                widget._highlights = block["highlights"]
                widget._line_cache.clear()
                widget.refresh()
            except Exception:
                pass
        return True

    def _make_deep_block_theme(self):
        """블록 위젯에 등록할 검색어 하이라이트 theme."""
        try:
            app_theme = self.current_theme
            surface = Color.parse(app_theme.surface or app_theme.background or "#202020")
            foreground = Color.parse(app_theme.foreground or "#f3f3f3")
            error_color = Color.parse(getattr(app_theme, "error", None) or "#e01b24")
            match_bg = surface.blend(error_color, 0.55)
            return TextAreaTheme(
                name="gccfork-deep-block",
                syntax_styles={
                    "deep-search-match": Style(
                        bgcolor=match_bg.rich_color,
                        color=foreground.rich_color,
                        bold=True,
                    ),
                },
            )
        except Exception:
            return None

    def _hide_deep_preview(self) -> None:
        """deep-preview 숨기고 일반 preview 다시 보이게.

        매 update_preview 마다 호출되므로 — 이미 숨겨진 상태면 즉시 return.
        """
        try:
            container = self.query_one("#deep-preview", VerticalScroll)
            # 이미 숨김 + 자식 0 = 노op (일반 모드 selection 변경의 흔한 케이스)
            if container.styles.display == "none" and not container.children:
                return
            normal = self.query_one("#preview-text")
            container.styles.display = "none"
            normal.styles.display = "block"
            for ch in list(container.children):
                ch.remove()
        except Exception:
            pass

    def _build_deep_match_blocks(
        self, session, query: str, max_hits: int = 6,
    ) -> list[dict]:
        """매치된 turn 의 ±2턴 발췌를 블록 리스트로 반환.

        각 블록 dict:
          - "text": 평문 (header + body 라인들) — TextArea 가 그대로 load_text
          - "highlights": defaultdict(row → [(start_byte, end_byte, "deep-search-match")])
          - "is_pre": bool (압축전 여부 — CSS 빨간 bg + border)
          - "dim": bool (alternating bg 의 "흐린" 차례)
        """
        if not query or not session.jsonl_path.exists():
            return []
        from gccfork_settings import get_deep_prefs_snapshot, get_scannable_text
        prefs = get_deep_prefs_snapshot()
        fuzz = None
        if prefs.get("deep_include_fuzzy", False):
            from rapidfuzz import fuzz as _fuzz
            fuzz = _fuzz
        qlow = query.lower()
        qstripped = re.sub(r"\s+", "", qlow)
        qtokens = [t for t in qlow.split() if len(t) >= 2]

        def line_matches(line_low: str) -> bool:
            if not line_low: return False
            if qlow in line_low: return True
            if qstripped and qstripped in re.sub(r"\s+", "", line_low): return True
            if len(qtokens) >= 2 and all(t in line_low for t in qtokens): return True
            if fuzz is not None and len(line_low) < 1500:
                if fuzz.partial_ratio(qlow, line_low) >= 80: return True
            return False

        turns: list[dict] = []
        last_compact_turn_idx = -1
        cur_turn: Optional[dict] = None
        try:
            with session.jsonl_path.open("r", encoding="utf-8", errors="ignore") as fh:
                for raw in fh:
                    try:
                        obj = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    # 5개 노이즈 필터 — 워커와 동일
                    scannable = get_scannable_text(obj, prefs)
                    line_has_match = line_matches(scannable) if scannable else False
                    if obj.get("isCompactSummary") is True:
                        last_compact_turn_idx = len(turns) - 1
                        continue
                    typ = obj.get("type")
                    is_meta = obj.get("isMeta") or obj.get("isSidechain")
                    msg = obj.get("message") or {}
                    role = msg.get("role") or typ
                    text = _extract_text_from_message(msg)
                    if role == "user" and not is_meta:
                        cur_turn = {
                            "idx": len(turns),
                            "lines": [("user", text)] if text else [],
                            "match": False,
                        }
                        turns.append(cur_turn)
                    elif role == "assistant" and not is_meta and cur_turn is not None and text:
                        cur_turn["lines"].append(("assistant", text))
                    if line_has_match and cur_turn is not None:
                        cur_turn["match"] = True
        except OSError:
            return []

        match_turns = [t["idx"] for t in turns if t["match"]][:max_hits]
        if not match_turns:
            return []

        # ±2 windows + merge overlapping
        windows: list[tuple[int, int, int]] = []
        for mi in match_turns:
            lo = max(0, mi - 2)
            hi = min(len(turns) - 1, mi + 2)
            if windows and lo <= windows[-1][1] + 1:
                prev_lo, _, prev_mi = windows[-1]
                windows[-1] = (prev_lo, hi, prev_mi)
            else:
                windows.append((lo, hi, mi))

        type_marks = {"user": "🧑", "assistant": "🤖"}
        MAX_LINES_PER_MSG = 6
        MAX_CHARS_PER_MSG = 500
        qlow = query.lower()
        qlen = len(query)
        blocks: list[dict] = []
        for bi, (lo, hi, primary_mi) in enumerate(windows, 1):
            is_pre = (last_compact_turn_idx >= 0 and primary_mi <= last_compact_turn_idx)
            header_text = (
                f"━━ 매치 #{bi}/{len(windows)} · turn {primary_mi+1}"
                + (" · 🚨 압축전 (resume 후 AI 미기억)" if is_pre else "")
                + " ━━"
            )
            text_lines: list[str] = [header_text]
            for ti in range(lo, hi + 1):
                t = turns[ti]
                marker = "▸ " if ti == primary_mi else "  "
                for role, text_msg in t["lines"]:
                    mk = type_marks.get(role, "·")
                    label = f"{marker}[t{ti+1:>3}] {mk} "
                    indent = " " * cell_len(label)
                    msg = text_msg[:MAX_CHARS_PER_MSG]
                    if len(text_msg) > MAX_CHARS_PER_MSG:
                        msg = msg + "…"
                    msg_lines = msg.split("\n")
                    truncated = msg_lines[:MAX_LINES_PER_MSG]
                    omitted = len(msg_lines) - MAX_LINES_PER_MSG
                    for i, ml in enumerate(truncated):
                        prefix = label if i == 0 else indent
                        text_lines.append(prefix + ml.rstrip())
                    if omitted > 0:
                        text_lines.append(f"{indent}… ({omitted}줄 더)")
                    marker = "  "
            full_text = "\n".join(text_lines)

            # 검색어 substring 위치를 byte range 로 매핑 (TextArea highlights 형식)
            highlights: defaultdict = defaultdict(list)
            for row, line in enumerate(text_lines):
                line_lower = line.lower()
                pos = 0
                while True:
                    idx = line_lower.find(qlow, pos)
                    if idx < 0:
                        break
                    s = len(line[:idx].encode("utf-8"))
                    e = len(line[:idx + qlen].encode("utf-8"))
                    highlights[row].append((s, e, "deep-search-match"))
                    pos = idx + qlen

            blocks.append({
                "text": full_text,
                "highlights": highlights,
                "is_pre": is_pre,
                "dim": (bi % 2 == 0),
            })
        return blocks

    def _apply_deep_block_bg(
        self, preview: TextArea, text: str, block_meta: list[tuple[int, int, bool]],
    ) -> None:
        """발췌 블록마다 alternating bg + 압축전 블록은 빨간 bg."""
        if not block_meta:
            return
        try:
            app_theme = self.current_theme
            surface = Color.parse(app_theme.surface or app_theme.background or "#202020")
            foreground = Color.parse(app_theme.foreground or "#f3f3f3")
            error_color = Color.parse(getattr(app_theme, "error", None) or "#e01b24")
            dim_bg = surface.blend(foreground, 0.07)
            pre_compact_bg = surface.blend(error_color, 0.20)
            cur_theme_name = preview.theme
            theme = preview._themes.get(cur_theme_name)
            if theme is None:
                return
            ss = dict(theme.syntax_styles)
            ss["deep-block-dim"] = Style(
                bgcolor=dim_bg.rich_color,
                color=foreground.rich_color,
            )
            ss["deep-block-pre-compact"] = Style(
                bgcolor=pre_compact_bg.rich_color,
                color=foreground.rich_color,
            )
            new_theme = TextAreaTheme(
                name=cur_theme_name,
                cursor_line_style=theme.cursor_line_style,
                syntax_styles=ss,
            )
            preview.register_theme(new_theme)
            preview.theme = cur_theme_name

            highlights = preview._highlights or defaultdict(list)
            lines = text.split("\n")
            for bi, (start, end, is_pre) in enumerate(block_meta):
                style_name = "deep-block-pre-compact" if is_pre else (
                    "deep-block-dim" if bi % 2 == 1 else None
                )
                if style_name is None:
                    continue
                for row in range(start, end + 1):
                    if row >= len(lines):
                        break
                    line_end = len(lines[row].encode("utf-8"))
                    if line_end > 0:
                        highlights[row].insert(0, (0, line_end, style_name))
            preview._highlights = highlights
            preview._line_cache.clear()
            preview.refresh()
        except Exception:
            pass
