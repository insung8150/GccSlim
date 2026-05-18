"""gccfork — ⚙ 설정 모달 + 섹션 레지스트리 (사이드카 모듈).

main 의 사용:
    from gccfork_settings import SettingsScreen, get_deep_prefs_snapshot, get_scannable_text

    # 액션바 ⚙ 버튼 핸들러:
    self.push_screen(SettingsScreen())

    # 딥검색 워커:
    prefs = get_deep_prefs_snapshot()
    scannable = get_scannable_text(obj, prefs)   # '' 면 그 라인은 노이즈로 간주, skip
    if scannable and line_match(scannable):
        ...

새 설정 섹션 추가 절차:
    1. `get_settings_sections()` 의 리스트에 dict 한 개 추가
       - "type": "checkboxes" 면 items: [{key, label, hint, default}, ...]
       - "type": "text" 면 content: "<읽기 전용 본문>"
    2. 체크박스 키는 그대로 prefs 키 → 코드에서 `pref_get(key, default)` 로 읽기
    3. SettingsScreen 의 compose 는 자동으로 새 섹션을 렌더 (수정 불필요)

딥검색 5개 노이즈 카테고리(2026-04-27 추가):
    - attachment            : Claude Code 자동 첨부 메타 (파일명/경로)
    - file-history-snapshot : cwd 파일 스냅샷
    - tool_result           : ls/find/git 등 도구 출력
    - tool_use args         : tool_use 의 input(command/path 등)
    - system / 내부 메시지   : system, isMeta, isSidechain, <system-reminder>, /command 입력, thinking
"""
from __future__ import annotations

import json
import re
import sys

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, RadioButton, RadioSet, Static, TextArea


# main 의 INTERNAL_USER_PREFIXES 와 동일 (parse_session 에서 같은 문자열 사용).
# user 메시지가 이 prefix 로 시작하면 system 자동 텍스트로 간주.
_INTERNAL_USER_PREFIXES = (
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<local-command-stdout>",
    "<bash-stdout>",
    "<bash-stderr>",
    "Caveat: The messages below were generated",
    "<system-reminder>",
)


class SettingsBrainButton(Static, can_focus=True):
    """One-line brain button for settings panes."""

    def __init__(self, target: str) -> None:
        super().__init__("🧠 지능", id=f"btn-settings-brain-{target}")
        self.target = target

    async def on_click(self) -> None:
        screen = self.screen
        if hasattr(screen, "_open_settings_brain_agent"):
            screen._open_settings_brain_agent(self.target)

    async def _on_key(self, event: events.Key) -> None:
        if event.key in {"enter", "space"}:
            event.prevent_default()
            event.stop()
            screen = self.screen
            if hasattr(screen, "_open_settings_brain_agent"):
                screen._open_settings_brain_agent(self.target)


# ─── 딥검색 노이즈 필터 5개 ────────────────────────────────────────────
# default=False (체크 해제 = 노이즈 필터링 ON = 매처에서 제외).
# 체크하면 그 카테고리도 매치 대상에 포함 (옛 동작 복원).
DEEP_SEARCH_ITEMS: list[dict] = [
    {
        "key": "deep_include_attachment",
        "label": "[attachment] 라인 매치에 포함",
        "hint": "Claude Code 가 자동 첨부한 파일 메타. 파일명/경로가 박혀 있어 노이즈가 큼.",
        "default": False,
    },
    {
        "key": "deep_include_file_history",
        "label": "[file-history-snapshot] 매치에 포함",
        "hint": "세션 시작/재개 시 cwd 파일 스냅샷. 파일명 substring 매치가 흔함.",
        "default": False,
    },
    {
        "key": "deep_include_tool_result",
        "label": "tool_result 본문 매치에 포함  (ls/find/git/cat 출력 등)",
        "hint": "도구 출력에 파일명/디렉토리 리스팅이 들어와 무관 매치가 발생.",
        "default": False,
    },
    {
        "key": "deep_include_tool_use_args",
        "label": "tool_use 인자(command/path/...) 매치에 포함",
        "hint": "Bash 의 command, Read/Edit 의 file_path 등 도구 호출 메타.",
        "default": False,
    },
    {
        "key": "deep_include_system_internal",
        "label": "system / <system-reminder> / 내부 메시지 매치에 포함",
        "hint": "system 자동 텍스트, /command 입력, isMeta/isSidechain, thinking 블록.",
        "default": False,
    },
    {
        "key": "deep_include_fuzzy",
        "label": "fuzzy 매치 허용  (rapidfuzz partial_ratio ≥ 80)",
        "hint": "비슷한 단어도 매치. 예: 'altera' 검색이 'alternate-screen' 매치 가능 → 디폴트 OFF.",
        "default": False,
    },
]


# ─── 슬림 모드 — 3개 프리셋 (strong / medium / weak, 한국어 강중약) ────
# 각 모드 × 5 카테고리 = 15 prefs 키. KEEP=True 면 그 카테고리 라인을
# (또는 stub 으로) 보존, False 면 DROP.
#
# 디폴트 정책:
#   strict:    "지금의 구조" — 5개 모두 False (텍스트만 보존, 가장 작은 슬림)
#   balanced:  텍스트 + tool_use 한 줄 + system_internal 부분 + 에러 tool_result
#   loose:     balanced + 첫 attachment + 짧은 tool_result 도 보존
SLIM_CATEGORIES: list[dict] = [
    {
        "cat": "attachment",
        "label": "[attachment] 보존 (첫 첨부 1줄 stub)",
        "hint": "사용자가 무엇을 컨텍스트에 처음 넣었는지 — 반복 재첨부는 항상 제외.",
    },
    {
        "cat": "file_history",
        "label": "[file-history-snapshot] 보존",
        "hint": "cwd 파일 변경 스냅샷. 보통 노이즈, KEEP 시에도 1줄 stub 으로.",
    },
    {
        "cat": "tool_result",
        "label": "tool_result 보존 (에러 우선, 짧은 출력)",
        "hint": "Bash/ls/cat 등 도구 출력. 에러는 항상, 성공은 ≤200자만 보존.",
    },
    {
        "cat": "tool_use_args",
        "label": "tool_use 인자 보존 (이름 + 짧은 input)",
        "hint": "AI 가 무엇을 호출했는지 trace. name + 첫 80자만 1줄 요약.",
    },
    {
        "cat": "system_internal",
        "label": "system / thinking / 슬래시 명령 보존",
        "hint": "thinking 첫 1문장, /compact 같은 사용자 명시 명령, 압축 요약.",
    },
]

SLIM_MODE_DEFAULTS: dict[str, dict[str, bool]] = {
    "strong": {"attachment": False, "file_history": False, "tool_result": False, "tool_use_args": False, "system_internal": False},
    "medium": {"attachment": False, "file_history": False, "tool_result": True,  "tool_use_args": True,  "system_internal": True},
    "weak":   {"attachment": True,  "file_history": True,  "tool_result": True,  "tool_use_args": True,  "system_internal": True},
}

SLIM_MODE_LABELS: dict[str, tuple[str, str]] = {
    "strong": ("🔻 슬림 (strong)", "텍스트만 보존 — 가장 작음 (~3% 원본). 디폴트."),
    "medium": ("🔻 슬림 (medium)", "텍스트 + 도구 호출 trace + thinking 첫 문장 — 흐름 가독성 (~15% 원본)."),
    "weak":   ("🔻 슬림 (weak)",   "medium + 첨부 + 파일 history — 보수적 슬림 (~50% 원본)."),
}

CODEX_SLIM_MODE_LABELS: dict[str, tuple[str, str]] = {
    "safe": (
        "안전 모드",
        "이전 compact 요약 전부 + 현재 slim 본문 + 최근 raw 30턴을 보존합니다. 복구/검토 우선입니다.",
    ),
    "strong": (
        "강력 모드",
        "이전 compact 요약 전부 + 현재 slim 본문 + 최근 raw 3턴만 보존합니다. 컨텍스트 확보 우선입니다.",
    ),
}

CODEX_SLIM_MODE_DEFAULT_KEEP: dict[str, int] = {
    "safe": 30,
    "strong": 3,
}

# 옛 키 마이그레이션 alias (CLI / 옛 registry 호환용)
SLIM_MODE_ALIASES: dict[str, str] = {
    "strict": "strong",
    "balanced": "medium",
    "loose": "weak",
}

# claude `/slim` 슬래시명령이 gccfork TUI 에 위임할 때 참조하는 기본값.
# 사용자는 SettingsScreen 의 [🔻 슬림] 탭에서 변경 가능.
#
# 보호 턴은 **모드별 차등** — 강도와 보호 강도가 합리적 그라데이션:
#   strong → 5 턴   (강하게 자름. 보호 짧음)
#   medium → 10 턴  (균형)
#   weak   → 30 턴  (많이 보존. 보호 길음)
# claude `/slim` 호출 시 `slim_default_mode` 가 가리키는 모드의 turns 키 조회.
SLIM_DEFAULT_PREFS: dict[str, "str | int | bool"] = {
    "slim_default_mode": "strong",                # strong | medium | weak
    "slim_default_reload": True,                  # True 면 슬림 후 자동 resume, False 면 disk 만 슬림
    "slim_strong_keep_recent_turns": 5,
    "slim_medium_keep_recent_turns": 10,
    "slim_weak_keep_recent_turns": 30,
    "slim_default_anti_fragmentation": True,      # True 면 번들(묶음) 처리 (bundle 구조, in-place) — Claude 인식률 향상 권장값
    "slim_default_dynamic_cap": True,             # True 면 jsonl 크기 측정해 cap 자동 조정 (1M context 안에)
    "slim_default_visible_cap_compact": True,     # True 면 context 비포함 영역을 native compact marker 앞으로 (cap_overflow 시)
    "slim_default_send_other_env": False,         # True 면 다른 환경 (VSCode bridge / gnome-terminal) 로 보내기 디폴트
    "slim_default_newtab": False,                 # True 면 새 탭으로 열기, False 면 새 창
    "codex_slim_default_mode": "strong",          # safe | strong
    "codex_slim_keep_recent": 3,                  # Codex /slim 기본 최근 user turn 보존 수
    "codex_slim_include_compact_summaries": True, # 이전 compact/압축 요약을 새 컨텍스트에 포함
    "codex_slim_default_clone": False,            # True 면 원본 보존 slim 복제본 생성
    "codex_slim_default_reload": True,            # True 면 슬림 후 자동 열기/재시작
    "codex_slim_default_send_other_env": False,
    "codex_slim_default_newtab": False,
}


