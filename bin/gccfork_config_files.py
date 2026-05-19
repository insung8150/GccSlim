"""gccfork_config_files.py — 설정/메모리 파일 편집기 spawn 모듈.

세션 시작 시 자동 주입되는 파일들 (CLAUDE.md / MEMORY.md / GLOBAL_MEMORY.md
등) 을 TUI 안에서 클릭 한 번으로 외부 편집기 (기본 VSCode) 로 열기 위한
사이드카. mono 비대화 정책 — 메인 gccfork 는 import + Mixin 결합 + 버튼/키
바인딩만 추가 (~15 lines).

═══════════════════════════════════════════════════════════════════════════
⚠️ subprocess 사전 주의사항 (반드시 숙지) — 위반 시 사용자 작업 손실
═══════════════════════════════════════════════════════════════════════════

이 파일이 외부 편집기를 spawn 하는 핵심이라 다음 3가지를 반드시 지킨다:

[1] start_new_session=True  — 별개 프로세스 그룹/세션으로 detach
    이유: subprocess.Popen 기본 = TUI 의 자식 프로세스. TUI 가 죽으면
         자식들도 SIGHUP 받아 같이 죽음. 사용자가 "VSCode 켜놓고 TUI 만
         닫기" 했을 때 편집 중이던 파일이 강제 종료됨.
    검증: TUI 죽인 후 VSCode 가 살아있어야 정상.

[2] stdout=DEVNULL, stderr=DEVNULL  — 표준 스트림 모두 버림
    이유: VSCode 시작 시 stderr 로 GTK warning, GPU 메시지, dbus 잡소리
         등 출력. 그게 textual TUI 가 그리고 있는 같은 터미널 화면에
         직접 인쇄 → 박스 보더 깨지고 위젯 망가짐. 한 번 깨지면 화면
         전체 redraw 안 하면 복구 안 됨.
    검증: spawn 후 TUI 화면이 그대로여야 정상.

[3] FileNotFoundError 명시적 catch + notify
    이유: `code` 명령 미설치 시 silently FileNotFoundError 던지고 끝.
         사용자는 "버튼 눌렀는데 왜 안 열려?" 만 봄. notify 로 어떤
         editor 가 없는지 + fallback 결과를 명확히 알린다.
    검증: 일부러 없는 editor 지정해서 notify 메시지 잘 뜨는지.

추가 주의:
  - cwd 인자 안 넘김 → spawn 된 편집기가 사용자 PWD 기준 동작 (덜 혼란)
  - shell=True 절대 사용 X → 경로에 공백/한글 들어갈 때 escape 사고

═══════════════════════════════════════════════════════════════════════════

## 편집기 선택 우선순위

1. prefs `config_editor` (사용자가 ⚙ 설정 → 📝 편집기 탭에서 지정)
2. $EDITOR 환경변수
3. code (VSCode)
4. cursor
5. nano (마지막 fallback)

`auto` 선택 시 위 순서대로 첫 번째 발견되는 명령 사용.

## 발견할 파일 5종 + 동적

| 이모지 | 라벨 | 경로 | 비고 |
|---|---|---|---|
| 🌐 | 글로벌 CLAUDE.md | `~/.claude/CLAUDE.md` | 항상 |
| 📂 | 프로젝트 CLAUDE.md | `<cwd>/CLAUDE.md` | cwd 기준 |
| 🧠 | 프로젝트 MEMORY.md | `~/.claude/projects/<slug>/memory/MEMORY.md` | 자동 발견 |
| 🌍 | GLOBAL_MEMORY.md | `~/.gccslim/memory/GLOBAL_MEMORY.md` | 있을 때만 |
| 🖥 | system-apps.md | `~/.gccslim/memory/system-apps.md` | 있을 때만 |
| 📁 | 메모리 폴더 전체 | `~/.claude/projects/<slug>/memory/` (디렉토리) | VSCode/cursor 만 |
| 📝 | 메모리 *.md 개별 | 같은 폴더 안 모든 .md | 동적 expand |
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# textual — 메인 gccfork 가 PEP 723 venv 안에서만 import 함. 데이터 함수만
# 사용하는 단위 테스트는 이 import 없이 따로 호출 가능 (try/except 가드 안 둠 —
# import 자체가 실패하면 사이드카 import 실패 → 메인이 fallback).
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static, Tree
from textual.widgets.tree import TreeNode

from gccfork_sessions import (
    PROJECTS_DIR,
    cwd_to_slug,
    pref_get,
)


# ─── 편집기 후보 + 우선순위 ─────────────────────────────────────────────
EDITOR_CANDIDATES = ["code", "cursor", "nano"]
EDITOR_DEFAULT = "auto"  # auto = prefs → $EDITOR → 후보 순서대로


def resolve_editor() -> tuple[Optional[str], str]:
    """편집기 명령 + 어떻게 결정했는지 사유 반환.

    Returns:
        (editor_cmd, reason) — editor_cmd 가 None 이면 모두 fallback 실패.
        reason 은 사용자에게 보여줄 한 줄 설명 ("prefs", "$EDITOR", "auto:code" 등).
    """
    # 1. prefs (사용자가 명시 지정)
    pref = str(pref_get("config_editor", EDITOR_DEFAULT))
    if pref != EDITOR_DEFAULT and pref:
        if shutil.which(pref):
            return pref, f"prefs ({pref})"
        # prefs 에 적힌 게 PATH 에 없으면 auto 로 폴백
        return _auto_resolve("prefs 명령 없음 → auto fallback")

    # 2. auto 모드
    return _auto_resolve("auto")


def _auto_resolve(prefix: str) -> tuple[Optional[str], str]:
    """auto 우선순위: $EDITOR → EDITOR_CANDIDATES 순서."""
    env_editor = os.environ.get("EDITOR")
    if env_editor and shutil.which(env_editor.split()[0]):
        return env_editor, f"{prefix}: $EDITOR={env_editor}"
    for cand in EDITOR_CANDIDATES:
        if shutil.which(cand):
            return cand, f"{prefix}: {cand}"
    return None, f"{prefix}: 모든 후보 ({', '.join(EDITOR_CANDIDATES)}) 미설치"


# ─── 파일 메타 ──────────────────────────────────────────────────────────
@dataclass
class ConfigFileEntry:
    """편집 가능한 설정/메모리 파일 한 항목."""
    label: str
    emoji: str
    path: Path
    is_dir: bool = False
    exists: bool = False
    size_bytes: int = 0
    mtime_iso: str = ""

    @property
    def display_path(self) -> str:
        """홈 디렉토리는 ~ 로 줄여서 표시."""
        try:
            rel = self.path.relative_to(Path.home())
            return f"~/{rel}"
        except ValueError:
            return str(self.path)

    @property
    def short_meta(self) -> str:
        """카드 우측 메타 — '3.1KB · 2일 전' 또는 '없음'."""
        if not self.exists:
            return "(없음)"
        if self.is_dir:
            return "폴더"
        kb = max(1, self.size_bytes // 1024)
        return f"{kb}KB · {self.mtime_iso[:10] if self.mtime_iso else '?'}"


def _file_entry(label: str, emoji: str, path: Path, is_dir: bool = False) -> ConfigFileEntry:
    """경로 검사 + 메타 채워서 entry 만들기 (없는 파일도 entry 만들어 회색 표시)."""
    entry = ConfigFileEntry(label=label, emoji=emoji, path=path, is_dir=is_dir)
    try:
        st = path.stat()
        entry.exists = True
        if not is_dir:
            entry.size_bytes = st.st_size
        # iso8601 yyyy-mm-dd
        from datetime import datetime, timezone
        entry.mtime_iso = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
    except (OSError, ValueError):
        pass
    return entry


def discover_config_files(current_cwd: Optional[str]) -> list[ConfigFileEntry]:
    """현재 cwd 기준 편집 가능한 파일들 발견.

    cwd 없으면 (예: TUI 가 임의 경로에서 떴을 때) 프로젝트 CLAUDE.md / MEMORY.md
    는 entry 추가하되 exists=False.
    """
    home = Path.home()
    entries: list[ConfigFileEntry] = []

    # 1. 글로벌 CLAUDE.md
    entries.append(_file_entry("글로벌 CLAUDE.md", "🌐", home / ".claude" / "CLAUDE.md"))

    # 2. 프로젝트 CLAUDE.md (cwd 기준)
    if current_cwd:
        proj_claude = Path(current_cwd) / "CLAUDE.md"
        entries.append(_file_entry("프로젝트 CLAUDE.md", "📂", proj_claude))

    # 3. 프로젝트 MEMORY.md + 메모리 개별 .md 들 (slug 발견)
    if current_cwd:
        try:
            slug = cwd_to_slug(current_cwd)
            mem_dir = PROJECTS_DIR / slug / "memory"
            mem_index = mem_dir / "MEMORY.md"
            entries.append(_file_entry("프로젝트 MEMORY.md", "🧠", mem_index))
            # 메모리 폴더 전체 (편집기로 열면 폴더로 열림 — VSCode/cursor 만)
            entries.append(_file_entry(
                "메모리 폴더 전체", "📁", mem_dir, is_dir=True,
            ))
            # 개별 .md 파일들 (MEMORY.md 제외)
            if mem_dir.exists():
                for md_path in sorted(mem_dir.glob("*.md")):
                    if md_path.name == "MEMORY.md":
                        continue
                    entries.append(_file_entry(
                        md_path.stem, "📝", md_path,
                    ))
        except Exception:
            pass

    # 4. GLOBAL_MEMORY.md
    entries.append(_file_entry(
        "GLOBAL_MEMORY.md", "🌍",
        home / ".gccslim/memory" / "GLOBAL_MEMORY.md",
    ))

    # 5. system-apps.md
    entries.append(_file_entry(
        "system-apps.md", "🖥",
        home / ".gccslim/memory" / "system-apps.md",
    ))

    return entries


# ─── 편집기 spawn 헬퍼 ─────────────────────────────────────────────────
def spawn_editor(path: Path) -> tuple[bool, str]:
    """외부 편집기로 파일/폴더 열기. 반환: (성공여부, 사용자 메시지).

    ⚠️ 위 docstring [1][2][3] 사전 주의사항 모두 적용:
      - start_new_session=True (TUI 죽어도 VSCode 살아있게)
      - stdout/stderr=DEVNULL (TUI 화면 안 깨지게)
      - FileNotFoundError catch (notify 로 명확히)
    """
    editor, reason = resolve_editor()
    if editor is None:
        return False, f"❌ 편집기 못 찾음 — {reason}"

    if not path.exists():
        return False, f"❌ 파일 없음: {path}"

    # editor 가 "code --new-window" 같이 옵션 포함이면 split
    parts = editor.split() if " " in editor else [editor]
    cmd = parts + [str(path)]

    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,   # ⚠️ [2] TUI 화면 보호
            stderr=subprocess.DEVNULL,   # ⚠️ [2] TUI 화면 보호
            stdin=subprocess.DEVNULL,    # 추가 안전장치
            start_new_session=True,      # ⚠️ [1] TUI 죽어도 살아있게
            close_fds=True,
        )
    except FileNotFoundError:
        # ⚠️ [3] resolve 결과로는 PATH 에 있다고 했지만 race condition 으로
        # 사이에 사라졌을 수 있음 — 명확히 안내.
        return False, f"❌ 편집기 실행 실패: '{editor}' 명령 못 찾음 (resolve={reason})"
    except OSError as exc:
        return False, f"❌ 편집기 spawn OSError: {exc} (editor={editor})"

    return True, f"📝 {path.name} 을 {editor} 로 열었습니다 (저장 시 즉시 반영)"


# ─── ConfigFilesScreen — 모달 ──────────────────────────────────────────
CONFIG_FILES_CSS = """
ConfigFilesScreen {
    align: center middle;
}
#cfg-box {
    width: 80%;
    max-width: 100;
    height: 80%;
    background: $surface;
    border: round $accent 35%;
    padding: 0;
}
#cfg-header {
    padding: 1 2;
    height: 4;
    background: $accent 30%;
    border-bottom: hkey $accent 20%;
}
#cfg-header-title {
    text-style: bold;
    color: $accent;
}
#cfg-header-meta {
    color: $foreground 70%;
}
#cfg-tree {
    height: 1fr;
    padding: 1 2;
    background: $surface;
    color: $foreground;
}
#cfg-tree:focus {
    background: $surface;
}
#cfg-tree > .tree--cursor {
    background: $accent 16%;
}
#cfg-tree > .tree--label {
    color: $foreground;
}
#cfg-tree > .tree--guides {
    color: $accent 35%;
}
#cfg-tree > .tree--guides-hover {
    color: $accent 60%;
}
#cfg-tree > .tree--guides-selected {
    color: $accent;
}
#cfg-scroll {
    padding: 1 2;
}
.cfg-card {
    padding: 0 2;
    margin: 0 0 1 0;
    background: $accent 5%;
    border: round $accent 20%;
    height: 4;
    color: $foreground;
}
.cfg-card:hover {
    background: $accent 10%;
    border: round $accent 35%;
}
.cfg-card:focus {
    background: $accent 16%;
    border: round $accent;
}
.cfg-card.-disabled {
    background: transparent;
    color: $foreground 30%;
}
.cfg-card-line1 {
    text-style: bold;
    color: $foreground;
    width: 100%;
    height: 1;
}
.cfg-card-line2 {
    color: $foreground 60%;
    width: 100%;
    height: 1;
}
#cfg-btn-row {
    padding: 0 2;
    height: 5;
    background: $accent 8%;
    border-top: hkey $accent 20%;
    layout: horizontal;
    align: left middle;
}
#cfg-btn-row Button {
    height: 3;
    min-width: 12;
    background: $accent 5%;
    border: round $accent 20%;
    color: $foreground;
    text-style: bold;
    margin: 0 1 0 0;
    padding: 0 2;
}
#cfg-btn-row Button:hover {
    background: $accent 10%;
    border: round $accent 35%;
    color: $accent;
}
#cfg-btn-row Button:focus {
    background: $accent 16%;
    border: round $accent;
    color: $accent;
}
#cfg-btn-row Static {
    color: $foreground 70%;
    height: 3;
    content-align: left middle;
}
#cfg-spacer {
    width: 1fr;
    height: 3;
}
#cfg-btn-cancel {
    min-width: 10;
}
#cfg-btn-advanced {
    min-width: 24;
}
"""


from textual.message import Message as _Message


class ConfigCard(Vertical):
    """파일 카드 — Vertical container + 두 줄 Static + click handler.

    Button 의 single-line label 한계를 우회하기 위해 container 로 wrap.
    클릭/Enter/Space 모두 Activated 메시지 발화.
    """

    can_focus = True
    BINDINGS = [
        Binding("enter", "activate", "열기", show=False),
        Binding("space", "activate", "열기", show=False),
    ]

    class Activated(_Message):
        """카드 활성화 (클릭/Enter/Space) — bubble up 으로 ConfigFilesScreen 이 catch."""
        def __init__(self, entry: "ConfigFileEntry") -> None:
            super().__init__()
            self.entry = entry

    def __init__(self, entry: ConfigFileEntry, **kwargs) -> None:
        classes = "cfg-card" + (" -disabled" if not entry.exists else "")
        super().__init__(classes=classes, **kwargs)
        self.entry = entry
        if not entry.exists:
            self.can_focus = False

    def compose(self) -> ComposeResult:
        line1 = f"{self.entry.emoji}  {self.entry.label}"
        line2 = f"   {self.entry.display_path}  ·  {self.entry.short_meta}"
        yield Static(line1, classes="cfg-card-line1")
        yield Static(line2, classes="cfg-card-line2")

    def on_click(self) -> None:
        if self.entry.exists:
            self.post_message(self.Activated(self.entry))

    def action_activate(self) -> None:
        if self.entry.exists:
            self.post_message(self.Activated(self.entry))


class ConfigFilesScreen(ModalScreen[None]):
    """📝 설정/메모리 파일 편집 모달.

    클릭한 파일을 외부 편집기로 spawn. 편집기는 prefs `config_editor` →
    $EDITOR → code → cursor → nano 순서로 자동 결정.
    """
    DEFAULT_CSS = CONFIG_FILES_CSS

    BINDINGS = [
        Binding("escape", "cancel", "닫기"),
        Binding("q", "cancel", show=False),
    ]

    def __init__(self, current_cwd: Optional[str], **kwargs) -> None:
        super().__init__(**kwargs)
        self.current_cwd = current_cwd
        self.entries: list[ConfigFileEntry] = []

    def on_mount(self) -> None:
        """Tree 노드 빌드 — build_tracked_tree 결과를 textual Tree 에 채움.

        실패 시 풀 traceback 을 /tmp/gccfork-tree-error.log 에 기록 + notify 에 경로.
        """
        import traceback as _tb
        log_path = Path("/tmp/gccfork-tree-error.log")
        try:
            tracked = build_tracked_tree(self.current_cwd)
            tree: Tree = self.query_one("#cfg-tree", Tree)
            self._populate_tree(tree, tracked)
            n_kids = len(tree.root.children)
            self.app.notify(
                f"📥 트리 빌드 완료 · 카테고리 {n_kids}개  (실패 시 {log_path})",
                timeout=4,
            )
        except Exception as exc:
            try:
                log_path.write_text(
                    f"=== {Path('/tmp').exists() and 'cfg tree build error'} ===\n"
                    f"current_cwd: {self.current_cwd}\n\n"
                    f"{_tb.format_exc()}\n",
                    encoding="utf-8",
                )
            except Exception:
                pass
            try:
                self.app.notify(
                    f"❌ 트리 빌드 실패: {exc}\n→ 풀 traceback: {log_path}",
                    severity="error", timeout=15,
                )
            except Exception:
                pass

    def _populate_tree(self, tree: Tree, tracked: dict) -> None:
        """6 카테고리 → Tree 노드. 단순화 원칙:
        - 카테고리 라벨 = `아이콘 NAME · 카운트` (subtitle 제거)
        - hook 명령 → basename 만 (풀 경로는 노드 data 에 저장)
        - 출처 (글로벌/프로젝트) = 색상으로만 구분 ([라벨] prefix 제거)
        - 없는 settings 는 한 줄 요약으로 묶음
        - MEMORY 개별 / reminder 는 default collapsed

        각 카테고리 try/except 로 격리 — 한 카테고리 실패해도 나머지 보임.
        모든 레이블은 markup-safe 하게 escape (`[`, `]` 가 들어간 hook 명령 방어).
        """
        from rich.markup import escape as _esc
        root: TreeNode = tree.root
        root.expand()

        # 출처 → 색상 매핑
        def src_color(src: str) -> str:
            if "프로젝트" in src:
                return "cyan"
            return "white"

        def safe_add(parent: TreeNode, raw: str, *, expand: bool = False, **kwargs):
            """markup-unsafe 문자 escape 후 add. 실패 시 plain text 로 fallback.

            expand 인자는 add() 에 안 넘기고, 노드 생성 후 명시적 .expand() 호출.
            (textual 버전마다 add() 의 expand kwarg 지원이 다름 — 명시적으로 안전.)
            """
            try:
                node = parent.add(raw, **kwargs)
            except Exception:
                node = parent.add(_esc(raw), **kwargs)
            if expand:
                try:
                    node.expand()
                except Exception:
                    pass
            return node

        def safe_leaf(parent: TreeNode, raw: str, **kwargs):
            try:
                return parent.add_leaf(raw, **kwargs)
            except Exception:
                return parent.add_leaf(_esc(raw), **kwargs)

        def fail(name: str, exc: Exception) -> None:
            try:
                self.app.notify(f"[{name}] 빌드 실패: {exc}", severity="error", timeout=8)
            except Exception:
                pass

        # ── 1. SETTINGS — 존재하는 것만 보이고, 없는 건 요약 ──────
        try:
            s = tracked["settings"]
            existing = [f for f in s["files"] if f["exists"]]
            missing = [f for f in s["files"] if not f["exists"]]
            n_settings = safe_add(root, f"[b]{s['icon']} SETTINGS · {len(existing)}[/b]", expand=True)
            for f in existing:
                label_short = _esc(f["label"].split("—")[0].strip())
                color = src_color(f["source_label"])
                hooks_n = _esc(f["label"].split("—")[1].strip() if "—" in f["label"] else "")
                safe_leaf(
                    n_settings,
                    f"[{color}]{label_short}[/{color}]  [dim]· {hooks_n}[/dim]",
                    data={"kind": "file", "path": f["path"], "exists": True},
                )
            if missing:
                safe_leaf(n_settings, f"[dim]({len(missing)}개 없음 — 만들면 자동 인식)[/dim]")
        except Exception as exc:
            fail("SETTINGS", exc)

        # ── 2. HOOK — basename + 색상으로 출처 구분 ──────────────
        try:
            h = tracked["hooks_registered"]
            total_hooks = sum(len(ev["items"]) for ev in h["events"])
            n_hooks = safe_add(root, f"[b]{h['icon']} HOOK · {total_hooks}[/b]", expand=True)
            if not h["events"]:
                safe_leaf(n_hooks, "[dim](없음)[/dim]")
            for ev in h["events"]:
                ev_name = _esc(ev["label"].split(" (")[0])
                n_event = safe_add(n_hooks, f"[yellow]{ev_name}[/yellow] · {len(ev['items'])}", expand=True)
                for it in ev["items"]:
                    color = src_color(it["source"])
                    if it["script_path"]:
                        name = Path(it["script_path"]).name
                    else:
                        cmd = it["label"]
                        import re as _re3
                        m = _re3.search(r"(\S+\.(sh|mjs|js|py|ts))", cmd)
                        name = m.group(1).split("/")[-1] if m else cmd[:40] + ("…" if len(cmd) > 40 else "")
                    name_safe = _esc(name)
                    leaf_label = f"[{color}]{name_safe}[/{color}]"
                    if it["script_path"]:
                        safe_leaf(n_event, leaf_label, data={
                            "kind": "file",
                            "path": it["script_path"],
                            "exists": Path(it["script_path"]).exists(),
                        })
                    else:
                        safe_leaf(n_event, f"{leaf_label}  [dim](인라인 명령)[/dim]")
        except Exception as exc:
            fail("HOOK", exc)

        # ── 3. 실제 주입 증거 — 매칭된 출처별 카운트만 ─────────────
        try:
            e = tracked["evidence"]
            from collections import Counter
            sources_counter = Counter(r["matched_source"] for r in e["reminders"] if r["matched_source"])
            unmatched_count = sum(1 for r in e["reminders"] if not r["matched_source"])
            total_r = len(e["reminders"])
            n_ev = safe_add(root, f"[b]{e['icon']} 실제 증거 · {total_r}개 reminder[/b]", expand=True)
            if e["session_jsonl"]:
                sid_short = Path(e["session_jsonl"]).stem[:8]
                mt = _esc(e.get("mtime_iso", "")[5:16].replace("T", " "))
                safe_leaf(
                    n_ev,
                    f"[dim]세션 {sid_short} · {mt}[/dim]",
                    data={"kind": "file", "path": e["session_jsonl"], "exists": True},
                )
            for src, cnt in sorted(sources_counter.items(), key=lambda x: -x[1]):
                safe_leaf(n_ev, f"[green]✓[/green] [magenta]{_esc(src)}[/magenta] · {cnt}회")
            if unmatched_count:
                safe_leaf(n_ev, f"[dim]? 미매칭 · {unmatched_count}회[/dim]")
            if not e["reminders"]:
                safe_leaf(n_ev, "[dim](아직 reminder 없음)[/dim]")
        except Exception as exc:
            fail("실제 증거", exc)

        # ── 4. CLAUDE.md ─────────────────────────────────────────
        try:
            cm = tracked["claude_md"]
            cm_existing = [f for f in cm["files"] if f["exists"]]
            n_cm = safe_add(root, f"[b]{cm['icon']} CLAUDE.md · {len(cm_existing)}[/b]", expand=True)
            for f in cm_existing:
                label_short = _esc(f["label"].replace(" CLAUDE.md", ""))
                safe_leaf(n_cm, f"[white]{label_short}[/white]", data={
                    "kind": "file", "path": f["path"], "exists": True,
                })
            cm_missing = len(cm["files"]) - len(cm_existing)
            if cm_missing:
                safe_leaf(n_cm, f"[dim]({cm_missing}개 없음)[/dim]")
        except Exception as exc:
            fail("CLAUDE.md", exc)

        # ── 5. MEMORY — collapsed default + 인덱스 + 개별 분리 ───
        try:
            m = tracked["memory"]
            m_existing = [f for f in m["files"] if f["exists"]]
            index_files = [f for f in m_existing if "MEMORY.md" in f["label"] or "폴더" in f["label"]]
            individual = [f for f in m_existing if f not in index_files]
            n_m = safe_add(
                root,
                f"[b]{m['icon']} MEMORY · 인덱스 + .md {len(individual)}개[/b]",
                expand=False,
            )
            for f in index_files:
                safe_leaf(n_m, f"[white]{_esc(f['label'])}[/white]", data={
                    "kind": "file", "path": f["path"], "exists": True,
                })
            if individual:
                n_indv = safe_add(n_m, f"[dim]개별 .md ({len(individual)})[/dim]", expand=False)
                for f in individual:
                    short = _esc(f["label"].replace("memory/", "").replace(".md", ""))
                    safe_leaf(n_indv, f"[white]{short}[/white]", data={
                        "kind": "file", "path": f["path"], "exists": True,
                    })
        except Exception as exc:
            fail("MEMORY", exc)

        # ── 6. 외부 페이로드 ─────────────────────────────────────
        try:
            ex = tracked["external_payload"]
            ex_existing = [f for f in ex["files"] if f["exists"]]
            n_ex = safe_add(root, f"[b]{ex['icon']} 외부 payload · {len(ex_existing)}[/b]", expand=True)
            for f in ex_existing:
                safe_leaf(n_ex, f"[white]{_esc(f['label'])}[/white]", data={
                    "kind": "file", "path": f["path"], "exists": True,
                })
            ex_missing = len(ex["files"]) - len(ex_existing)
            if ex_missing:
                safe_leaf(n_ex, f"[dim]({ex_missing}개 없음)[/dim]")
        except Exception as exc:
            fail("외부 payload", exc)

    def on_tree_node_selected(self, event: "Tree.NodeSelected") -> None:
        """리프 노드 클릭 / Enter → spawn_editor.

        모달은 자동 close 하지 않음 — 사용자가 여러 파일 연속으로 열 수 있게.
        Esc / [취소] 누를 때만 close.
        """
        node = event.node
        data = node.data
        if not data or not isinstance(data, dict):
            return
        if data.get("kind") != "file":
            return
        path_str = data.get("path", "")
        if not path_str:
            return
        path = Path(path_str)
        if not data.get("exists") or not path.exists():
            self.app.notify(f"파일 없음: {path}", severity="warning")
            return
        ok, msg = spawn_editor(path)
        sev = "information" if ok else "error"
        self.app.notify(msg, severity=sev, timeout=4)
        # 의도적으로 dismiss 안 함 — 사용자가 다른 파일도 계속 열 수 있게

    def compose(self) -> ComposeResult:
        # 평면 카드 데이터는 더 안 씀 — Tree 위젯이 모두 보여줌
        editor, reason = resolve_editor()
        editor_label = f"편집기: {editor or '없음'}" if editor else "❌ 편집기 없음"

        with Vertical(id="cfg-box"):
            with Vertical(id="cfg-header"):
                yield Static("📥 자동 주입 — 실제 추적", id="cfg-header-title")
                yield Static(
                    f"리프 노드 Enter/클릭 = VSCode 로 열기 · {reason}",
                    id="cfg-header-meta",
                )
            tree: Tree = Tree("📥 Claude Code 자동 주입 (이 프로젝트)", id="cfg-tree")
            tree.show_root = True
            tree.show_guides = True
            yield tree
            with Horizontal(id="cfg-btn-row"):
                yield Button("[취소]", id="cfg-btn-cancel")
                yield Static("", id="cfg-spacer")
                yield Button("🌐 고급 (브라우저)", id="cfg-btn-advanced")
                yield Static("  " + editor_label)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cfg-btn-cancel":
            self.dismiss()
            return
        if event.button.id == "cfg-btn-advanced":
            self._open_advanced_browser()
            return

    def on_config_card_activated(self, event: "ConfigCard.Activated") -> None:
        """카드 활성화 (클릭/Enter/Space) → 편집기 spawn. 모달은 안 닫힘 (연속 편집)."""
        entry = event.entry
        ok, msg = spawn_editor(entry.path)
        sev = "information" if ok else "error"
        self.app.notify(msg, severity=sev, timeout=4)

    def _open_advanced_browser(self) -> None:
        """🌐 고급 — 5 카테고리 트리 HTML 을 브라우저로 spawn. 모달 유지."""
        try:
            tree = build_injection_tree(self.current_cwd)
            html = render_html_tree(tree)
            import time as _t
            sid = f"{Path(self.current_cwd or 'default').name}-{int(_t.time())}"
            ok, msg, _html_path = open_in_browser(html, sid_hint=sid)
            sev = "information" if ok else "error"
            self.app.notify(msg, severity=sev, timeout=5)
            # 모달 안 닫음 — 사용자가 TUI 트리도 계속 보면서 브라우저 보기
        except Exception as exc:
            self.app.notify(f"❌ 고급 브라우저 spawn 실패: {exc}", severity="error", timeout=8)

    def action_cancel(self) -> None:
        self.dismiss()


# ─── ConfigFilesMixin — App 결합용 ─────────────────────────────────────
class ConfigFilesMixin:
    """메인 GCCForkApp 에 다음 메서드 추가:

      - action_show_config_files() — Ctrl+E 또는 📝 편집 버튼 클릭 시 호출

    self.notify / self.push_screen / self.current_cwd 는 App 본체가 제공.
    """
    def action_show_config_files(self) -> None:
        try:
            current_cwd = getattr(self, "current_cwd", None)
            self.push_screen(ConfigFilesScreen(current_cwd=current_cwd))  # type: ignore[attr-defined]
        except Exception as exc:
            try:
                self.notify(f"📝 편집 모달 띄우기 실패: {exc}", severity="error")  # type: ignore[attr-defined]
            except Exception:
                pass


__all__ = [
    "ConfigFileEntry",
    "ConfigFilesMixin",
    "ConfigFilesScreen",
    "EDITOR_CANDIDATES",
    "EDITOR_DEFAULT",
    "build_injection_tree",
    "build_tracked_tree",
    "discover_config_files",
    "extract_system_reminders",
    "open_in_browser",
    "parse_settings_files",
    "render_html_tree",
    "resolve_editor",
    "spawn_editor",
]


# ═══════════════════════════════════════════════════════════════════════
# 실제 추적 — settings.json 파싱 + jsonl reminder 매칭
# ═══════════════════════════════════════════════════════════════════════

def parse_settings_files(current_cwd: Optional[str]) -> dict:
    """4 settings 파일 (글로벌 + 로컬 + 프로젝트 + 프로젝트 로컬) 파싱.

    각 파일의 hooks 섹션 추출 + 출처 기록. **hook 명령 실행 X — 정적 분석만.**

    Returns:
        {
          "files": [{path, exists, hook_count, raw}, ...],   # 4 entries
          "merged_hooks": {event: [{source, command, type, matcher}]},
        }
    """
    import json as _json
    home = Path.home()

    candidates = [
        (home / ".claude" / "settings.json", "글로벌"),
        (home / ".claude" / "settings.local.json", "글로벌 로컬"),
    ]
    if current_cwd:
        candidates.append((Path(current_cwd) / ".claude" / "settings.json", "프로젝트"))
        candidates.append((Path(current_cwd) / ".claude" / "settings.local.json", "프로젝트 로컬"))

    files: list[dict] = []
    merged: dict[str, list[dict]] = {}

    for path, source_label in candidates:
        entry: dict = {
            "path": str(path),
            "source_label": source_label,
            "exists": path.exists(),
            "hook_count": 0,
            "events": [],
        }
        if path.exists():
            try:
                raw = _json.loads(path.read_text(encoding="utf-8"))
                hooks = raw.get("hooks", {}) or {}
                entry["events"] = list(hooks.keys())
                count = 0
                for event, items in hooks.items():
                    if event not in merged:
                        merged[event] = []
                    if not isinstance(items, list):
                        continue
                    for item in items:
                        # textual settings.json 패턴: [{matcher, hooks: [{type, command}]}]
                        # 또는 단순 패턴: [{type, command}]
                        if isinstance(item, dict):
                            if "hooks" in item and isinstance(item["hooks"], list):
                                matcher = item.get("matcher", "*")
                                for h in item["hooks"]:
                                    if isinstance(h, dict):
                                        merged[event].append({
                                            "source": source_label,
                                            "matcher": matcher,
                                            "type": h.get("type", "?"),
                                            "command": h.get("command", h.get("prompt", "")),
                                        })
                                        count += 1
                            else:
                                merged[event].append({
                                    "source": source_label,
                                    "matcher": item.get("matcher", "*"),
                                    "type": item.get("type", "?"),
                                    "command": item.get("command", item.get("prompt", "")),
                                })
                                count += 1
                entry["hook_count"] = count
            except (OSError, _json.JSONDecodeError) as exc:
                entry["error"] = str(exc)
        files.append(entry)

    return {"files": files, "merged_hooks": merged}


# system-reminder 매칭에 쓸 출처 후보 — (label, source_path, content_signature)
def _build_source_signatures(current_cwd: Optional[str]) -> list[tuple[str, Path, str]]:
    """매칭에 쓸 출처 후보 — 각 파일의 첫 200자 prefix 를 시그니처로."""
    sigs: list[tuple[str, Path, str]] = []
    home = Path.home()

    for label, path in [
        ("글로벌 CLAUDE.md", home / ".claude" / "CLAUDE.md"),
        ("프로젝트 CLAUDE.md", Path(current_cwd) / "CLAUDE.md") if current_cwd else (None, None),
        ("GLOBAL_MEMORY.md", home / ".gccslim/memory" / "GLOBAL_MEMORY.md"),
        ("system-apps.md", home / ".gccslim/memory" / "system-apps.md"),
    ]:
        if label is None or path is None:
            continue
        try:
            if path.exists():
                content = path.read_text(encoding="utf-8", errors="ignore")
                # 의미 있는 첫 줄들 (whitespace 무시)
                sig = "".join(c for c in content[:300] if c.strip())[:80]
                if sig:
                    sigs.append((label, path, sig))
        except OSError:
            pass

    # MEMORY.md 별도
    if current_cwd:
        try:
            slug = cwd_to_slug(current_cwd)
            mem = PROJECTS_DIR / slug / "memory" / "MEMORY.md"
            if mem.exists():
                content = mem.read_text(encoding="utf-8", errors="ignore")
                sig = "".join(c for c in content[:300] if c.strip())[:80]
                if sig:
                    sigs.append(("프로젝트 MEMORY.md", mem, sig))
        except Exception:
            pass

    return sigs


def extract_system_reminders(
    current_cwd: Optional[str],
    max_bytes: int = 30_000_000,   # 30MB — 전체 read (큰 jsonl 도 SessionStart 후 모든 reminder 포함)
    max_reminders: int = 50,
) -> dict:
    """현재 cwd 의 슬러그 폴더에서 mtime 최신 jsonl → system-reminder 추출 + 출처 매칭.

    Returns:
        {
          "session_jsonl": str,   # 분석한 jsonl 절대 경로
          "session_id": str,      # ca099152...
          "mtime_iso": str,
          "reminders": [{
              "snippet": str,         # 처음 80자
              "matched_source": str|None,  # 매칭된 파일 라벨
              "matched_path": str|None,    # 매칭된 파일 경로
          }],
          "total_count": int,     # 전체 reminder 수
        }
    """
    import re as _re
    result = {
        "session_jsonl": "",
        "session_id": "",
        "mtime_iso": "",
        "reminders": [],
        "total_count": 0,
    }

    if not current_cwd:
        return result

    try:
        slug = cwd_to_slug(current_cwd)
    except Exception:
        return result

    proj_dir = PROJECTS_DIR / slug
    if not proj_dir.exists():
        return result

    jsonls = sorted(
        [p for p in proj_dir.glob("*.jsonl") if not p.name.endswith(".bak.jsonl") and ".bak." not in p.name],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not jsonls:
        return result

    latest = jsonls[0]
    result["session_jsonl"] = str(latest)
    result["session_id"] = latest.stem
    try:
        from datetime import datetime as _dt, timezone as _tz
        result["mtime_iso"] = _dt.fromtimestamp(latest.stat().st_mtime, tz=_tz.utc).isoformat()
    except OSError:
        pass

    try:
        with latest.open("rb") as fh:
            data = fh.read(max_bytes)
        text = data.decode("utf-8", errors="ignore")
    except OSError:
        return result

    # <system-reminder> 블록 추출 — attribute 도 허용 + 길이 제한 X
    blocks = _re.findall(r"<system-reminder[^>]*>([\s\S]*?)</system-reminder>", text)
    result["total_count"] = len(blocks)

    # 출처 시그니처 build
    sigs = _build_source_signatures(current_cwd)

    # 추가 휴리스틱 매칭 — 특정 키워드 → hook 출처
    KEYWORD_HINTS = [
        ("openboard", "openboard hook"),
        ("CLAUDE SESSION INITIALIZED", "session-init.sh hook"),
        ("USER CONTEXT", "GLOBAL_MEMORY.md (hook 주입)"),
        ("[SYNC]", "session-init.sh (git sync)"),
    ]

    seen_sigs: set[str] = set()
    for blk in blocks[:max_reminders * 2]:  # 같은 reminder 중복 가능
        sig = blk.strip()[:60]
        if sig in seen_sigs:
            continue
        seen_sigs.add(sig)
        snippet = blk.strip().replace("\n", " ")[:120]

        matched_source = None
        matched_path = None

        # 1. 키워드 휴리스틱 (hook 출처)
        for kw, hint in KEYWORD_HINTS:
            if kw in blk:
                matched_source = hint
                break

        # 2. 파일 시그니처 매칭
        if matched_source is None:
            blk_compact = "".join(c for c in blk[:300] if c.strip())[:80]
            for label, path, file_sig in sigs:
                # 시그니처 prefix 매칭 (앞 30자가 일치하면)
                if len(file_sig) >= 30 and file_sig[:30] in blk_compact:
                    matched_source = label
                    matched_path = str(path)
                    break

        result["reminders"].append({
            "snippet": snippet,
            "matched_source": matched_source,
            "matched_path": matched_path,
        })
        if len(result["reminders"]) >= max_reminders:
            break

    return result


def build_tracked_tree(current_cwd: Optional[str]) -> dict:
    """6 카테고리 통합 — settings + hooks + 실제증거 + claude_md + memory + 외부.

    textual.widgets.Tree 가 그릴 수 있는 dict 트리 반환.
    """
    settings_data = parse_settings_files(current_cwd)
    reminders_data = extract_system_reminders(current_cwd)
    base_tree = build_injection_tree(current_cwd)

    # 카테고리 1 — SETTINGS 파일들 (정적 분석)
    settings_node = {
        "key": "settings",
        "icon": "⚙",
        "label": f"SETTINGS 파일 ({len(settings_data['files'])}개) [정적 분석]",
        "subtitle": "hook 등록 위치. 정적 파싱 only — 실행 X.",
        "files": [],
    }
    for f in settings_data["files"]:
        suffix = f"활성 hook {f['hook_count']}개" if f["exists"] else "(없음)"
        settings_node["files"].append({
            "label": f"{f['source_label']} — {suffix}",
            "source_label": f["source_label"],   # _populate_tree 가 색상 매핑에 사용
            "path": f["path"],
            "exists": f["exists"],
            "is_dir": False,
            "size_bytes": Path(f["path"]).stat().st_size if f["exists"] else 0,
            "mtime_iso": "",
            "when": "마스터",
            "how": f["source_label"] + " settings",
        })

    # 카테고리 2 — 등록된 HOOK (event 별)
    hooks_node = {
        "key": "hooks_registered",
        "icon": "🪝",
        "label": "등록된 HOOK (event 별)",
        "subtitle": "settings 파일 4개 머지 결과 — 어떤 hook 이 어느 event 에 등록됐나",
        "events": [],
    }
    for event, items in settings_data["merged_hooks"].items():
        event_node = {
            "label": f"{event} ({len(items)}개)",
            "items": [],
        }
        for h in items:
            cmd = h["command"][:80] + ("..." if len(h["command"]) > 80 else "")
            # 명령 안에 .sh 경로 추출 시도 (편집 가능하게)
            script_path = ""
            import re as _re2
            m = _re2.search(r"(\S+\.sh)\b", h["command"])
            if m:
                script_path = m.group(1).replace("~", str(Path.home()))
            event_node["items"].append({
                "label": cmd,
                "source": h["source"],
                "type": h["type"],
                "matcher": h.get("matcher", "*"),
                "script_path": script_path,
            })
        hooks_node["events"].append(event_node)

    # 카테고리 3 — 실제 주입 증거
    evidence_node = {
        "key": "evidence",
        "icon": "✅",
        "label": "실제 주입 증거 — 최근 세션 jsonl",
        "subtitle": (
            f"세션: {reminders_data['session_id'][:8] if reminders_data['session_id'] else '(없음)'}  ·  "
            f"mtime: {reminders_data['mtime_iso'][:19] if reminders_data['mtime_iso'] else '?'}  ·  "
            f"system-reminder: {reminders_data['total_count']}개"
        ),
        "session_jsonl": reminders_data["session_jsonl"],
        "reminders": reminders_data["reminders"],
    }

    return {
        "settings": settings_node,
        "hooks_registered": hooks_node,
        "evidence": evidence_node,
        "claude_md": next(c for c in base_tree["categories"] if c["key"] == "claude_md"),
        "memory": next(c for c in base_tree["categories"] if c["key"] == "memory"),
        "external_payload": next(c for c in base_tree["categories"] if c["key"] == "external_payload"),
        "meta": base_tree["meta"],
    }


# ═══════════════════════════════════════════════════════════════════════
# 고급 — 자동 주입 트리 + 개발자 친화 HTML
# ═══════════════════════════════════════════════════════════════════════

def build_injection_tree(current_cwd: Optional[str]) -> dict:
    """5 카테고리로 자동 주입 항목들을 dict 트리로 반환.

    각 leaf 노드는: {label, path, exists, size_bytes, mtime_iso, when, how}
    카테고리: hooks / claude_md / memory / external_payload / skills
    """
    home = Path.home()
    claude_dir = home / ".claude"

    def file_node(label: str, path: Path, when: str, how: str) -> dict:
        node = {
            "label": label,
            "path": str(path),
            "exists": path.exists(),
            "is_dir": path.is_dir() if path.exists() else False,
            "size_bytes": 0,
            "mtime_iso": "",
            "when": when,
            "how": how,
        }
        try:
            st = path.stat()
            if not node["is_dir"]:
                node["size_bytes"] = st.st_size
            from datetime import datetime, timezone
            node["mtime_iso"] = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
        except OSError:
            pass
        return node

    # 1. HOOK 인프라 ⭐
    hook_files = []
    hook_files.append(file_node(
        "settings.json (마스터 hook 정의)",
        claude_dir / "settings.json",
        "매 세션 + 매 prompt",
        "hook 등록 → stdout 을 컨텍스트에 주입",
    ))
    hook_files.append(file_node(
        "settings.local.json (로컬 override)",
        claude_dir / "settings.local.json",
        "매 세션 + 매 prompt",
        "settings.json 위에 머지 (있으면)",
    ))
    hooks_dir = claude_dir / "hooks"
    if hooks_dir.exists():
        for hook in sorted(hooks_dir.glob("*.sh")):
            hook_files.append(file_node(
                f"hooks/{hook.name}",
                hook,
                "settings.json 의 trigger 따라",
                "shell script — stdout 으로 주입",
            ))

    # 2. CLAUDE.md (자동 발견)
    claude_md_files = []
    claude_md_files.append(file_node(
        "글로벌 CLAUDE.md",
        claude_dir / "CLAUDE.md",
        "매 turn",
        "Claude Code 가 자동 발견 + 컨텍스트 주입",
    ))
    if current_cwd:
        # cwd 부터 위로 traverse
        seen = set()
        cur = Path(current_cwd).resolve()
        while True:
            for cand in [cur / "CLAUDE.md", cur / ".claude" / "CLAUDE.md"]:
                if cand in seen:
                    continue
                seen.add(cand)
                if cand.exists():
                    rel = "프로젝트" if cur == Path(current_cwd).resolve() else f"상위 {cur.name}"
                    sub = " (.claude/)" if ".claude" in cand.parts else ""
                    claude_md_files.append(file_node(
                        f"{rel}{sub} CLAUDE.md",
                        cand,
                        "매 turn",
                        "Claude Code 가 cwd → root traverse 중 발견",
                    ))
            if cur.parent == cur:
                break
            cur = cur.parent

    # 3. MEMORY
    memory_files = []
    if current_cwd:
        try:
            slug = cwd_to_slug(current_cwd)
            mem_dir = PROJECTS_DIR / slug / "memory"
            memory_files.append(file_node(
                "MEMORY.md (인덱스)",
                mem_dir / "MEMORY.md",
                "매 세션 (200줄까지)",
                "Claude Code 가 자동 로드 + 컨텍스트 inject",
            ))
            memory_files.append(file_node(
                "memory/ 폴더 전체",
                mem_dir,
                "lazy",
                "모델이 인덱스 보고 필요 시 직접 read",
            ))
            if mem_dir.exists():
                for md in sorted(mem_dir.glob("*.md")):
                    if md.name == "MEMORY.md":
                        continue
                    memory_files.append(file_node(
                        f"memory/{md.name}",
                        md,
                        "lazy (인덱스 참조 시)",
                        "모델이 인덱스 보고 read",
                    ))
        except Exception:
            pass

    # 4. 외부 hook 페이로드
    payload_files = []
    payload_files.append(file_node(
        "GLOBAL_MEMORY.md",
        home / ".gccslim/memory" / "GLOBAL_MEMORY.md",
        "매 세션 시작",
        "SessionStart hook 이 cat → 컨텍스트 주입",
    ))
    payload_files.append(file_node(
        "system-apps.md",
        home / ".gccslim/memory" / "system-apps.md",
        "참조용",
        "CLAUDE.md 가 link, 모델이 필요 시 read",
    ))

    # 5. SKILLS
    skill_files = []
    skills_dir = claude_dir / "skills"
    if skills_dir.exists():
        for skill_dir in sorted(skills_dir.iterdir()):
            skill_md = skill_dir / "SKILL.md"
            if skill_md.exists():
                skill_files.append(file_node(
                    f"skills/{skill_dir.name}/SKILL.md",
                    skill_md,
                    "/skill 호출 시",
                    "사용자가 /skill 호출 → 그때 로드",
                ))

    return {
        "categories": [
            {
                "key": "hooks",
                "icon": "🪝",
                "label": "HOOK 인프라",
                "subtitle": "자동 주입의 마스터 — 무엇이 언제 주입될지 결정",
                "when": "매 세션 + 매 prompt",
                "starred": True,
                "files": hook_files,
            },
            {
                "key": "claude_md",
                "icon": "📄",
                "label": "CLAUDE.md",
                "subtitle": "Claude Code 가 cwd → root traverse 중 자동 발견",
                "when": "매 turn 컨텍스트",
                "files": claude_md_files,
            },
            {
                "key": "memory",
                "icon": "🧠",
                "label": "MEMORY",
                "subtitle": "인덱스 자동 로드 + 본문은 모델이 lazy read",
                "when": "매 세션 (인덱스) / lazy (본문)",
                "files": memory_files,
            },
            {
                "key": "external_payload",
                "icon": "🌍",
                "label": "외부 hook 페이로드",
                "subtitle": "SessionStart hook 이 cat → 매 세션 주입",
                "when": "매 세션 시작",
                "files": payload_files,
            },
            {
                "key": "skills",
                "icon": "🛠",
                "label": "SKILLS",
                "subtitle": "/skill 호출 시 로드 (자동 주입 X)",
                "when": "on-demand",
                "files": skill_files,
            },
        ],
        "meta": {
            "current_cwd": current_cwd or "",
            "home": str(home),
        },
    }


# ── 개발자 친화 HTML 템플릿 (single file, 의존성 0) ─────────────────────
# 다크 모노스페이스. 키보드: / = 검색, j/k = 다음/이전, Enter = vscode 열기,
# Esc = 검색 비우기. 세부 트리는 <details>/<summary> 로 native expand/collapse.
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>📥 Claude Code 자동 주입 트리</title>
<style>
  :root {
    --bg: #0d1117;
    --bg-card: #161b22;
    --bg-hover: #1f2937;
    --fg: #c9d1d9;
    --fg-dim: #8b949e;
    --border: #30363d;
    --accent: #58a6ff;
    --accent-soft: #1f6feb33;
    --green: #56d364;
    --yellow: #e3b341;
    --red: #f85149;
    --purple: #bc8cff;
    --mono: 'JetBrains Mono', 'SF Mono', 'Menlo', 'Consolas', monospace;
  }
  * { box-sizing: border-box; }
  body {
    background: var(--bg);
    color: var(--fg);
    font-family: var(--mono);
    font-size: 13px;
    line-height: 1.5;
    margin: 0;
    padding: 24px 32px;
    max-width: 1100px;
    margin: 0 auto;
  }
  h1 {
    font-size: 18px;
    font-weight: 600;
    margin: 0 0 4px 0;
    color: var(--fg);
  }
  .subtitle { color: var(--fg-dim); margin: 0 0 24px 0; font-size: 12px; }
  .meta-bar {
    display: flex; gap: 16px; padding: 8px 12px;
    background: var(--bg-card); border: 1px solid var(--border);
    border-radius: 6px; margin-bottom: 16px; font-size: 12px;
    color: var(--fg-dim);
  }
  .meta-bar span strong { color: var(--fg); }

  #search {
    width: 100%; padding: 10px 14px;
    background: var(--bg-card); color: var(--fg);
    border: 1px solid var(--border); border-radius: 6px;
    font-family: var(--mono); font-size: 13px;
    margin-bottom: 16px;
  }
  #search:focus { outline: none; border-color: var(--accent); }

  details {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 6px;
    margin-bottom: 12px;
    overflow: hidden;
  }
  details[open] { border-color: var(--accent-soft); }
  summary {
    padding: 12px 16px;
    cursor: pointer;
    list-style: none;
    user-select: none;
    display: flex; align-items: baseline; gap: 12px;
    font-weight: 500;
  }
  summary::before {
    content: "▶"; color: var(--fg-dim); font-size: 10px; width: 12px;
    transition: transform 0.1s;
  }
  details[open] summary::before { transform: rotate(90deg); }
  summary:hover { background: var(--bg-hover); }
  .cat-icon { font-size: 16px; }
  .cat-label { color: var(--fg); }
  .cat-when {
    margin-left: auto; padding: 2px 8px;
    background: var(--accent-soft); color: var(--accent);
    border-radius: 10px; font-size: 11px;
  }
  .cat-star { color: var(--yellow); }
  .cat-sub {
    padding: 0 16px 8px 40px;
    color: var(--fg-dim); font-size: 12px;
  }
  .files { padding: 0 0 8px 0; }
  .file {
    display: grid;
    grid-template-columns: 1fr auto auto;
    gap: 12px;
    padding: 8px 16px 8px 40px;
    border-top: 1px solid var(--border);
    align-items: center;
  }
  .file:hover { background: var(--bg-hover); }
  .file.missing { opacity: 0.4; }
  .file-label { color: var(--fg); }
  .file-path {
    color: var(--fg-dim); font-size: 11px;
    margin-top: 2px; word-break: break-all;
  }
  .file-meta {
    color: var(--fg-dim); font-size: 11px; white-space: nowrap;
  }
  .file-actions { display: flex; gap: 6px; }
  .btn {
    padding: 4px 10px; border-radius: 4px;
    background: transparent; border: 1px solid var(--border);
    color: var(--fg-dim); cursor: pointer;
    font-family: var(--mono); font-size: 11px;
    text-decoration: none;
  }
  .btn:hover { background: var(--accent-soft); border-color: var(--accent); color: var(--accent); }
  .btn-primary { color: var(--accent); border-color: var(--accent-soft); }
  .file-when {
    color: var(--purple); font-size: 11px;
    padding: 2px 6px; background: rgba(188, 140, 255, 0.1);
    border-radius: 3px;
  }
  .file-how {
    color: var(--fg-dim); font-size: 11px;
    margin-top: 2px;
  }
  .empty {
    padding: 16px 40px; color: var(--fg-dim);
    font-style: italic; font-size: 12px;
  }
  .help {
    margin-top: 24px; padding: 12px 16px;
    background: var(--bg-card); border: 1px solid var(--border);
    border-radius: 6px; color: var(--fg-dim); font-size: 11px;
  }
  .help kbd {
    background: var(--bg); border: 1px solid var(--border);
    border-radius: 3px; padding: 1px 6px; font-family: var(--mono);
    color: var(--fg); font-size: 10px;
  }
  .total-bar {
    display: flex; gap: 12px; margin-bottom: 16px;
    padding: 8px 12px; background: var(--bg-card);
    border: 1px solid var(--border); border-radius: 6px;
    font-size: 11px; color: var(--fg-dim);
  }
  .total-bar .stat strong { color: var(--green); }
</style>
</head>
<body>

<h1>📥 Claude Code 자동 주입 트리</h1>
<p class="subtitle">언제 / 어떻게 / 어디서 → 클릭으로 편집</p>

<div class="meta-bar">
  <span><strong>cwd:</strong> __CURRENT_CWD__</span>
  <span><strong>home:</strong> __HOME__</span>
  <span><strong>generated:</strong> __GENERATED__</span>
</div>

<div class="total-bar" id="totals"></div>

<input id="search" type="text" placeholder="🔍  파일명/경로 검색  (/ 단축키 · Esc 비우기)" autofocus>

<div id="categories"></div>

<div class="help">
  <strong>키보드</strong>:
  <kbd>/</kbd> 검색 ·
  <kbd>Esc</kbd> 검색 비우기 ·
  <kbd>j</kbd>/<kbd>k</kbd> 다음/이전 ·
  <kbd>Enter</kbd> 편집기 열기 ·
  <kbd>g</kbd>/<kbd>G</kbd> 처음/끝
  <br><br>
  <strong>편집기</strong>: vscode:// URL 스킴 — 클릭 시 OS 가 VSCode 자동 spawn (이미 떠있으면 같은 창 새 탭).
  <br>
  <strong>📁 파일 매니저</strong>: 폴더/파일 옆 📁 버튼 = OS 기본 파일 매니저로 위치 열기 (file:// URL).
</div>

<script>
const TREE = __TREE_JSON__;

function fmtSize(b) {
  if (!b) return "-";
  if (b < 1024) return b + "B";
  if (b < 1024 * 1024) return (b / 1024).toFixed(1) + "KB";
  return (b / 1024 / 1024).toFixed(1) + "MB";
}
function fmtDate(iso) {
  if (!iso) return "-";
  return iso.slice(0, 10);
}
function fmtFromHome(path) {
  const home = TREE.meta.home;
  if (path.startsWith(home + "/")) return "~/" + path.slice(home.length + 1);
  return path;
}

function render() {
  const root = document.getElementById("categories");
  root.innerHTML = "";

  let totalFiles = 0, totalExists = 0, totalSize = 0;

  for (const cat of TREE.categories) {
    const det = document.createElement("details");
    det.dataset.catKey = cat.key;
    det.open = (cat.key === "hooks" || cat.key === "claude_md" || cat.key === "memory");

    const summary = document.createElement("summary");
    summary.innerHTML = `
      <span class="cat-icon">${cat.icon}</span>
      <span class="cat-label">${cat.label}${cat.starred ? ' <span class="cat-star">★</span>' : ''}</span>
      <span class="cat-when">${cat.when}</span>
    `;
    det.appendChild(summary);

    const sub = document.createElement("div");
    sub.className = "cat-sub";
    sub.textContent = cat.subtitle;
    det.appendChild(sub);

    const filesDiv = document.createElement("div");
    filesDiv.className = "files";

    if (!cat.files.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "(없음)";
      filesDiv.appendChild(empty);
    }

    for (const f of cat.files) {
      totalFiles++;
      if (f.exists) { totalExists++; totalSize += f.size_bytes || 0; }

      const row = document.createElement("div");
      row.className = "file" + (f.exists ? "" : " missing");
      row.dataset.search = (f.label + " " + f.path).toLowerCase();

      const main = document.createElement("div");
      main.innerHTML = `
        <div class="file-label">${f.label}</div>
        <div class="file-path">${fmtFromHome(f.path)}</div>
        <div class="file-how">↳ ${f.how}</div>
      `;
      row.appendChild(main);

      const meta = document.createElement("div");
      meta.className = "file-meta";
      meta.innerHTML = `
        <div><span class="file-when">${f.when}</span></div>
        <div style="margin-top:4px;">${fmtSize(f.size_bytes)} · ${fmtDate(f.mtime_iso)}</div>
      `;
      row.appendChild(meta);

      const acts = document.createElement("div");
      acts.className = "file-actions";
      if (f.exists) {
        const editUrl = "vscode://file" + encodeURI(f.path);
        const fileUrl = "file://" + encodeURI(f.is_dir ? f.path : f.path.substring(0, f.path.lastIndexOf("/")));
        acts.innerHTML = `
          <a class="btn btn-primary" href="${editUrl}" data-edit>📝 편집</a>
          <a class="btn" href="${fileUrl}" target="_blank">📁</a>
        `;
      } else {
        acts.innerHTML = `<span class="btn" style="cursor:default;opacity:0.5;">없음</span>`;
      }
      row.appendChild(acts);

      filesDiv.appendChild(row);
    }

    det.appendChild(filesDiv);
    root.appendChild(det);
  }

  // 토탈 바
  const t = document.getElementById("totals");
  t.innerHTML = `
    <span class="stat"><strong>${totalExists}</strong> / ${totalFiles} 파일 존재</span>
    <span class="stat">총 <strong>${fmtSize(totalSize)}</strong></span>
    <span class="stat">${TREE.categories.length} 카테고리</span>
  `;
}

// 검색 — 즉시 필터
function applySearch(q) {
  q = (q || "").toLowerCase().trim();
  const rows = document.querySelectorAll(".file");
  let visible = 0;
  rows.forEach(r => {
    const match = !q || r.dataset.search.includes(q);
    r.style.display = match ? "" : "none";
    if (match) visible++;
  });
  document.querySelectorAll("details").forEach(d => {
    if (q) d.open = true;
    const cat = d.querySelectorAll(".file");
    const anyVisible = Array.from(cat).some(r => r.style.display !== "none");
    d.style.display = (q && !anyVisible) ? "none" : "";
  });
}

const search = document.getElementById("search");
search.addEventListener("input", e => applySearch(e.target.value));

// 키보드 — vim-like
let cursor = -1;
function visibleRows() {
  return Array.from(document.querySelectorAll(".file")).filter(r => r.style.display !== "none");
}
function moveCursor(delta) {
  const rows = visibleRows();
  if (!rows.length) return;
  cursor = Math.max(0, Math.min(rows.length - 1, cursor + delta));
  rows.forEach((r, i) => {
    r.style.outline = (i === cursor) ? "2px solid var(--accent)" : "";
    r.style.outlineOffset = (i === cursor) ? "-2px" : "";
  });
  rows[cursor].scrollIntoView({ block: "nearest" });
}
document.addEventListener("keydown", e => {
  if (e.target.tagName === "INPUT") {
    if (e.key === "Escape") { search.value = ""; applySearch(""); search.blur(); }
    return;
  }
  if (e.key === "/") { e.preventDefault(); search.focus(); }
  else if (e.key === "j") { e.preventDefault(); moveCursor(1); }
  else if (e.key === "k") { e.preventDefault(); moveCursor(-1); }
  else if (e.key === "g") { cursor = -1; moveCursor(1); }
  else if (e.key === "G") { cursor = visibleRows().length; moveCursor(-1); }
  else if (e.key === "Enter") {
    const rows = visibleRows();
    if (cursor >= 0 && rows[cursor]) {
      const link = rows[cursor].querySelector("a[data-edit]");
      if (link) link.click();
    }
  }
});

render();
</script>
</body>
</html>
"""