def _slim_items_for_mode(mode: str) -> list[dict]:
    """3 모드 × 5 카테고리 = 15개 prefs 항목을 한 모드분 빌드."""
    defaults = SLIM_MODE_DEFAULTS[mode]
    return [
        {
            "key": f"slim_{mode}_keep_{c['cat']}",
            "label": c["label"],
            "hint": c["hint"],
            "default": defaults[c["cat"]],
        }
        for c in SLIM_CATEGORIES
    ]



# ─── Phase A archive (2026-05-06) ───────────────────────────────────────
# get_slim_mode_prefs + slim_line_verdict + _stub_* helpers 모두
# bin/_archive_2026-05-06_phase_a_python/verdict_python.py 로 이동.
# Rust 단일 처리 — 호출은 ~/.local/bin/gccfork 의 _call_rust_slim_general()
# (subprocess) 으로 라우팅됨.

def get_settings_sections() -> list[dict]:
    """섹션 레지스트리 — 호출 시점 빌드 (HelpScreen import 가 module load 시점에 실패해도 안전).

    섹션 dict 키:
      - id: 식별자 (CSS / 추후 라우팅용)
      - title: 헤더 표시 텍스트
      - type: "checkboxes" | "text"
      - intro: (선택) 헤더 아래 설명 한 줄
      - items: (checkboxes) [{key, label, hint, default}, ...]
      - content: (text) 읽기 전용 본문
    """
    out: list[dict] = [
        {
            "id": "deep-search",
            "title": "🔬 딥검색 — 본문 매처 노이즈 필터",
            "type": "checkboxes",
            "intro": (
                "체크 = 매치에 포함 (노이즈 허용)  /  체크 해제 = 매처에서 제외 (필터링).\n"
                "디폴트: 5개 모두 해제 → 사용자가 실제 입력한 본문에서만 매치됨."
            ),
            "items": DEEP_SEARCH_ITEMS,
        },
    ]
    # 슬림 3개 모드 — 각 모드 × 5 카테고리 = 15 체크박스 (자동 생성)
    for mode in ("strong", "medium", "weak"):
        title, intro = SLIM_MODE_LABELS[mode]
        out.append({
            "id": f"slim-{mode}",
            "title": title,
            "type": "checkboxes",
            "intro": (
                f"{intro}\n"
                "체크 = 그 카테고리도 (stub 으로) 보존  /  체크 해제 = 완전 DROP."
            ),
            "items": _slim_items_for_mode(mode),
        })
    # 🗂 Archive 섹션 — 현재 prefs 값 read-only 표시 (RadioSet UI 는 추후)
    out.append({
        "id": "archive",
        "title": "🗂 Archive (병합) — 현재 설정",
        "type": "text",
        "content": _build_archive_settings_text(),
    })
    out.append({
        "id": "help",
        "title": "❓ 도움말",
        "type": "text",
        "content": _load_help_text(),
    })
    return out


# ─── 🗂 Archive 옵션 spec — settings UI 가 사용 ────────────────────────
# 각 옵션:
#   key    — prefs key (gccfork_archive.ARCHIVE_DEFAULTS 의 키와 일치)
#   kind   — "radio" (enum) | "bool" (Checkbox)
#   label  — UI 표시 라벨
#   hint   — 설명 (선택)
#   choices — radio 의 경우 [(value, label), ...]
ARCHIVE_OPTIONS: list[dict] = [
    {
        "key": "archive_preview_mode",
        "kind": "radio",
        "label": "Preview 통합 방식",
        "hint": "부모 미리보기에 archive 자식들을 어떻게 보여줄지",
        "choices": [
            ("tail_sections", "📜 자식 섹션 끝에 (default)"),
            ("interleave", "⏱ 시간순 인터리브"),
            ("headers_only", "📋 헤더만 (간략)"),
            ("split", "↕ 상하 분할 picker"),
        ],
    },
    {
        "key": "archive_search_includes_children",
        "kind": "bool",
        "label": "자식 본문 검색 통합 (deep search 시 archive 자식도)",
    },
    {
        "key": "archive_important_handling",
        "kind": "radio",
        "label": "★ 중요 표시된 세션 archive 시",
        "choices": [
            ("confirm", "한 번 더 confirm (안전)"),
            ("auto_include", "자동 포함 (질문 안 함)"),
            ("reject", "거부 (★ 떼고 다시)"),
        ],
    },
    {
        "key": "archive_restore_enabled",
        "kind": "radio",
        "label": "복원 기능",
        "choices": [
            ("trash_pattern", "🗑 휴지통 패턴 (복원 가능)"),
            ("permanent", "⛔ 영구 (복원 X)"),
        ],
    },
    {
        "key": "archive_trigger_mode",
        "kind": "radio",
        "label": "트리거 진입점",
        "choices": [
            ("both", "🗂 버튼 + Ctrl+Shift+M (둘 다)"),
            ("button", "🗂 버튼만"),
            ("keybinding", "Ctrl+Shift+M 만"),
        ],
    },
    {
        "key": "archive_lazy_load",
        "kind": "bool",
        "label": "Lazy load (자식 본문 처음 5KB만 표시 — 무거운 jsonl 보호)",
    },
    {
        "key": "archive_child_color_distinction",
        "kind": "bool",
        "label": "자식별 색깔 구분 (각 자식의 root 색)",
    },
    {
        "key": "archive_section_header_format",
        "kind": "radio",
        "label": "자식 섹션 헤더 포맷",
        "choices": [
            ("simple", "▶ short_id  name"),
            ("verbose", "▶ short_id  name  ·  N턴  ·  KB  ·  archived_at"),
        ],
    },
    {
        "key": "archive_child_sort_order",
        "kind": "radio",
        "label": "자식 정렬 순서",
        "choices": [
            ("mtime", "최근 수정순"),
            ("branch_order", "분기 시점순 (archived_at)"),
            ("alphabetic", "이름 알파벳순"),
        ],
    },
    {
        "key": "archive_folder_layout",
        "kind": "radio",
        "label": "Archive 폴더 위치",
        "choices": [
            ("per_project", "프로젝트별 — <P>/archive/"),
            ("central", "통합 — ~/.claude/gccfork-archive/<P>/"),
        ],
    },
    # ── True Merge (model B / Phase 6) 옵션 ─────────────────────────────
    {
        "key": "merge_stitching_method",
        "kind": "radio",
        "label": "🗂 병합 stitching 방법 (active jsonl)",
        "hint": "병합으로 만들어진 새 세션에서 어떤 통합 방식을 보여줄지",
        "choices": [
            ("interleave",   "interleave — 공통 + 고유 timestamp 정렬 + 출신 prefix [sid HH:MM] (기본)"),
            ("linear",       "linear — 공통 + 각자 고유 순차 chain"),
            ("parallel",     "parallel — 공통 + 분기 그대로 유지"),
            ("common-only",  "common-only — 공통만 (고유 drop)"),
            ("as-sections",  "as-sections — 공통 + 섹션 구분자 + 각 고유"),
        ],
    },
]


def _build_archive_settings_text() -> str:
    """archive 옵션 10개의 현재 값 표시 + 변경 방법 안내.

    옵션 키에 점 (`archive.preview_mode` 등) 이 있어 textual id 충돌 위험으로
    체크박스 UI 미사용. prefs 파일 직접 편집 또는 추후 별도 모달.
    """
    try:
        from gccfork_archive import ARCHIVE_DEFAULTS, get_archive_pref
    except ImportError:
        return "(archive 모듈 import 실패)"

    LABELS = {
        "archive_preview_mode": ("Preview 통합 방식", "interleave / tail_sections / headers_only / split"),
        "archive_search_includes_children": ("자식 본문 검색 통합", "true / false"),
        "archive_important_handling": ("★ 중요 archive 시", "auto_include / confirm / reject"),
        "archive_restore_enabled": ("복원 기능", "trash_pattern / permanent"),
        "archive_trigger_mode": ("트리거 진입점", "keybinding / button / both"),
        "archive_lazy_load": ("Lazy load (자식 본문 부분만)", "true / false"),
        "archive_child_color_distinction": ("자식별 색깔 구분", "true / false"),
        "archive_section_header_format": ("자식 헤더 포맷", "simple / verbose"),
        "archive_child_sort_order": ("자식 정렬", "mtime / branch_order / alphabetic"),
        "archive_folder_layout": ("Archive 폴더 위치", "per_project / central"),
        # ── True Merge (Phase 6 model B) ─────────────────────────────────
        "merge_stitching_method": ("🗂 병합 stitching 방법", "linear / interleave / parallel / common-only / as-sections"),
    }
    # MERGE_DEFAULTS 도 같이 표시 (lazy import — 실패 시 archive 만)
    DEFAULTS_ALL = dict(ARCHIVE_DEFAULTS)
    try:
        from gccfork_merge import MERGE_DEFAULTS
        DEFAULTS_ALL.update(MERGE_DEFAULTS)
    except Exception:
        pass

    def _get_pref(key):
        # 모든 키는 underscore 그대로 (archive_X, merge_X) — pref_get 직접 조회.
        if key.startswith("archive_"):
            return get_archive_pref(key)
        if key.startswith("merge_"):
            from gccfork_sessions import pref_get as _pref_get
            return _pref_get(key, DEFAULTS_ALL.get(key))
        return None

    lines: list[str] = [
        "현재 적용된 archive + merge 옵션 값입니다 (●=기본값과 같음, ◆=변경됨):",
        "",
    ]
    for key in DEFAULTS_ALL:
        label, choices = LABELS.get(key, (key, ""))
        default = DEFAULTS_ALL[key]
        current = _get_pref(key)
        marker = "●" if current == default else "◆"
        lines.append(f"  {marker} {label}")
        lines.append(f"     키:    {key}")
        lines.append(f"     현재: {current!r}  (기본: {default!r})")
        lines.append(f"     선택: {choices}")
        lines.append("")

    lines += [
        "변경 방법 (현재):",
        "  1. ~/.claude/gccfork-registry.json 의 prefs 항목에 직접 추가/수정",
        "     예: \"prefs\": { \"archive_preview_mode\": \"headers_only\" }",
        "  2. python3 -c \"from gccfork_sessions import pref_set; pref_set('archive.preview_mode', 'headers_only')\"",
        "  3. 추후 RadioSet 기반 UI 모달 추가 예정",
        "",
        "🗂 트리거 (multi-select 후):",
        "  - 멀티 액션 바의 [🗂 병합] 버튼  (trigger_mode = button / both)",
        "  - 단축키 Ctrl+Shift+M  (trigger_mode = keybinding / both)",
    ]
    return "\n".join(lines)


def _load_help_text() -> str:
    """gccfork.HelpScreen.HELP_TEXT 를 가져와 그대로 보여줌 (도움말은 단일 진실 소스 유지)."""
    for module_name in ("gccfork", "__main__"):
        module = sys.modules.get(module_name)
        help_screen = getattr(module, "HelpScreen", None) if module else None
        help_text = getattr(help_screen, "HELP_TEXT", None)
        if isinstance(help_text, str) and help_text.strip():
            return help_text
    try:
        from gccfork import HelpScreen
        return HelpScreen.HELP_TEXT
    except Exception as exc:
        return (
            "[bold red]도움말 로드 실패[/]\n\n"
            f"HelpScreen.HELP_TEXT 를 찾지 못했습니다: {type(exc).__name__}: {exc}"
        )


def _help_text_for_text_area(text: str) -> str:
    """TextArea 는 Rich markup 을 해석하지 않으므로 도움말 표시 태그를 제거."""
    return re.sub(r"\[(?:/?[a-zA-Z][^\]]*|/)\]", "", str(text))


def _settings_items_text(title: str, intro: str | None, items: list[dict]) -> str:
    lines = [_help_text_for_text_area(title)]
    if intro:
        lines += ["", _help_text_for_text_area(intro)]
    for item in items:
        lines.append("")
        lines.append(f"- {_help_text_for_text_area(str(item.get('label', '')))}")
        hint = item.get("hint")
        if hint:
            lines.append(f"  {_help_text_for_text_area(str(hint))}")
    return "\n".join(lines).strip()


def _archive_options_text() -> str:
    lines = [
        "🗂 Archive / 병합 옵션",
        "",
        "변경 즉시 prefs 에 저장되고 다음 archive 작업부터 적용됩니다.",
    ]
    for opt in ARCHIVE_OPTIONS:
        lines.append("")
        lines.append(f"- {_help_text_for_text_area(str(opt.get('label', '')))}")
        hint = opt.get("hint")
        if hint:
            lines.append(f"  {_help_text_for_text_area(str(hint))}")
        choices = opt.get("choices") or []
        for value, label in choices:
            lines.append(f"  - {value}: {_help_text_for_text_area(str(label))}")
    return "\n".join(lines)


def _editor_options_text() -> str:
    return "\n".join(
        [
            "📝 편집기",
            "",
            "Ctrl+E 또는 📝 편집 버튼에서 설정/메모리 파일을 외부 편집기로 엽니다.",
            "auto 는 $EDITOR → code → cursor → nano 우선순위로 자동 선택합니다.",
        ]
    )


def _slim_options_text() -> str:
    lines = [
        "🪴 /slim 기본값",
        "",
        "Claude / Codex 의 /slim 호출과 GccSlim 슬림 버튼 기본값입니다.",
        "",
        "Claude:",
        "- 기본 모드: strong / medium / weak",
        "- 번들(묶음) 처리: context 포함 영역을 큰 묶음으로 정리",
        "- context 비포함 압축본화: cap 밖 영역을 native compact boundary 앞으로 분리",
        "- 슬림 후 자동 열기",
        "- 다른 환경으로 보내기",
        "- 새 탭으로 열기",
        "- 동적 cap: jsonl 크기를 측정해 1M context 안에 들어가도록 자동 조정",
        "- 최근 raw 보존 턴: 모드별 마지막 user 턴 원본 보존",
        "",
        "Codex:",
        "- 기본 모드: safe / strong",
        "- 이전 compact/압축 요약을 새 컨텍스트 앞에 시간순으로 포함",
        "- 결과 구조: 압축요약 #1..N → 현재 slim 본문 → 최근 raw 보호 턴",
        "- 모드 요약:",
    ]
    for mode in ("safe", "strong"):
        title, intro = CODEX_SLIM_MODE_LABELS[mode]
        keep = CODEX_SLIM_MODE_DEFAULT_KEEP[mode]
        lines.append(f"  - {title}: 최근 user 턴 기본 {keep}개 raw 보존. {_help_text_for_text_area(intro)}")
    lines.extend([
        "- keep: 최근 user 턴 raw 보존 수",
        "- 이전 압축요약 포함: compacted.payload.message 를 모두 모아 새 컨텍스트에 삽입",
        "- 원본 보존: slim 복제본 생성",
        "- 슬림 후 자동 열기",
        "- 다른 환경으로 보내기",
        "- 새 탭으로 열기",
        "",
        "일반 슬림 세부 보존 옵션:",
    ])
    for mode in ("strong", "medium", "weak"):
        title, intro = SLIM_MODE_LABELS[mode]
        lines.append("")
        lines.append(f"{_help_text_for_text_area(title)}")
        lines.append(_help_text_for_text_area(intro))
        for item in _slim_items_for_mode(mode):
            lines.append(f"- {_help_text_for_text_area(str(item.get('label', '')))}")
            hint = item.get("hint")
            if hint:
                lines.append(f"  {_help_text_for_text_area(str(hint))}")
    return "\n".join(lines)


# ─── 외부 API — 딥검색 워커가 호출 ─────────────────────────────────────
def get_deep_prefs_snapshot() -> dict:
    """현재 prefs 에서 deep_include_* 5개를 한 번에 읽어 dict 로.

    워커 thread 가 메인 prefs 와 비동기로 작업하므로 시작 시점 스냅샷을 사용.
    """
    from gccfork import pref_get
    return {item["key"]: pref_get(item["key"], item["default"]) for item in DEEP_SEARCH_ITEMS}


def get_scannable_text(obj: dict, prefs: dict) -> str:
    """jsonl 한 라인의 parsed JSON → 매처에 보낼 lowercase 본문 텍스트.

    prefs 의 5개 deep_include_* 플래그를 보고:
      - 그 카테고리가 "포함 OFF" 면 빈 문자열 반환 → 매처가 skip
      - "포함 ON" 면 해당 본문 텍스트를 lowercase 로 반환

    빈 문자열은 곧 "이 라인은 매치 후보 아님" 의 의미.
    """
    if not isinstance(obj, dict):
        return ""

    typ = obj.get("type", "")

    # ── 1. attachment ─────────────────────────────────────────────────
    if typ == "attachment":
        return _full_obj_text(obj) if prefs.get("deep_include_attachment", False) else ""

    # ── 2. file-history-snapshot ──────────────────────────────────────
    if typ == "file-history-snapshot":
        return _full_obj_text(obj) if prefs.get("deep_include_file_history", False) else ""

    # ── 5-a. system / summary / permission-mode 등 메타 타입 ───────────
    if typ in ("system", "summary", "permission-mode", "last-prompt", "custom-title"):
        return _full_obj_text(obj) if prefs.get("deep_include_system_internal", False) else ""

    # ── 5-b. isMeta / isSidechain (user/assistant 라도 메타) ───────────
    if obj.get("isMeta") or obj.get("isSidechain"):
        return _full_obj_text(obj) if prefs.get("deep_include_system_internal", False) else ""

    # ── user/assistant — content 블록 단위로 검사 ────────────────────
    msg = obj.get("message") or {}
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")

    # 5-c. user 메시지 중 <command-name> 같은 internal prefix 시작
    if typ == "user" and isinstance(content, str):
        if not prefs.get("deep_include_system_internal", False):
            stripped = content.lstrip()
            for prefix in _INTERNAL_USER_PREFIXES:
                if stripped.startswith(prefix):
                    return ""

    parts: list[str] = []
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text":
                # 실제 사용자/어시스턴트 본문 — 항상 포함
                t = b.get("text")
                if isinstance(t, str):
                    parts.append(t)
            elif bt == "tool_use":
                # 4. tool_use 인자 (command/path/...)
                if prefs.get("deep_include_tool_use_args", False):
                    name = b.get("name")
                    if isinstance(name, str):
                        parts.append(name)
                    inp = b.get("input")
                    if inp is not None:
                        try:
                            parts.append(json.dumps(inp, ensure_ascii=False))
                        except (TypeError, ValueError):
                            parts.append(str(inp))
            elif bt == "tool_result":
                # 3. tool_result 본문 (ls/find/git/cat 출력)
                if prefs.get("deep_include_tool_result", False):
                    tr = b.get("content")
                    if isinstance(tr, str):
                        parts.append(tr)
                    elif isinstance(tr, list):
                        for x in tr:
                            if isinstance(x, dict):
                                t = x.get("text")
                                if isinstance(t, str):
                                    parts.append(t)
            elif bt == "thinking":
                # 5-d. thinking 블록 — system/internal 토글에 묶음
                if prefs.get("deep_include_system_internal", False):
                    th = b.get("thinking")
                    if isinstance(th, str):
                        parts.append(th)

    return " ".join(p for p in parts if p).lower()