def render_html_tree(tree: dict) -> str:
    """build_injection_tree 결과를 단일 HTML 문자열로 렌더링.

    의존성 0 — 모든 CSS/JS 가 inline. JSON 은 안전하게 escape (</script> 방어).
    """
    import json as _json
    from datetime import datetime as _dt
    json_str = _json.dumps(tree, ensure_ascii=False).replace("</", "<\\/")
    html = HTML_TEMPLATE
    html = html.replace("__TREE_JSON__", json_str)
    html = html.replace("__CURRENT_CWD__", _html_escape(tree["meta"].get("current_cwd", "(없음)")))
    html = html.replace("__HOME__", _html_escape(tree["meta"].get("home", "")))
    html = html.replace("__GENERATED__", _dt.now().strftime("%Y-%m-%d %H:%M:%S"))
    return html


def _html_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def open_in_browser(html_content: str, sid_hint: str = "current") -> tuple[bool, str, Optional[Path]]:
    """HTML 을 /tmp 에 쓰고 OS 기본 브라우저로 spawn.

    같은 sid 면 덮어쓰기 → /tmp 누적 방지. /tmp 라 OS 가 부팅 시 정리.

    ⚠️ subprocess 사전 주의사항 [1][2][3] 모두 적용 — spawn_editor 와 동일.

    Returns:
        (성공여부, 사용자 메시지, html_path)
    """
    sid_safe = "".join(c for c in sid_hint if c.isalnum() or c in "-_")[:32] or "current"
    html_path = Path("/tmp") / f"gccfork-tree-{sid_safe}.html"
    try:
        html_path.write_text(html_content, encoding="utf-8")
    except OSError as exc:
        return False, f"❌ HTML 쓰기 실패: {exc}", None

    # 브라우저 spawn — Linux=xdg-open, macOS=open, fallback=$BROWSER
    opener = None
    for cand in ("xdg-open", "open"):
        if shutil.which(cand):
            opener = cand
            break
    if opener is None:
        env_browser = os.environ.get("BROWSER")
        if env_browser and shutil.which(env_browser.split()[0]):
            opener = env_browser
    if opener is None:
        return False, (
            f"❌ 브라우저 opener 못 찾음 (xdg-open / open / $BROWSER 모두 없음). "
            f"수동: file://{html_path}"
        ), html_path

    try:
        subprocess.Popen(
            opener.split() + [str(html_path)],
            stdout=subprocess.DEVNULL,   # ⚠️[2] TUI 화면 보호
            stderr=subprocess.DEVNULL,   # ⚠️[2]
            stdin=subprocess.DEVNULL,
            start_new_session=True,      # ⚠️[1] TUI 죽어도 살아있게
            close_fds=True,
        )
    except FileNotFoundError:
        return False, f"❌ '{opener}' 실행 실패 (race condition)", html_path
    except OSError as exc:
        return False, f"❌ 브라우저 spawn OSError: {exc}", html_path

    return True, f"🌐 브라우저로 트리 열림 ({html_path.name})", html_path