def _full_obj_text(obj: dict) -> str:
    """체크박스가 켜진 카테고리 — 라인 전체 raw json 을 lowercase 로 (옛 워커 동작)."""
    try:
        return json.dumps(obj, ensure_ascii=False).lower()
    except (TypeError, ValueError):
        return str(obj).lower()


# ─── SettingsScreen 모달 ────────────────────────────────────────────────
class SettingsScreen(ModalScreen[None]):
    """⚙ gccfork 설정 — 슬림 모달과 동일한 디자인 톤 + 3-탭 분리.

    레이아웃 (슬림 모달 패턴 그대로):
      ┌─ #settings-box (round $accent 50%) ─────────────┐
      │ ┌─ #settings-header (좌 brand · 중 ⚙ 설정 · 우 v)│
      │ ├─ #settings-tabs ([🔬 검색] [🔻 슬림] [❓ 도움말])│
      │ ├─ #settings-content                             │
      │ │   ─ #pane-search   : 탭 내부 스크롤            │
      │ │   ─ #pane-slim     : 탭 내부 스크롤            │
      │ │   ─ #pane-help     : TextArea 직접 스크롤/선택 │
      │ └─ #settings-btn-row (· N개 변경 · spacer · 닫기)│
      └────────────────────────────────────────────────┘

    탭 전환 — 클릭 시 활성 탭만 display=True, 나머지 False.
    슬림 탭의 모드 전환 — ModeCard 클릭 시 _select_mode() (슬림 모달과 호환).
    """
    BINDINGS = [
        Binding("escape", "close", "닫기", show=False),
        Binding("q", "close", "닫기", show=False),
        Binding("1", "switch_tab('search')", "검색 탭", show=False),
        Binding("2", "switch_tab('slim')", "슬림 탭", show=False),
        Binding("3", "switch_tab('archive')", "병합 탭", show=False),
        Binding("4", "switch_tab('editor')", "편집기 탭", show=False),
        Binding("5", "switch_tab('help')", "도움말 탭", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._active_tab: str = "search"
        self._slim_sub_tab: str = "claude"
        self._slim_mode: str = "strong"
        self._slim_advanced_open: bool = False

    def compose(self) -> ComposeResult:
        from gccfork import (
            pref_get,
            GCCFORK_VERSION,
            CopyMenuOverlay,
            ModeCard,
            SelectableTextArea,
        )

        # 섹션 dict 캐시 — id → section
        sections = {s.get("id", ""): s for s in get_settings_sections()}
        deep_section = sections.get("deep-search", {})
        help_section = sections.get("help", {})
        archive_section = sections.get("archive", {})
        slim_sections = {
            mode: sections.get(f"slim-{mode}", {})
            for mode in ("strong", "medium", "weak")
        }

        with Vertical(id="settings-box"):
            # ── 헤더 ───────────────────────────────────────────────
            with Horizontal(id="settings-header"):
                yield Static("[b]GccForK[/]", id="settings-brand", markup=True)
                yield Static("[b]⚙ 설정[/]", id="settings-title", markup=True)
                yield Static(
                    f"[dim]v{GCCFORK_VERSION}[/]",
                    id="settings-meta", markup=True,
                )

            # ── Scope 토글 제거 (2026-05-08) ────────────────────────
            # gccfork 는 항상 프로젝트 cwd 에서 띄우므로 "전역 vs 프로젝트"
            # 선택은 사용자에게 의미가 없음 — 모든 prefs 는 자동으로
            # <cwd>/.gccfork/ccfork-prefs.json 에 저장됨 (Policy B).
            # backend (set_active_pref_scope) 는 그대로 두되 default="project"
            # 로 강제. 토글 UI 만 제거.

            # ── 탭바 ───────────────────────────────────────────────
            with Horizontal(id="settings-tabs"):
                yield Button("🔬 검색", id="tab-search", classes="settings-tab -active")
                yield Button("🔻 슬림", id="tab-slim", classes="settings-tab")
                yield Button("🗂 병합", id="tab-archive", classes="settings-tab")
                yield Button("📝 편집기", id="tab-editor", classes="settings-tab")
                yield Button("❓ 도움말", id="tab-help", classes="settings-tab")

            # ── 본문 — 탭별 viewport (공통 부모 스크롤 금지) ────────
            with Vertical(id="settings-content"):
                # ─── 검색 pane ────────────────────────────────────
                with VerticalScroll(id="pane-search", classes="settings-pane settings-pane-scroll"):
                    yield Static(
                        deep_section.get("title", "🔬 딥검색"),
                        classes="settings-pane-title", markup=True,
                    )
                    yield SelectableTextArea(
                        _settings_items_text(
                            deep_section.get("title", "🔬 딥검색"),
                            deep_section.get("intro"),
                            deep_section.get("items", []),
                        ),
                        id="settings-search-copy",
                        classes="settings-select-text",
                        read_only=True,
                        soft_wrap=True,
                        compact=True,
                        show_line_numbers=False,
                        highlight_cursor_line=True,
                    )
                    intro = deep_section.get("intro")
                    if intro:
                        yield Static(intro, classes="settings-intro")
                    for item in deep_section.get("items", []):
                        value = pref_get(item["key"], item["default"])
                        with Vertical(classes="settings-checkbox-row"):
                            yield Checkbox(
                                item["label"],
                                value=bool(value),
                                id=f"chk-{item['key']}",
                                classes="settings-checkbox",
                            )
                            hint = item.get("hint")
                            if hint:
                                yield Static(
                                    f"↳ {hint}",
                                    classes="settings-item-hint",
                                )
                    with Horizontal(classes="settings-section-actions"):
                        yield Button(
                            "↻ 디폴트로",
                            id="btn-reset-deep-search",
                            classes="settings-reset-btn",
                        )

                # ─── 슬림 pane ────────────────────────────────────
                with VerticalScroll(id="pane-slim", classes="settings-pane settings-pane-scroll"):
                    # ─── /slim 기본값 — Claude / Codex 별도 기본값 ─────
                    with Horizontal(classes="settings-pane-head"):
                        yield Static(
                            "[b]🪴 /slim 기본값[/]  [dim]· Claude / Codex 호출 시 자동 적용[/]",
                            classes="settings-pane-title", markup=True,
                        )
                        yield Static("", classes="settings-spacer")
                        yield SettingsBrainButton("slim")
                    yield SelectableTextArea(
                        _slim_options_text(),
                        id="settings-slim-copy",
                        classes="settings-select-text",
                        read_only=True,
                        soft_wrap=True,
                        compact=True,
                        show_line_numbers=False,
                        highlight_cursor_line=True,
                    )
                    with Horizontal(id="settings-slim-subtabs"):
                        yield Button("Claude", id="btn-slim-sub-claude", classes="settings-tab -active")
                        yield Button("Codex", id="btn-slim-sub-codex", classes="settings-tab")

                    with Vertical(id="slim-pane-claude", classes="settings-slim-subpane"):
                        yield Static(
                            "claude 의 `/slim` 명령은 옵션 없이 gccfork 에 위임. "
                            "아래 항목은 슬림 버튼 모달의 기본 선택값과 같은 의미입니다.",
                            classes="settings-intro", markup=True,
                        )
                        cur_def_mode = str(pref_get("slim_default_mode", "strong"))
                        yield Static(
                            "[b]기본 모드[/]",
                            classes="settings-radio-label", markup=True,
                        )
                        with RadioSet(id="rs-slim_default_mode", classes="settings-radioset"):
                            for m in ("strong", "medium", "weak"):
                                yield RadioButton(
                                    m,
                                    value=(cur_def_mode == m),
                                    id=f"rb-slim_default_mode-{m}",
                                    classes="settings-radio",
                                )
                        yield Static(
                            "[b]적용 구조[/]\n"
                            "전체 세션\n"
                            "├─ 압축본 / archive        보존\n"
                            "└─ active 영역\n"
                            "   ├─ context 비포함       압축본화\n"
                            "   └─ context 포함         bundle 대상\n"
                            "      └─ 최근 N턴 raw      원본 보존",
                            classes="settings-intro",
                            markup=True,
                        )
                        # [모달과 동일 순서 + 동일 단어]
                        # 1) 번들(묶음) 처리
                        cur_anti_frag = bool(pref_get("slim_default_anti_fragmentation", True))
                        with Vertical(classes="settings-checkbox-row"):
                            yield Checkbox(
                                "번들(묶음) 처리 (context 포함 영역을 큰 묶음으로 정리)",
                                value=cur_anti_frag,
                                id="chk-slim_default_anti_fragmentation",
                                classes="settings-checkbox",
                            )
                            yield Static(
                                "↳ 옛 turn 들을 bundle 구조로 묶어서 in-place 적용. "
                                "context 포함 영역을 정리하고 마지막 N user 턴은 raw 그대로 보존.",
                                classes="settings-item-hint",
                            )

                        # 2) context 비포함 압축본화 — 모달은 cap_overflow 시에만 보이지만,
                        #    설정은 디폴트 정책이므로 항상 노출.
                        cur_cap_compact = bool(pref_get("slim_default_visible_cap_compact", True))
                        with Vertical(classes="settings-checkbox-row"):
                            yield Checkbox(
                                "context 비포함 압축본화 (cap 밖)",
                                value=cur_cap_compact,
                                id="chk-slim_default_visible_cap_compact",
                                classes="settings-checkbox",
                            )
                            yield Static(
                                "↳ Claude Code cap (~230 메시지) 밖의 context 비포함 영역을 "
                                "native compact boundary 앞으로 분리. raw 는 jsonl 에 그대로 보존.",
                                classes="settings-item-hint",
                            )

                        # 3) 슬림 후 자동 열기  (= hot-reload 의 사용자 친화 단어)
                        cur_def_reload = bool(pref_get("slim_default_reload", True))
                        with Vertical(classes="settings-checkbox-row"):
                            yield Checkbox(
                                "슬림 후 자동 열기",
                                value=cur_def_reload,
                                id="chk-slim_default_reload",
                                classes="settings-checkbox",
                            )
                            yield Static(
                                "↳ slim 후 같은 sid 를 새 터미널/탭으로 자동 resume. "
                                "끄면 disk 만 슬림 → 다음 재개 때 적용.",
                                classes="settings-item-hint",
                            )

                        # 3-└) 다른 환경으로 보내기 (디폴트 토글) — 모달의 "└ VSCode 로 보내기" 와 동격
                        cur_other_env = bool(pref_get("slim_default_send_other_env", False))
                        with Vertical(classes="settings-checkbox-row"):
                            yield Checkbox(
                                "    └ 다른 환경으로 보내기 (VSCode / gnome-terminal)",
                                value=cur_other_env,
                                id="chk-slim_default_send_other_env",
                                classes="settings-checkbox",
                            )
                            yield Static(
                                "↳ 자동 열기 ON 일 때 활성. 모달에서는 \"VSCode 로 보내기\" 처럼 "
                                "현재 감지된 다른 환경 이름이 표시됨.",
                                classes="settings-item-hint",
                            )

                        # 3-└) 새 탭으로 열기 (디폴트 토글)
                        cur_newtab = bool(pref_get("slim_default_newtab", False))
                        with Vertical(classes="settings-checkbox-row"):
                            yield Checkbox(
                                "    └ 새 탭으로 열기 (해제 = 새 창)",
                                value=cur_newtab,
                                id="chk-slim_default_newtab",
                                classes="settings-checkbox",
                            )
                            yield Static(
                                "↳ 자동 열기 ON + VSCode 아닐 때 활성.",
                                classes="settings-item-hint",
                            )

                        # 4) 동적 cap — 모달에는 없고 설정 전용 (시스템 자동 정책)
                        cur_dyn_cap = bool(pref_get("slim_default_dynamic_cap", True))
                        with Vertical(classes="settings-checkbox-row"):
                            yield Checkbox(
                                "📏 동적 cap (jsonl 크기 측정 → 1M context 안에 들어가도록 자동 조정)",
                                value=cur_dyn_cap,
                                id="chk-slim_default_dynamic_cap",
                                classes="settings-checkbox",
                            )
                            yield Static(
                                "↳ 큰 세션은 trim cap 자동 축소, 작은 세션은 raw 보존. "
                                "번들(묶음) 처리 ON 일 때만 적용. OFF 면 모드별 고정 cap (200/500/1000). "
                                "모달엔 노출 안 됨 (시스템 자동 정책).",
                                classes="settings-item-hint",
                            )
                        yield Static(
                            "[b]최근 raw 보존 턴[/]",
                            classes="settings-radio-label", markup=True,
                        )
                        for mode in ("strong", "medium", "weak"):
                            turns_key = f"slim_{mode}_keep_recent_turns"
                            cur_turns = int(
                                pref_get(turns_key, SLIM_DEFAULT_PREFS.get(turns_key, 5)) or 5
                            )
                            with Horizontal(classes="settings-turn-row"):
                                yield Static(
                                    f"{mode:<7}",
                                    classes="settings-radio-label",
                                )
                                yield Input(
                                    value=str(cur_turns),
                                    id=f"input-{turns_key}",
                                    type="integer",
                                    classes="settings-input",
                                )
                                yield Static(
                                    "user 턴 raw 보존",
                                    classes="settings-item-hint",
                                )
                        yield Static(
                            "↳ user 메시지 기준. 이 값만큼 마지막 대화 흐름을 raw 로 보존.",
                            classes="settings-item-hint",
                        )
                        with Horizontal(classes="settings-section-actions"):
                            yield Button(
                                "↻ /slim 기본값 디폴트로",
                                id="btn-reset-slim-defaults",
                                classes="settings-reset-btn",
                            )

                        yield Button(
                            "▶ 일반 슬림 세부 보존 옵션",
                            id="btn-toggle-slim-advanced",
                            classes="settings-reset-btn",
                        )
                        with Vertical(id="slim-advanced-pane", classes="settings-slim-sub"):
                            yield Static(
                                "[b][고급][/] 일반 슬림 세부 보존 옵션",
                                classes="settings-pane-title", markup=True,
                            )
                            yield Static(
                                "번들(묶음) 처리를 끄거나 일반 슬림 경로를 쓸 때 의미 있는 옛 세부 옵션입니다.",
                                classes="settings-intro",
                            )
                            with Horizontal(id="settings-slim-mode-row"):
                                for mode in ("strong", "medium", "weak"):
                                    sec = slim_sections.get(mode, {})
                                    mode_title = sec.get("title", mode)
                                    mode_intro = sec.get("intro", "").split("\n")[0]
                                    badge = " [b](기본)[/]" if mode == "strong" else ""
                                    card = ModeCard(
                                        f"[b]{mode_title}[/]{badge}\n[dim]{mode_intro[:48]}[/]",
                                        id=f"setting-mode-{mode}",
                                        classes="settings-mode-card",
                                        markup=True,
                                    )
                                    if mode == self._slim_mode:
                                        card.add_class("-selected")
                                    yield card

                            # 선택 모드의 5체크박스 — 모드별 sub-pane (display 토글)
                            for mode in ("strong", "medium", "weak"):
                                sec = slim_sections.get(mode, {})
                                with Vertical(
                                    id=f"slim-sub-{mode}",
                                    classes="settings-slim-sub",
                                ):
                                    sub_intro = sec.get("intro")
                                    if sub_intro:
                                        yield Static(sub_intro, classes="settings-intro")
                                    for item in sec.get("items", []):
                                        value = pref_get(item["key"], item["default"])
                                        with Vertical(classes="settings-checkbox-row"):
                                            yield Checkbox(
                                                item["label"],
                                                value=bool(value),
                                                id=f"chk-{item['key']}",
                                                classes="settings-checkbox",
                                            )
                                            hint = item.get("hint")
                                            if hint:
                                                yield Static(
                                                    f"↳ {hint}",
                                                    classes="settings-item-hint",
                                                )
                                    with Horizontal(classes="settings-section-actions"):
                                        yield Button(
                                            "↻ 이 모드 디폴트로",
                                            id=f"btn-reset-slim-{mode}",
                                            classes="settings-reset-btn",
                                        )


                    with Vertical(id="slim-pane-codex", classes="settings-slim-subpane"):
                        yield Static(
                            "codex TUI 의 `/slim` 명령과 GccSlim Codex 슬림 버튼 기본값입니다. "
                            "Codex wrapper가 현재 프로젝트의 .gccfork/ccfork-prefs.json 값을 읽어 적용합니다.",
                            classes="settings-intro", markup=True,
                        )
                        cur_codex_mode = str(pref_get("codex_slim_default_mode", "strong"))
                        if cur_codex_mode not in CODEX_SLIM_MODE_LABELS:
                            cur_codex_mode = "strong"
                        yield Static("[b]기본 모드[/]", classes="settings-radio-label", markup=True)
                        with RadioSet(id="rs-codex_slim_default_mode", classes="settings-radioset"):
                            for m in ("safe", "strong"):
                                yield RadioButton(
                                    m,
                                    value=(cur_codex_mode == m),
                                    id=f"rb-codex_slim_default_mode-{m}",
                                    classes="settings-radio",
                                )
                        with Vertical(classes="settings-checkbox-row"):
                            yield Static("[b]모드 요약[/]", classes="settings-radio-label", markup=True)
                            for m in ("safe", "strong"):
                                title, intro = CODEX_SLIM_MODE_LABELS[m]
                                keep = CODEX_SLIM_MODE_DEFAULT_KEEP[m]
                                yield Static(
                                    f"[b]{title}[/]  [dim]최근 user 턴 기본 {keep}개 raw 보존[/]\n"
                                    f"   {intro}",
                                    classes="settings-item-hint",
                                    markup=True,
                                )
                        with Horizontal(classes="settings-turn-row"):
                            yield Static("keep", classes="settings-radio-label")
                            yield Input(
                                value=str(int(pref_get("codex_slim_keep_recent", 3) or 3)),
                                id="input-codex_slim_keep_recent",
                                type="integer",
                                classes="settings-input",
                            )
                            yield Static("최근 user 턴 raw 보존", classes="settings-item-hint")
                        with Vertical(classes="settings-checkbox-row"):
                            yield Checkbox(
                                "이전 compact/압축 요약을 새 컨텍스트에 포함",
                                value=bool(pref_get("codex_slim_include_compact_summaries", True)),
                                id="chk-codex_slim_include_compact_summaries",
                                classes="settings-checkbox",
                            )
                            yield Static(
                                "↳ 3번 압축됐다면 요약 #1, #2, #3을 시간순으로 모아 현재 slim 본문 앞에 넣습니다.",
                                classes="settings-item-hint",
                            )
                        with Vertical(classes="settings-checkbox-row"):
                            yield Checkbox(
                                "원본 보존: slim 복제본 생성",
                                value=bool(pref_get("codex_slim_default_clone", False)),
                                id="chk-codex_slim_default_clone",
                                classes="settings-checkbox",
                            )
                            yield Static("↳ ON이면 원본 JSONL은 그대로 두고 slim된 새 Codex SID를 만듭니다.", classes="settings-item-hint")
                        with Vertical(classes="settings-checkbox-row"):
                            yield Checkbox(
                                "슬림 후 자동 열기",
                                value=bool(pref_get("codex_slim_default_reload", True)),
                                id="chk-codex_slim_default_reload",
                                classes="settings-checkbox",
                            )
                            yield Static("↳ 활성 wrapper 세션이면 같은 터미널 재시작 marker를 사용합니다.", classes="settings-item-hint")
                        with Vertical(classes="settings-checkbox-row"):
                            yield Checkbox(
                                "    └ 다른 환경으로 보내기 (VSCode / gnome-terminal)",
                                value=bool(pref_get("codex_slim_default_send_other_env", False)),
                                id="chk-codex_slim_default_send_other_env",
                                classes="settings-checkbox",
                            )
                        with Vertical(classes="settings-checkbox-row"):
                            yield Checkbox(
                                "    └ 새 탭으로 열기 (해제 = 새 창)",
                                value=bool(pref_get("codex_slim_default_newtab", False)),
                                id="chk-codex_slim_default_newtab",
                                classes="settings-checkbox",
                            )
                        with Horizontal(classes="settings-section-actions"):
                            yield Button(
                                "↻ Codex /slim 기본값 디폴트로",
                                id="btn-reset-codex-slim-defaults",
                                classes="settings-reset-btn",
                            )

                # ─── 🗂 병합 pane — 진짜 RadioSet/Checkbox UI ────────
                with VerticalScroll(id="pane-archive", classes="settings-pane settings-pane-scroll"):
                    yield Static(
                        "[b]🗂 Archive (병합) — 옵션 10개[/]",
                        classes="settings-pane-title", markup=True,
                    )
                    yield SelectableTextArea(
                        _archive_options_text(),
                        id="settings-archive-copy",
                        classes="settings-select-text",
                        read_only=True,
                        soft_wrap=True,
                        compact=True,
                        show_line_numbers=False,
                        highlight_cursor_line=True,
                    )
                    yield Static(
                        "변경 즉시 prefs 에 저장 + 다음 archive 작업부터 적용.",
                        classes="settings-intro",
                    )
                    # 사이드카 import — archive + merge defaults 합치기
                    try:
                        from gccfork_archive import ARCHIVE_DEFAULTS
                    except ImportError:
                        ARCHIVE_DEFAULTS = {}
                    try:
                        from gccfork_merge import MERGE_DEFAULTS
                    except ImportError:
                        MERGE_DEFAULTS = {}
                    DEFAULTS_COMBINED = {**ARCHIVE_DEFAULTS, **MERGE_DEFAULTS}

                    for opt in ARCHIVE_OPTIONS:
                        key = opt["key"]
                        default = DEFAULTS_COMBINED.get(key)
                        current = pref_get(key, default)
                        if opt["kind"] == "bool":
                            with Vertical(classes="settings-checkbox-row"):
                                yield Checkbox(
                                    opt["label"],
                                    value=bool(current),
                                    id=f"chk-{key}",
                                    classes="settings-checkbox",
                                )
                                hint = opt.get("hint")
                                if hint:
                                    yield Static(f"↳ {hint}", classes="settings-item-hint")
                        elif opt["kind"] == "radio":
                            yield Static(
                                f"[b]{opt['label']}[/]",
                                classes="settings-radio-label", markup=True,
                            )
                            hint = opt.get("hint")
                            if hint:
                                yield Static(f"↳ {hint}", classes="settings-item-hint")
                            radio_id = f"rs-{key}"
                            with RadioSet(id=radio_id, classes="settings-radioset"):
                                for value, choice_label in opt["choices"]:
                                    is_current = (str(current) == str(value))
                                    yield RadioButton(
                                        choice_label,
                                        value=is_current,
                                        id=f"rb-{key}-{value}",
                                        classes="settings-radio",
                                    )
                    with Horizontal(classes="settings-section-actions"):
                        yield Button(
                            "↻ 모두 디폴트로",
                            id="btn-reset-archive",
                            classes="settings-reset-btn",
                        )

                # ─── 📝 편집기 pane — config_editor RadioSet ─────
                with VerticalScroll(id="pane-editor", classes="settings-pane settings-pane-scroll"):
                    yield Static(
                        "[b]📝 편집기 — Ctrl+E 또는 📝 편집 버튼[/]",
                        classes="settings-pane-title", markup=True,
                    )
                    yield SelectableTextArea(
                        _editor_options_text(),
                        id="settings-editor-copy",
                        classes="settings-select-text",
                        read_only=True,
                        soft_wrap=True,
                        compact=True,
                        show_line_numbers=False,
                        highlight_cursor_line=True,
                    )
                    yield Static(
                        "설정/메모리 파일 (CLAUDE.md, MEMORY.md 등) 을 클릭으로 외부 편집기 spawn.",
                        classes="settings-intro",
                    )
                    try:
                        from gccfork_config_files import EDITOR_CANDIDATES, EDITOR_DEFAULT, resolve_editor
                    except ImportError:
                        EDITOR_CANDIDATES = ["code", "cursor", "nano"]
                        EDITOR_DEFAULT = "auto"
                        resolve_editor = None
                    cur_editor = pref_get("config_editor", EDITOR_DEFAULT)
                    yield Static(
                        "[b]편집기 선택[/]",
                        classes="settings-radio-label", markup=True,
                    )
                    yield Static(
                        "↳ auto = $EDITOR → code → cursor → nano 우선순위 자동",
                        classes="settings-item-hint",
                    )
                    with RadioSet(id="rs-config_editor", classes="settings-radioset"):
                        for value in [EDITOR_DEFAULT] + EDITOR_CANDIDATES:
                            label = f"{value} (자동 우선순위)" if value == EDITOR_DEFAULT else value
                            yield RadioButton(
                                label,
                                value=(str(cur_editor) == value),
                                id=f"rb-config_editor-{value}",
                                classes="settings-radio",
                            )
                    if resolve_editor is not None:
                        eff, reason = resolve_editor()
                        eff_text = f"현재 적용: [b]{eff or '(없음)'}[/]  [dim]· {reason}[/]"
                        yield Static(eff_text, classes="settings-intro", markup=True)
                    with Horizontal(classes="settings-section-actions"):
                        yield Button(
                            "↻ auto 로 복원",
                            id="btn-reset-editor",
                            classes="settings-reset-btn",
                        )

                # ─── 도움말 pane ──────────────────────────────────
                with Vertical(id="pane-help", classes="settings-pane"):
                    yield Static(
                        help_section.get("title", "❓ 도움말"),
                        classes="settings-pane-title", markup=True,
                    )
                    yield SelectableTextArea(
                        _help_text_for_text_area(help_section.get("content", "")),
                        id="settings-help-content",
                        read_only=True,
                        soft_wrap=True,
                        compact=True,
                        show_line_numbers=False,
                        highlight_cursor_line=True,
                    )

            # ── 푸터 — 양 끝 정렬 ─────────────────────────────────
            with Horizontal(id="settings-btn-row"):
                yield Static("", id="settings-status")
                yield Static("", id="settings-btn-spacer")
                yield Button("Esc 닫기", id="btn-settings-close", variant="primary")
        yield CopyMenuOverlay(id="copy-menu")

    def on_mount(self) -> None:
        # 초기 탭 — 검색만 보이고 나머지 숨김
        self._apply_tab_visibility()
        self._apply_slim_mode_visibility()
        self._apply_slim_subtab_visibility()
        try:
            self.query_one("#tab-search", Button).focus()
        except Exception:
            pass
        self._refresh_status()

    # _compose_scope_toggle removed (2026-05-08): gccfork is always launched
    # in a project's cwd, so a "global vs project" choice is meaningless to
    # the user. All prefs auto-save to <cwd>/.gccfork/ccfork-prefs.json
    # via the active project scope set on TUI mount. Backend infrastructure
    # (set_active_pref_scope) is preserved for power users who edit JSON
    # manually, but no UI exposes the toggle.

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        cid = event.checkbox.id or ""
        if cid.startswith("chk-"):
            from gccfork import pref_set
            pref_set(cid[4:], bool(event.value))
            self._refresh_status()

    def on_input_changed(self, event: Input.Changed) -> None:
        """Input 변경 — id="input-{key}" 패턴에서 key 추출 후 pref_set.
        type=integer 인 input 은 비어있거나 잘못된 값이면 무시 (기본값 보존).
        """
        iid = event.input.id or ""
        if not iid.startswith("input-"):
            return
        key = iid[len("input-"):]
        raw = (event.value or "").strip()
        if not raw:
            return
        try:
            value: object = int(raw)
        except ValueError:
            try:
                value = float(raw)
            except ValueError:
                value = raw
        from gccfork import pref_set
        pref_set(key, value)
        self._refresh_status()

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        """RadioSet 변경 — id="rs-{key}" 패턴에서 key 추출, 선택된 RadioButton 의
        id="rb-{key}-{value}" 에서 value 추출 후 pref_set.

        (Scope toggle removed 2026-05-08 — see _compose_scope_toggle comment.)
        """
        rs_id = event.radio_set.id or ""
        if not rs_id.startswith("rs-"):
            return
        key = rs_id[3:]
        rb_id = event.pressed.id or ""
        prefix = f"rb-{key}-"
        if not rb_id.startswith(prefix):
            return
        value = rb_id[len(prefix):]
        from gccfork import pref_set
        pref_set(key, value)
        self._refresh_status()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid == "btn-settings-close":
            self.dismiss(None)
            return
        if bid.startswith("tab-"):
            self._select_tab(bid[len("tab-"):])
            return
        if bid.startswith("btn-slim-sub-"):
            self._select_slim_sub_tab(bid[len("btn-slim-sub-"):])
            return
        if bid == "btn-toggle-slim-advanced":
            self._slim_advanced_open = not self._slim_advanced_open
            self._apply_slim_mode_visibility()
            return
        if bid.startswith("btn-reset-"):
            self._reset_section(bid[len("btn-reset-"):])
            return

    def action_close(self) -> None:
        self.dismiss(None)

    def action_switch_tab(self, tab: str) -> None:
        self._select_tab(tab)

    # ─── 탭 전환 ────────────────────────────────────────────────────
    def _select_tab(self, tab: str) -> None:
        if tab not in ("search", "slim", "archive", "editor", "help"):
            return
        self._active_tab = tab
        self._apply_tab_visibility()

    def _apply_tab_visibility(self) -> None:
        for tab in ("search", "slim", "archive", "editor", "help"):
            try:
                pane = self.query_one(f"#pane-{tab}")
                pane.display = (tab == self._active_tab)
            except Exception:
                pass
            try:
                btn = self.query_one(f"#tab-{tab}", Button)
                if tab == self._active_tab:
                    btn.add_class("-active")
                else:
                    btn.remove_class("-active")
            except Exception:
                pass

    def _select_slim_sub_tab(self, tab: str) -> None:
        if tab not in ("claude", "codex"):
            return
        self._slim_sub_tab = tab
        self._apply_slim_subtab_visibility()

    def _apply_slim_subtab_visibility(self) -> None:
        for tab in ("claude", "codex"):
            try:
                pane = self.query_one(f"#slim-pane-{tab}", Vertical)
                pane.display = (tab == self._slim_sub_tab)
            except Exception:
                pass
            try:
                btn = self.query_one(f"#btn-slim-sub-{tab}", Button)
                if tab == self._slim_sub_tab:
                    btn.add_class("-active")
                else:
                    btn.remove_class("-active")
            except Exception:
                pass

    # ─── 슬림 모드 전환 (ModeCard 가 호출) ───────────────────────────
    def _select_mode(self, mode: str) -> None:
        if mode not in ("strong", "medium", "weak"):
            return
        self._slim_mode = mode
        # 카드 .-selected 토글
        for m in ("strong", "medium", "weak"):
            try:
                card = self.query_one(f"#setting-mode-{m}")
                if m == mode:
                    card.add_class("-selected")
                else:
                    card.remove_class("-selected")
            except Exception:
                pass
        self._apply_slim_mode_visibility()

    def _apply_slim_mode_visibility(self) -> None:
        try:
            pane = self.query_one("#slim-advanced-pane", Vertical)
            pane.display = self._slim_advanced_open
        except Exception:
            pass
        try:
            btn = self.query_one("#btn-toggle-slim-advanced", Button)
            btn.label = (
                "▼ 일반 슬림 세부 보존 옵션"
                if self._slim_advanced_open
                else "▶ 일반 슬림 세부 보존 옵션"
            )
        except Exception:
            pass
        for m in ("strong", "medium", "weak"):
            try:
                sub = self.query_one(f"#slim-sub-{m}", Vertical)
                sub.display = (self._slim_advanced_open and m == self._slim_mode)
            except Exception:
                pass

    def _slim_settings_system_prompt(self) -> str:
        return (
            "당신은 GccSlim 설정 화면의 슬림 탭 해설 전용 지능 에이전트입니다.\n"
            "목표는 사용자가 슬림 설정과 슬림 버튼 모달의 관계를 바로 이해하도록 돕는 것입니다.\n"
            "GccSlim 슬림이 다루는 Claude Code 세션 구조:\n"
            "- 세션은 ~/.claude/projects/<cwd-slug>/<sid>.jsonl 의 JSONL 파일입니다.\n"
            "- Claude Code 는 native compact marker(isCompactSummary / compact_boundary) 이후의 대화만 주로 컨텍스트로 재구성합니다.\n"
            "- 추가로 user/assistant 메시지 개수 cap 이 있어, 1M 토큰이 비어 있어도 오래된 active 메시지는 컨텍스트에 안 들어갈 수 있습니다.\n"
            "- 기존 압축본/archive 는 raw JSONL 에 남아 있지만 native compact boundary 앞 영역이라 일반 resume 컨텍스트에는 직접 들어가지 않는 영역입니다.\n"
            "- active 영역은 마지막 native compact marker 뒤의 원본 대화입니다.\n"
            "- context 비포함은 active 안에 있지만 Claude 메시지 cap 밖이라 현재 컨텍스트에 들어가지 않는 구간입니다.\n"
            "- context 포함은 현재 cap 안에 들어오는 active 구간이며 bundle 처리 대상입니다.\n"
            "- 최근 raw 보존 턴은 사용자가 방금 보던 마지막 흐름이므로 JSONL 라인을 원본 그대로 보존합니다.\n"
            "슬림 적용 구조:\n"
            "- 압축본/archive: 보존\n"
            "- context 비포함: native compact boundary 앞으로 분리해서 컨텍스트 밖으로 둠\n"
            "- context 포함: bundle 로 묶어 컨텍스트 부담을 줄임\n"
            "- 최근 N턴 raw: 원본 그대로 보존\n"
            "모드 의미:\n"
            "- strong: context 확보 우선. 가장 강하게 줄이고 최근 raw 기본 5턴만 보존합니다.\n"
            "- medium: 균형형. 흐름 가독성과 절감 사이 절충, 최근 raw 기본 10턴 보존입니다.\n"
            "- weak: 보수형. 더 많이 보존하고 절감은 작으며, 최근 raw 기본 30턴 보존입니다.\n"
            "Codex 모드 의미:\n"
            "- safe: 이전 compact 요약 전부, 현재 slim 본문, 최근 raw 30턴을 보존합니다. 복구/검토 우선입니다.\n"
            "- strong: 이전 compact 요약 전부, 현재 slim 본문, 최근 raw 3턴만 보존합니다. 컨텍스트 확보 우선입니다.\n"
            "Codex slim 결과 구조:\n"
            "- compacted.payload.message 요약 #1..N\n"
            "- 현재 세션의 slim 본문\n"
            "- 최근 raw 보호 턴\n"
            "설명 규칙:\n"
            "- 한국어로 답합니다.\n"
            "- 현재 설정값만 근거로 설명합니다.\n"
            "- 기본 설정과 고급 설정을 구분합니다.\n"
            "- 'context 비포함 압축본화'는 raw 삭제가 아니라 native compact boundary 앞으로 분리하는 정책이라고 설명합니다.\n"
            "- '번들(묶음) 처리'와 '최근 raw 보존 턴'이 실제 슬림 실행에 어떤 영향을 주는지 짧게 설명합니다.\n"
            "- 마지막에 사용자가 보통 유지해야 할 추천값을 한 줄로 제시합니다."
        )

    def _slim_settings_user_prompt(self) -> str:
        from gccfork import pref_get
        mode = str(pref_get("slim_default_mode", "strong"))
        vals = {
            "번들(묶음) 처리": bool(pref_get("slim_default_anti_fragmentation", True)),
            "context 비포함 압축본화": bool(pref_get("slim_default_visible_cap_compact", True)),
            "슬림 후 자동 열기": bool(pref_get("slim_default_reload", True)),
            "다른 환경으로 보내기": bool(pref_get("slim_default_send_other_env", False)),
            "새 탭으로 열기": bool(pref_get("slim_default_newtab", False)),
            "동적 cap": bool(pref_get("slim_default_dynamic_cap", True)),
        }
        turns = {
            m: int(pref_get(f"slim_{m}_keep_recent_turns", SLIM_DEFAULT_PREFS[f"slim_{m}_keep_recent_turns"]) or 5)
            for m in ("strong", "medium", "weak")
        }
        advanced = {}
        for m in ("strong", "medium", "weak"):
            advanced[m] = {}
            for item in _slim_items_for_mode(m):
                advanced[m][item["label"]] = bool(pref_get(item["key"], item["default"]))
        option_lines = "\n".join(
            f"- {k}: {'ON' if v else 'OFF'}" for k, v in vals.items()
        )
        turns_lines = "\n".join(f"- {m}: {n} user 턴 raw 보존" for m, n in turns.items())
        return (
            "GccSlim 설정 화면의 슬림 탭 상태를 사용자에게 설명해줘.\n\n"
            f"[기본 모드]\n- {mode}\n\n"
            "[적용 구조]\n"
            "전체 세션\n"
            "├─ 압축본 / archive        보존\n"
            "└─ active 영역\n"
            "   ├─ context 비포함       압축본화\n"
            "   └─ context 포함         bundle 대상\n"
            "      └─ 최근 N턴 raw      원본 보존\n\n"
            f"[기본 옵션]\n{option_lines}\n\n"
            f"[최근 raw 보존 턴]\n{turns_lines}\n\n"
            f"[고급 설정 펼침 상태]\n- {'펼쳐짐' if self._slim_advanced_open else '접힘'}\n\n"
            f"[고급 세부 보존 옵션]\n{json.dumps(advanced, ensure_ascii=False, indent=2)}\n\n"
            "[Codex 기본값]\n"
            f"- mode: {pref_get('codex_slim_default_mode', 'strong')}\n"
            f"- keep_recent: {pref_get('codex_slim_keep_recent', 3)}\n"
            f"- include_compact_summaries: {pref_get('codex_slim_include_compact_summaries', True)}\n"
            f"- clone: {pref_get('codex_slim_default_clone', False)}\n"
            f"- reload: {pref_get('codex_slim_default_reload', True)}\n"
            f"- other_env: {pref_get('codex_slim_default_send_other_env', False)}\n"
            f"- newtab: {pref_get('codex_slim_default_newtab', False)}\n\n"
            "이 설정이 슬림 버튼 모달의 기본 선택값과 어떻게 연결되는지 간결하게 설명해줘."
        )

    def _open_settings_brain_agent(self, target: str) -> None:
        if target != "slim":
            return
        app = self.app
        if not hasattr(app, "_spawn_settings_brain_agent"):
            try:
                app.notify("설정 지능 에이전트 실행 함수를 찾지 못했습니다.", severity="error", timeout=5)
            except Exception:
                pass
            return
        app._spawn_settings_brain_agent(
            source_key="settings-slim",
            system_prompt=self._slim_settings_system_prompt(),
            user_prompt=self._slim_settings_user_prompt(),
        )

    # ─── helpers ────────────────────────────────────────────────────
    def _reset_section(self, section_id: str) -> None:
        """섹션 안의 모든 체크박스/RadioSet 을 default 로 복원.

        Checkbox.value 변경 시 on_checkbox_changed 자동 fire 되어 pref_set 따라옴.
        RadioSet 도 selected 변경 시 on_radio_set_changed fire.
        """
        # 🪴 /slim 기본값 섹션 — SLIM_DEFAULT_PREFS 의 mode + reload 만 복원.
        # 모드별 보호 턴 (slim_{mode}_keep_recent_turns) 은 각 모드의 "이 모드 디폴트로"
        # 버튼 (btn-reset-slim-strong/medium/weak) 이 처리.
        if section_id == "slim-defaults":
            from gccfork import pref_set
            for k in ("slim_default_mode", "slim_default_reload",
                      "slim_default_anti_fragmentation",
                      "slim_default_dynamic_cap",
                      "slim_default_visible_cap_compact",
                      "slim_default_send_other_env",
                      "slim_default_newtab",
                      "slim_strong_keep_recent_turns",
                      "slim_medium_keep_recent_turns",
                      "slim_weak_keep_recent_turns"):
                pref_set(k, SLIM_DEFAULT_PREFS[k])
            try:
                rb = self.query_one(
                    f"#rb-slim_default_mode-{SLIM_DEFAULT_PREFS['slim_default_mode']}",
                    RadioButton,
                )
                if not rb.value:
                    rb.value = True
            except Exception:
                pass
            for chk_id, key in (
                ("#chk-slim_default_reload", "slim_default_reload"),
                ("#chk-slim_default_anti_fragmentation", "slim_default_anti_fragmentation"),
                ("#chk-slim_default_dynamic_cap", "slim_default_dynamic_cap"),
                ("#chk-slim_default_visible_cap_compact", "slim_default_visible_cap_compact"),
                ("#chk-slim_default_send_other_env", "slim_default_send_other_env"),
                ("#chk-slim_default_newtab", "slim_default_newtab"),
            ):
                try:
                    cb = self.query_one(chk_id, Checkbox)
                    cb.value = bool(SLIM_DEFAULT_PREFS[key])
                except Exception:
                    pass
            for mode in ("strong", "medium", "weak"):
                turns_key = f"slim_{mode}_keep_recent_turns"
                try:
                    inp = self.query_one(f"#input-{turns_key}", Input)
                    inp.value = str(SLIM_DEFAULT_PREFS[turns_key])
                except Exception:
                    pass
            self.notify("↻ /slim 기본값 10개 (모드 + 옵션 + 보호 턴) 디폴트로 복원")
            self._refresh_status()
            return

        if section_id == "codex-slim-defaults":
            from gccfork import pref_set
            keys = (
                "codex_slim_default_mode",
                "codex_slim_keep_recent",
                "codex_slim_include_compact_summaries",
                "codex_slim_default_clone",
                "codex_slim_default_reload",
                "codex_slim_default_send_other_env",
                "codex_slim_default_newtab",
            )
            for key in keys:
                pref_set(key, SLIM_DEFAULT_PREFS[key])
            try:
                rb = self.query_one(
                    f"#rb-codex_slim_default_mode-{SLIM_DEFAULT_PREFS['codex_slim_default_mode']}",
                    RadioButton,
                )
                if not rb.value:
                    rb.value = True
            except Exception:
                pass
            try:
                inp = self.query_one("#input-codex_slim_keep_recent", Input)
                inp.value = str(SLIM_DEFAULT_PREFS["codex_slim_keep_recent"])
            except Exception:
                pass
            for chk_id, key in (
                ("#chk-codex_slim_include_compact_summaries", "codex_slim_include_compact_summaries"),
                ("#chk-codex_slim_default_clone", "codex_slim_default_clone"),
                ("#chk-codex_slim_default_reload", "codex_slim_default_reload"),
                ("#chk-codex_slim_default_send_other_env", "codex_slim_default_send_other_env"),
                ("#chk-codex_slim_default_newtab", "codex_slim_default_newtab"),
            ):
                try:
                    cb = self.query_one(chk_id, Checkbox)
                    cb.value = bool(SLIM_DEFAULT_PREFS[key])
                except Exception:
                    pass
            self.notify("↻ Codex /slim 기본값 디폴트로 복원")
            self._refresh_status()
            return

        # 📝 editor 섹션 — config_editor → auto 복원
        if section_id == "editor":
            try:
                from gccfork_config_files import EDITOR_DEFAULT
            except ImportError:
                EDITOR_DEFAULT = "auto"
            from gccfork import pref_set
            try:
                rb_default = self.query_one(f"#rb-config_editor-{EDITOR_DEFAULT}", RadioButton)
                if not rb_default.value:
                    rb_default.value = True
                    self.notify(f"↻ 편집기 → {EDITOR_DEFAULT}")
                else:
                    self.notify(f"이미 {EDITOR_DEFAULT}", severity="information")
            except Exception:
                pass
            pref_set("config_editor", EDITOR_DEFAULT)
            self._refresh_status()
            return

        # 🗂 archive 섹션 — ARCHIVE_OPTIONS 기준 복원 (merge 옵션 포함)
        if section_id == "archive":
            try:
                from gccfork_archive import ARCHIVE_DEFAULTS
            except ImportError:
                ARCHIVE_DEFAULTS = {}
            try:
                from gccfork_merge import MERGE_DEFAULTS
            except ImportError:
                MERGE_DEFAULTS = {}
            DEFAULTS_COMBINED = {**ARCHIVE_DEFAULTS, **MERGE_DEFAULTS}
            from gccfork import pref_set
            reset_count = 0
            for opt in ARCHIVE_OPTIONS:
                key = opt["key"]
                default = DEFAULTS_COMBINED.get(key)
                if opt["kind"] == "bool":
                    try:
                        cb = self.query_one(f"#chk-{key}", Checkbox)
                        if bool(cb.value) != bool(default):
                            cb.value = bool(default)
                            reset_count += 1
                    except Exception:
                        pass
                elif opt["kind"] == "radio":
                    # default 와 다른 RadioButton 선택 → default 의 RadioButton 클릭
                    try:
                        rb_default = self.query_one(f"#rb-{key}-{default}", RadioButton)
                        if not rb_default.value:
                            rb_default.value = True  # 라디오 자체가 다른 것 disable
                            reset_count += 1
                    except Exception:
                        pass
                    # prefs 도 직접 reset (RadioSet.changed 미발화 케이스 대비)
                    pref_set(key, None)
            if reset_count:
                self.notify(f"↻ '🗂 Archive' — {reset_count}개 디폴트 복원")
            else:
                self.notify("이미 '🗂 Archive' 디폴트 상태", severity="information")
            self._refresh_status()
            return

        # 기존 checkbox 섹션
        target = next(
            (s for s in get_settings_sections() if s.get("id") == section_id),
            None,
        )
        if not target or target.get("type") != "checkboxes":
            return
        reset_count = 0
        for item in target.get("items", []):
            try:
                cb = self.query_one(f"#chk-{item['key']}", Checkbox)
                if bool(cb.value) != bool(item["default"]):
                    cb.value = bool(item["default"])
                    reset_count += 1
            except Exception:
                pass
        # slim-{mode} 섹션 → 보호 턴 Input 도 같이 모드별 디폴트로 리셋.
        if section_id.startswith("slim-"):
            mode = section_id[len("slim-"):]
            if mode in ("strong", "medium", "weak"):
                turns_key = f"slim_{mode}_keep_recent_turns"
                turns_default = SLIM_DEFAULT_PREFS.get(turns_key)
                if turns_default is not None:
                    from gccfork import pref_set
                    pref_set(turns_key, turns_default)
                    try:
                        inp = self.query_one(f"#input-{turns_key}", Input)
                        if inp.value != str(turns_default):
                            inp.value = str(turns_default)
                            reset_count += 1
                    except Exception:
                        pass
        title = target.get("title", section_id)
        if reset_count:
            self.notify(f"↻ '{title}' — {reset_count}개 디폴트 복원")
        else:
            self.notify(f"이미 '{title}' 디폴트 상태", severity="information")

    def _refresh_status(self) -> None:
        """푸터 — 디폴트와 다른 prefs 개수 표시 (archive 옵션 포함)."""
        from gccfork import pref_get
        changed = 0
        for section in get_settings_sections():
            if section.get("type") != "checkboxes":
                continue
            for item in section.get("items", []):
                cur = pref_get(item["key"], item["default"])
                if bool(cur) != bool(item["default"]):
                    changed += 1
        # 🗂 archive + merge 옵션 — bool/enum 모두 카운트
        try:
            from gccfork_archive import ARCHIVE_DEFAULTS
        except ImportError:
            ARCHIVE_DEFAULTS = {}
        try:
            from gccfork_merge import MERGE_DEFAULTS
        except ImportError:
            MERGE_DEFAULTS = {}
        DEFAULTS_COMBINED = {**ARCHIVE_DEFAULTS, **MERGE_DEFAULTS}
        for opt in ARCHIVE_OPTIONS:
            key = opt["key"]
            default = DEFAULTS_COMBINED.get(key)
            cur = pref_get(key, default)
            if cur != default:
                changed += 1
        for key in (
            "slim_default_mode",
            "slim_default_reload",
            "slim_default_anti_fragmentation",
            "slim_default_dynamic_cap",
            "slim_default_visible_cap_compact",
            "slim_default_send_other_env",
            "slim_default_newtab",
            "slim_strong_keep_recent_turns",
            "slim_medium_keep_recent_turns",
            "slim_weak_keep_recent_turns",
            "codex_slim_default_mode",
            "codex_slim_keep_recent",
            "codex_slim_include_compact_summaries",
            "codex_slim_default_clone",
            "codex_slim_default_reload",
            "codex_slim_default_send_other_env",
            "codex_slim_default_newtab",
        ):
            if pref_get(key, SLIM_DEFAULT_PREFS.get(key)) != SLIM_DEFAULT_PREFS.get(key):
                changed += 1
        try:
            status = self.query_one("#settings-status", Static)
            if changed == 0:
                status.update("· 모두 디폴트 상태")
            else:
                status.update(f"· {changed}개 변경됨")
        except Exception:
            pass
