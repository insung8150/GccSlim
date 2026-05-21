"""GccSlim install advisor — opt-in installer modals + helpers.

Sidecar pattern (per project policy: feature-per-module separation).

Covers 3 optional integrations that the user can install either
automatically (TUI first-run modal) or manually (Settings modal's
"권고 설치" tab):

  1. Claude /slim slash-command integration
       - ~/.claude/commands/slim.md
       - ~/.claude/commands/slim/dry.md
       - ~/.claude/hooks/slim-reload-intercept.sh
       - ~/.claude/settings.json UserPromptSubmit hook entry

  2. Codex slim wrapper (Planned — stub modal for now; full asset
     bundle pending release/codex-integration/)

  3. Dingdong notification chime
       - ~/.local/share/gccslim/dingdong.sh
       - ~/.claude/settings.json Stop hook entry

All disk writes go through idempotent helpers — re-running is safe.
Dismiss decisions are persisted via gccfork_sessions.pref_set so the
auto-modal does not re-prompt after refusal (settings tab still works).
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Optional, Callable

# textual imports — runtime-only (PEP 723 inline deps activate via uv)
from textual.app import ComposeResult
from textual.containers import Vertical, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Static, Button


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

CLAUDE_DIR = Path.home() / ".claude"
CLAUDE_SETTINGS = CLAUDE_DIR / "settings.json"
CLAUDE_COMMANDS_DIR = CLAUDE_DIR / "commands"
CLAUDE_HOOKS_DIR = CLAUDE_DIR / "hooks"
GCCSLIM_SHARE = Path.home() / ".local" / "share" / "gccslim"
GCCSLIM_INTEGRATION = GCCSLIM_SHARE / "integration"
LOCAL_BIN = Path.home() / ".local" / "bin"

# Hook script destination + the command form that ends up in settings.json
CLAUDE_SLASH_HOOK_DST = CLAUDE_HOOKS_DIR / "slim-reload-intercept.sh"
CLAUDE_SLASH_HOOK_CMD = f"bash {CLAUDE_SLASH_HOOK_DST}"
CLAUDE_SLASH_EVENT = "UserPromptSubmit"

DINGDONG_DST = GCCSLIM_SHARE / "dingdong.sh"
DINGDONG_HOOK_CMD = f"bash {DINGDONG_DST}"
DINGDONG_EVENT = "Stop"
DINGDONG_EVENTS = ("Stop", "Notification")
PROJECT_PREFS_FILE = Path(".gccfork") / "ccfork-prefs.json"
GLOBAL_REGISTRY_PATH = Path.home() / ".claude" / "gccfork-registry.json"
VSCODE_SCROLLBACK_RECOMMENDED = 100000


# ---------------------------------------------------------------------------
# Asset locator — find the bundled release/ directory regardless of how
# GccSlim was installed.
# ---------------------------------------------------------------------------

def _release_dir() -> Optional[Path]:
    """Locate the directory holding sanitized integration assets.

    Search order:
      1. $GCCSLIM_RELEASE_DIR (explicit override)
      2. ~/.local/share/gccslim/integration/  (install.sh's drop point)
      3. <this-file-dir>/../release/          (dev source layout)
      4. <this-file-dir>/release/             (flat layout)

    Returns None if no candidate has the expected sub-directories.
    """
    candidates: list[Path] = []
    env = os.environ.get("GCCSLIM_RELEASE_DIR")
    if env:
        candidates.append(Path(env).expanduser())
    candidates.append(GCCSLIM_INTEGRATION)
    here = Path(__file__).resolve().parent
    candidates.append(here.parent / "release")
    candidates.append(here / "release")

    for cand in candidates:
        if not cand.exists():
            continue
        # A valid release dir must contain at least one of our sub-bundles.
        if (cand / "claude-integration").is_dir() or (cand / "optional").is_dir():
            return cand
    return None


# ---------------------------------------------------------------------------
# settings.json idempotent patch helpers
# ---------------------------------------------------------------------------

def _load_settings() -> dict:
    if not CLAUDE_SETTINGS.exists():
        return {}
    try:
        return json.loads(CLAUDE_SETTINGS.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_settings_atomic(data: dict) -> bool:
    """Atomic write — tmp file + rename. Backs up the prior file."""
    try:
        CLAUDE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        if CLAUDE_SETTINGS.exists():
            ts = int(time.time())
            bak = CLAUDE_SETTINGS.with_suffix(f".json.bak-gccslim-{ts}")
            try:
                shutil.copy2(CLAUDE_SETTINGS, bak)
            except Exception:
                pass
        with NamedTemporaryFile(
            "w",
            dir=str(CLAUDE_SETTINGS.parent),
            prefix=".settings.tmp-",
            delete=False,
            encoding="utf-8",
        ) as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            tmp_path = Path(f.name)
        os.replace(tmp_path, CLAUDE_SETTINGS)
        return True
    except Exception:
        return False


def hook_entry_exists(event: str, command_substring: str) -> bool:
    """Return True iff settings.json already has a hook entry under
    ``event`` whose command contains ``command_substring``."""
    settings = _load_settings()
    for matcher_block in settings.get("hooks", {}).get(event, []):
        for hook in matcher_block.get("hooks", []):
            if command_substring in hook.get("command", ""):
                return True
    return False


def patch_settings_hook(event: str, command: str) -> bool:
    """Add a hook entry (matcher='') idempotently. No-op if present."""
    settings = _load_settings()
    if hook_entry_exists(event, command):
        return True

    event_list = settings.setdefault("hooks", {}).setdefault(event, [])
    target = None
    for block in event_list:
        if block.get("matcher", "") == "":
            target = block
            break
    if target is None:
        target = {"matcher": "", "hooks": []}
        event_list.append(target)
    target.setdefault("hooks", []).append({"type": "command", "command": command})

    return _save_settings_atomic(settings)


def remove_settings_hook(event: str, command_substring: str) -> bool:
    """Remove any hook entry under ``event`` whose command contains the
    substring. Cleans up empty matcher blocks and the event key itself."""
    settings = _load_settings()
    blocks = settings.get("hooks", {}).get(event, [])
    if not blocks:
        return True
    changed = False
    for block in blocks:
        before = len(block.get("hooks", []))
        block["hooks"] = [
            h for h in block.get("hooks", [])
            if command_substring not in h.get("command", "")
        ]
        if before != len(block["hooks"]):
            changed = True
    settings["hooks"][event] = [b for b in blocks if b.get("hooks")]
    if not settings["hooks"][event]:
        settings["hooks"].pop(event, None)
    if not settings.get("hooks"):
        settings.pop("hooks", None)
    if not changed:
        return True
    return _save_settings_atomic(settings)


# ---------------------------------------------------------------------------
# Dismiss pref (so the auto-modal stops nagging after the user declines)
# ---------------------------------------------------------------------------

def _is_dismissed(key: str) -> bool:
    try:
        from gccfork_sessions import pref_get
        return bool(pref_get(key, False))
    except Exception:
        return False


def _set_dismissed(key: str, value: bool = True) -> None:
    try:
        from gccfork_sessions import pref_set
        pref_set(key, bool(value))
    except Exception:
        pass


# ===========================================================================
#  1) Claude /slim slash-command integration
# ===========================================================================

DISMISS_KEY_CLAUDE_SLASH = "claude_slash_install_dismissed"


def claude_slash_needs_install() -> bool:
    """True iff one or more of the required assets is missing.

    Checks all four anchors — single asset missing = whole integration
    considered "not installed" for safety.
    """
    if not (CLAUDE_COMMANDS_DIR / "slim.md").exists():
        return True
    if not (CLAUDE_COMMANDS_DIR / "slim" / "dry.md").exists():
        return True
    if not CLAUDE_SLASH_HOOK_DST.exists():
        return True
    if not hook_entry_exists(CLAUDE_SLASH_EVENT, "slim-reload-intercept.sh"):
        return True
    return False


def apply_claude_slash_install() -> tuple[bool, str]:
    """Install all Claude /slim assets. Returns (success, message)."""
    rel = _release_dir()
    if rel is None:
        return False, (
            "release/ 디렉터리를 찾을 수 없습니다. "
            "$GCCSLIM_RELEASE_DIR 환경변수를 설정하거나 "
            "~/.local/share/gccslim/integration/ 에 자산을 두세요."
        )
    src = rel / "claude-integration"
    if not src.is_dir():
        return False, f"claude-integration 자산 부재: {src}"

    try:
        CLAUDE_COMMANDS_DIR.mkdir(parents=True, exist_ok=True)
        (CLAUDE_COMMANDS_DIR / "slim").mkdir(parents=True, exist_ok=True)
        CLAUDE_HOOKS_DIR.mkdir(parents=True, exist_ok=True)

        shutil.copy2(src / "slim.md", CLAUDE_COMMANDS_DIR / "slim.md")
        shutil.copy2(src / "dry.md", CLAUDE_COMMANDS_DIR / "slim" / "dry.md")
        shutil.copy2(src / "slim-reload-intercept.sh", CLAUDE_SLASH_HOOK_DST)
        os.chmod(CLAUDE_SLASH_HOOK_DST, 0o755)
    except Exception as exc:
        return False, f"파일 복사 실패: {exc}"

    if not patch_settings_hook(CLAUDE_SLASH_EVENT, CLAUDE_SLASH_HOOK_CMD):
        return False, "settings.json 패치 실패."

    return True, "/slim 통합 설치 완료. 새 터미널에서 claude 띄우면 작동합니다."


def uninstall_claude_slash() -> tuple[bool, str]:
    removed: list[str] = []
    try:
        for p in (
            CLAUDE_COMMANDS_DIR / "slim.md",
            CLAUDE_COMMANDS_DIR / "slim" / "dry.md",
            CLAUDE_SLASH_HOOK_DST,
        ):
            if p.exists():
                p.unlink()
                removed.append(str(p))
        slim_dir = CLAUDE_COMMANDS_DIR / "slim"
        if slim_dir.exists() and not any(slim_dir.iterdir()):
            slim_dir.rmdir()
    except Exception as exc:
        return False, f"파일 제거 실패: {exc}"
    if not remove_settings_hook(CLAUDE_SLASH_EVENT, "slim-reload-intercept.sh"):
        return False, "settings.json 정리 실패."
    return True, f"제거 완료 ({len(removed)} 파일)."


# ===========================================================================
#  2) Codex /slim integration
# ===========================================================================

DISMISS_KEY_CODEX_WRAPPER = "codex_wrapper_install_dismissed"

CODEX_WRAPPER_BIN = LOCAL_BIN / "codex"
CODEX_SLIM_NOW_BIN = LOCAL_BIN / "codex-slim-now"
CODEX_SLIM_LOOP_BIN = LOCAL_BIN / "codex-slim-loop"
CODEX_SLIM_RELOAD_MODULE = LOCAL_BIN / "gccfork_codex_slim_reload.py"
CODEX_SLIM_LOOP_MODULE = LOCAL_BIN / "gccfork_codex_slim_loop.py"
CODEX_PATCHED_BIN = Path.home() / ".local" / "opt" / "codex-patched" / "bin" / "codex"


def _text_file_contains(path: Path, needle: str) -> bool:
    try:
        return needle in path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False


def _binary_contains(path: Path, needle: str) -> bool:
    if not path.exists():
        return False
    rg = shutil.which("rg")
    if rg:
        try:
            return subprocess.run(
                [rg, "-a", "-q", needle, str(path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=8,
                check=False,
            ).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False
    try:
        return needle.encode("utf-8") in path.read_bytes()
    except OSError:
        return False


def codex_wrapper_check_details() -> list[tuple[str, str, bool]]:
    """Per-anchor status for the Codex /slim runtime integration."""
    codex_on_path = shutil.which("codex") or "(PATH 에 codex 없음)"
    slim_now_on_path = shutil.which("codex-slim-now") or "(PATH 에 codex-slim-now 없음)"
    slim_loop_on_path = shutil.which("codex-slim-loop") or "(PATH 에 codex-slim-loop 없음)"
    return [
        ("codex 명령", codex_on_path, bool(shutil.which("codex"))),
        ("codex wrapper", str(CODEX_WRAPPER_BIN), CODEX_WRAPPER_BIN.exists()),
        (
            "wrapper plaintext compact env",
            "CODEX_GCCSLIM_PLAINTEXT_COMPACT",
            _text_file_contains(CODEX_WRAPPER_BIN, "CODEX_GCCSLIM_PLAINTEXT_COMPACT"),
        ),
        (
            "wrapper slim loop",
            "codex_slim_loop.py / codex-slim-loop",
            _text_file_contains(CODEX_WRAPPER_BIN, "codex_slim_loop.py")
            or _text_file_contains(CODEX_WRAPPER_BIN, "codex-slim-loop"),
        ),
        ("codex-slim-now", slim_now_on_path, bool(shutil.which("codex-slim-now"))),
        ("codex-slim-loop", slim_loop_on_path, bool(shutil.which("codex-slim-loop"))),
        (
            "slim reload module",
            str(CODEX_SLIM_RELOAD_MODULE),
            CODEX_SLIM_RELOAD_MODULE.exists(),
        ),
        (
            "slim loop module",
            str(CODEX_SLIM_LOOP_MODULE),
            CODEX_SLIM_LOOP_MODULE.exists(),
        ),
        (
            "patched codex plaintext marker",
            str(CODEX_PATCHED_BIN),
            _binary_contains(CODEX_PATCHED_BIN, "CODEX_GCCSLIM_PLAINTEXT_COMPACT"),
        ),
        (
            "patched codex /slim marker",
            str(CODEX_PATCHED_BIN),
            _binary_contains(CODEX_PATCHED_BIN, "codex-slim-now"),
        ),
    ]


def codex_wrapper_installed() -> bool:
    details = dict((label, ok) for label, _target, ok in codex_wrapper_check_details())
    required = (
        "codex 명령",
        "codex wrapper",
        "wrapper plaintext compact env",
        "wrapper slim loop",
        "codex-slim-now",
        "codex-slim-loop",
        "slim reload module",
        "slim loop module",
        "patched codex plaintext marker",
        "patched codex /slim marker",
    )
    return all(details.get(label, False) for label in required)


def codex_wrapper_needs_install() -> bool:
    """True when the local Codex /slim runtime integration is incomplete."""
    if codex_wrapper_installed():
        return False
    rel = _release_dir()
    if rel is None:
        return False
    if not (rel / "codex-integration").is_dir():
        return False
    return True


def apply_codex_wrapper_install() -> tuple[bool, str]:
    if codex_wrapper_installed():
        return True, "Codex /slim 통합은 이미 설치되어 있습니다."
    return False, (
        "Codex /slim 자동 설치 자산은 이 배포본에 없습니다. "
        "개발본에서는 scripts/patch_codex_for_gccslim.sh 로 설치/패치합니다."
    )


def uninstall_codex_wrapper() -> tuple[bool, str]:
    return False, (
        "Codex /slim 통합은 codex wrapper와 patched binary를 포함하므로 "
        "설정 탭에서 자동 제거하지 않습니다. 필요하면 ~/.local/bin/codex 를 "
        "원래 codex 실행 파일로 되돌려야 합니다."
    )


# ===========================================================================
#  3) Dingdong notification chime
# ===========================================================================

DISMISS_KEY_DINGDONG = "dingdong_install_dismissed"


_DEPS_CACHE: tuple[bool, str] | None = None


def _host_python() -> Optional[str]:
    """Return a path to the *system* python3, ignoring uv-script venv.

    The TUI runs inside a uv-managed PEP 723 venv whose `sys.executable`
    is the venv python (numpy NOT installed). The Stop hook, however, is
    spawned by claude itself and uses the system python3 — so that's the
    interpreter whose numpy availability actually matters.
    """
    for cand in ("/usr/bin/python3", "/usr/local/bin/python3"):
        if Path(cand).exists():
            return cand
    # PATH fallback — sanitize VIRTUAL_ENV first
    env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV",)}
    pth = env.get("PATH", "")
    for d in pth.split(os.pathsep):
        if "/uv/" in d or "/.venv/" in d:
            continue
        p = Path(d) / "python3"
        if p.exists():
            return str(p)
    which = shutil.which("python3")
    return which


def dingdong_dependencies_ok() -> tuple[bool, str]:
    """Verify the runtime can actually play the chime.

    Returns (ok, reason). reason is empty when ok. Cached after first call.
    """
    global _DEPS_CACHE
    if _DEPS_CACHE is not None:
        return _DEPS_CACHE
    # macOS — afplay is enough.
    if shutil.which("afplay"):
        _DEPS_CACHE = (True, "")
        return _DEPS_CACHE
    # Linux — need aplay + system python3 + numpy.
    if not shutil.which("aplay"):
        _DEPS_CACHE = (False, "aplay (ALSA) 미설치 — 패키지 매니저로 alsa-utils 설치 필요.")
        return _DEPS_CACHE
    py = _host_python()
    if not py:
        _DEPS_CACHE = (False, "python3 미설치.")
        return _DEPS_CACHE
    # Sanitize env — strip VIRTUAL_ENV so /usr/bin/python3 doesn't see the
    # uv venv's site-packages.
    env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV",)}
    try:
        subprocess.run(
            [py, "-c", "import numpy"],
            check=True,
            capture_output=True,
            timeout=5,
            env=env,
        )
    except Exception:
        _DEPS_CACHE = (False, f"{py} 의 numpy 패키지 미설치 (pip install numpy).")
        return _DEPS_CACHE
    _DEPS_CACHE = (True, "")
    return _DEPS_CACHE


def _ensure_dingdong_script_installed() -> tuple[bool, str]:
    if DINGDONG_DST.exists():
        return True, ""
    rel = _release_dir()
    if rel is None:
        return False, "release/ 디렉터리를 찾을 수 없습니다."
    src = rel / "optional" / "dingdong.sh"
    if not src.exists():
        return False, f"sample 부재: {src}"
    try:
        GCCSLIM_SHARE.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, DINGDONG_DST)
        os.chmod(DINGDONG_DST, 0o755)
    except Exception as exc:
        return False, f"sample 복사 실패: {exc}"
    return True, ""


def dingdong_needs_install() -> bool:
    """True iff the standard GccSlim chime script or Claude hooks are missing."""
    if not DINGDONG_DST.exists():
        return True
    return any(not hook_entry_exists(event, str(DINGDONG_DST)) for event in DINGDONG_EVENTS)


def _read_project_prefs() -> dict:
    path = Path.cwd() / PROJECT_PREFS_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    return data if isinstance(data, dict) else {}


def _write_project_pref(key: str, value: object) -> bool:
    path = Path.cwd() / PROJECT_PREFS_FILE
    data = _read_project_prefs()
    data[key] = value
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return False
    return True


def _read_global_prefs() -> dict:
    try:
        registry = json.loads(GLOBAL_REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    prefs = registry.get("prefs", {}) if isinstance(registry, dict) else {}
    return prefs if isinstance(prefs, dict) else {}


def _pref_bool(key: str, default: bool = False) -> bool:
    value = _read_project_prefs().get(key, _read_global_prefs().get(key, default))
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def codex_dingdong_installed() -> bool:
    return (
        _pref_bool("codex_dingdong_enabled", False)
        and codex_wrapper_installed()
        and DINGDONG_DST.exists()
    )


def codex_dingdong_check_details() -> list[tuple[str, str, bool]]:
    return [
        (
            "프로젝트 설정",
            ".gccfork/ccfork-prefs.json: codex_dingdong_enabled",
            _pref_bool("codex_dingdong_enabled", False),
        ),
        ("Codex wrapper", "~/.local/bin/codex", codex_wrapper_installed()),
        ("표준 딩동댕 스크립트", "~/.local/share/gccslim/dingdong.sh", DINGDONG_DST.exists()),
    ]


# ===========================================================================
#  4) VS Code terminal scrollback recommendation
# ===========================================================================


def _vscode_settings_candidates() -> list[Path]:
    home = Path.home()
    sysname = platform.system()
    if sysname == "Darwin":
        base = home / "Library" / "Application Support"
        return [
            base / "Code" / "User" / "settings.json",
            base / "Code - Insiders" / "User" / "settings.json",
            base / "VSCodium" / "User" / "settings.json",
        ]
    if sysname == "Windows":
        appdata = os.environ.get("APPDATA")
        if appdata:
            base = Path(appdata)
            return [
                base / "Code" / "User" / "settings.json",
                base / "Code - Insiders" / "User" / "settings.json",
                base / "VSCodium" / "User" / "settings.json",
            ]
    config = home / ".config"
    return [
        config / "Code" / "User" / "settings.json",
        config / "Code - Insiders" / "User" / "settings.json",
        config / "VSCodium" / "User" / "settings.json",
    ]


def _vscode_settings_path() -> Path:
    candidates = _vscode_settings_candidates()
    for path in candidates:
        if path.exists():
            return path
    return candidates[0]


def _strip_json_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"(^|[ \t])//.*?$", r"\1", text, flags=re.M)
    return text


def _read_vscode_settings() -> tuple[dict, Path, str]:
    path = _vscode_settings_path()
    if not path.exists():
        return {}, path, ""
    raw = path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = json.loads(_strip_json_comments(raw))
    if not isinstance(data, dict):
        data = {}
    return data, path, raw


def vscode_scrollback_value() -> Optional[int]:
    try:
        data, _path, _raw = _read_vscode_settings()
    except Exception:
        return None
    value = data.get("terminal.integrated.scrollback")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def vscode_scrollback_installed() -> bool:
    value = vscode_scrollback_value()
    return bool(value is not None and value >= VSCODE_SCROLLBACK_RECOMMENDED)


def vscode_scrollback_check_details() -> list[tuple[str, str, bool]]:
    path = _vscode_settings_path()
    value = vscode_scrollback_value()
    current = "(미설정)" if value is None else str(value)
    return [
        ("VS Code settings.json", str(path), path.exists()),
        (
            "terminal.integrated.scrollback",
            f"{current} / 권고 {VSCODE_SCROLLBACK_RECOMMENDED}",
            value is not None and value >= VSCODE_SCROLLBACK_RECOMMENDED,
        ),
    ]


def apply_vscode_scrollback_install() -> tuple[bool, str]:
    try:
        data, path, raw = _read_vscode_settings()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            ts = int(time.time())
            bak = path.with_suffix(f".json.bak-gccslim-scrollback-{ts}")
            try:
                bak.write_text(raw if raw else path.read_text(encoding="utf-8"), encoding="utf-8")
            except Exception:
                pass
        data["terminal.integrated.scrollback"] = VSCODE_SCROLLBACK_RECOMMENDED
        with NamedTemporaryFile(
            "w",
            dir=str(path.parent),
            prefix=".settings.tmp-",
            delete=False,
            encoding="utf-8",
        ) as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            tmp_path = Path(f.name)
        os.replace(tmp_path, path)
    except Exception as exc:
        return False, f"VS Code scrollback 설정 실패: {exc}"
    return True, f"VS Code 터미널 히스토리를 {VSCODE_SCROLLBACK_RECOMMENDED}줄로 설정했습니다."


def apply_codex_dingdong_install() -> tuple[bool, str]:
    if not codex_wrapper_installed():
        return False, "Codex wrapper 통합이 먼저 설치되어야 합니다."
    ok, msg = _ensure_dingdong_script_installed()
    if not ok:
        return False, msg
    if not _write_project_pref("codex_dingdong_enabled", True):
        return False, "프로젝트 prefs 저장 실패."
    return True, "Codex 작업 완료 알림을 켰습니다. 새 Codex 세션부터 적용됩니다."


def uninstall_codex_dingdong() -> tuple[bool, str]:
    if not _write_project_pref("codex_dingdong_enabled", False):
        return False, "프로젝트 prefs 저장 실패."
    return True, "Codex 작업 완료 알림을 껐습니다. 새 Codex 세션부터 적용됩니다."


def claude_slash_check_details() -> list[tuple[str, str, bool]]:
    """Per-anchor install status for the Claude /slim integration.

    Returns a list of (label, path-or-target, exists) for display.
    """
    settings = _load_settings()
    hook_match = ""
    for blk in settings.get("hooks", {}).get(CLAUDE_SLASH_EVENT, []):
        for h in blk.get("hooks", []):
            cmd = h.get("command", "")
            if "slim-reload-intercept.sh" in cmd:
                hook_match = cmd
                break
        if hook_match:
            break
    return [
        ("commands/slim.md", str(CLAUDE_COMMANDS_DIR / "slim.md"),
         (CLAUDE_COMMANDS_DIR / "slim.md").exists()),
        ("commands/slim/dry.md", str(CLAUDE_COMMANDS_DIR / "slim" / "dry.md"),
         (CLAUDE_COMMANDS_DIR / "slim" / "dry.md").exists()),
        ("hooks 스크립트", str(CLAUDE_SLASH_HOOK_DST), CLAUDE_SLASH_HOOK_DST.exists()),
        ("settings.json UserPromptSubmit", hook_match or "(미등록)", bool(hook_match)),
    ]


def dingdong_check_details() -> list[tuple[str, str, bool]]:
    """Per-anchor install status for the standard GccSlim dingdong hooks."""
    settings = _load_settings()
    out = [("표준 경로 스크립트", "~/.local/share/gccslim/dingdong.sh", DINGDONG_DST.exists())]
    for event in DINGDONG_EVENTS:
        hook_match = ""
        for blk in settings.get("hooks", {}).get(event, []):
            for h in blk.get("hooks", []):
                cmd = h.get("command", "")
                if str(DINGDONG_DST) in cmd:
                    hook_match = cmd
                    break
            if hook_match:
                break
        out.append((f"settings.json {event} hook", hook_match or "(미등록)", bool(hook_match)))
        if hook_match:
            out[-1] = (f"settings.json {event} hook", "bash ~/.local/share/gccslim/dingdong.sh", True)
    return out


def apply_dingdong_install() -> tuple[bool, str]:
    ok, msg = _ensure_dingdong_script_installed()
    if not ok:
        return False, msg

    for event in DINGDONG_EVENTS:
        if not patch_settings_hook(event, DINGDONG_HOOK_CMD):
            return False, f"settings.json {event} hook 등록 실패."
    return True, "딩동댕 알림 설치 완료. 다음 답변 끝나면 차임벨 재생."


def uninstall_dingdong() -> tuple[bool, str]:
    """Remove ONLY the standard GccSlim dingdong hooks and script."""
    try:
        if DINGDONG_DST.exists():
            DINGDONG_DST.unlink()
    except Exception as exc:
        return False, f"파일 제거 실패: {exc}"
    for event in DINGDONG_EVENTS:
        if not remove_settings_hook(event, str(DINGDONG_DST)):
            return False, f"settings.json {event} 정리 실패 (표준 경로 entry 만 제거됨)."
    return True, "표준 경로의 딩동댕 알림 제거 완료."


# ===========================================================================
#  Modal screens — shared shell + 3 concrete subclasses
# ===========================================================================

_SHARED_CSS = """
$ModalScreen {
    align: center middle;
}
#adv-box {
    width: 78;
    height: auto;
    max-height: 90%;
    border: round $accent 35%;
    padding: 0;
    background: $surface;
}
#adv-header {
    padding: 1 2;
    border-bottom: hkey $accent 20%;
    height: auto;
}
#adv-title {
    color: $accent;
    text-style: bold;
}
#adv-meta {
    color: $foreground 60%;
}
#adv-body {
    padding: 1 2;
    height: auto;
}
.adv-line {
    height: auto;
    color: $foreground 85%;
    margin: 0 0 1 0;
}
.adv-warn {
    color: $accent;
    text-style: bold;
}
#adv-btn-row {
    padding: 1 2;
    border-top: hkey $accent 20%;
    height: auto;
}
#adv-btn-row Button {
    margin: 0 1 0 0;
}
"""


class _AdvisorModalBase(ModalScreen[bool]):
    """Common shell for the three install modals.

    Subclasses override TITLE / META / BODY_LINES / install button text /
    `_apply()` to perform the actual disk writes.
    """

    DEFAULT_CSS = _SHARED_CSS

    TITLE: str = ""
    META: str = ""
    BODY_LINES: tuple[str, ...] = ()
    INSTALL_LABEL: str = "지금 설치"
    DISMISS_KEY: str = ""

    def _apply(self) -> tuple[bool, str]:
        return False, "not implemented"

    def compose(self) -> ComposeResult:
        with Vertical(id="adv-box"):
            with Vertical(id="adv-header"):
                yield Static(self.TITLE, id="adv-title")
                if self.META:
                    yield Static(self.META, id="adv-meta")
            with Vertical(id="adv-body"):
                for line in self.BODY_LINES:
                    yield Static(line, classes="adv-line")
            with Horizontal(id="adv-btn-row"):
                yield Button("다음에 안 함", id="adv-dismiss", variant="default")
                yield Static("", classes="spacer")
                yield Button("나중에", id="adv-later", variant="default")
                yield Button(self.INSTALL_LABEL, id="adv-install", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "adv-dismiss":
            if self.DISMISS_KEY:
                _set_dismissed(self.DISMISS_KEY, True)
            try:
                self.app.notify("다시 안내하지 않습니다. 설정 → 권고 설치 탭에서 언제든 설치 가능합니다.", timeout=4)
            except Exception:
                pass
            self.dismiss(False)
        elif bid == "adv-later":
            self.dismiss(False)
        elif bid == "adv-install":
            ok, msg = self._apply()
            try:
                self.app.notify(
                    msg,
                    severity="information" if ok else "error",
                    timeout=6,
                )
            except Exception:
                pass
            self.dismiss(ok)


class ClaudeSlashInstallScreen(_AdvisorModalBase):
    TITLE = "📝 Claude /slim 슬래시 통합 설치"
    META = "Claude TUI 안에서 /slim, /slim:dry 슬래시 명령을 사용할 수 있도록 자산을 설치합니다."
    BODY_LINES = (
        "설치 항목:",
        "  ~/.claude/commands/slim.md",
        "  ~/.claude/commands/slim/dry.md",
        "  ~/.claude/hooks/slim-reload-intercept.sh",
        "  ~/.claude/settings.json — UserPromptSubmit hook entry (idempotent)",
        "",
        "기존 settings.json 은 timestamped backup 으로 보존됩니다.",
        "GccSlim TUI 가 떠 있을 때만 슬래시가 작동합니다.",
    )
    INSTALL_LABEL = "지금 설치"
    DISMISS_KEY = DISMISS_KEY_CLAUDE_SLASH

    def _apply(self) -> tuple[bool, str]:
        return apply_claude_slash_install()


class CodexWrapperInstallScreen(_AdvisorModalBase):
    TITLE = "🦊 Codex slim wrapper 통합"
    META = "Codex 세션에서 slim 자동 reload 가 가능하도록 ~/.local/bin/codex wrapper 를 설치합니다."
    BODY_LINES = (
        "현재 release 에서는 준비 중입니다.",
        "다음 release 에서 codex-integration/ 자산이 동봉됩니다.",
        "",
        "그동안은 'Codex 의 slim 자동 reload' 기능 없이 기본 codex 만 사용됩니다.",
    )
    INSTALL_LABEL = "준비 중"
    DISMISS_KEY = DISMISS_KEY_CODEX_WRAPPER

    def _apply(self) -> tuple[bool, str]:
        return apply_codex_wrapper_install()


class DingdongInstallScreen(_AdvisorModalBase):
    TITLE = "🔔 딩동댕 알림 설치 (권고)"
    META = "Claude 답변이 끝나면 짧은 차임벨 (솔–미–도) 을 재생합니다. 선택 사항입니다."
    BODY_LINES = (
        "설치 항목:",
        "  ~/.local/share/gccslim/dingdong.sh  (sample)",
        "  ~/.claude/settings.json — Stop hook entry (idempotent)",
        "",
        "Linux: aplay + python3 + numpy 필요.",
        "macOS: afplay (기본 내장) 사용.",
        "의존성이 없으면 hook 이 silent exit — 다른 동작에 영향 없음.",
    )
    INSTALL_LABEL = "지금 설치"
    DISMISS_KEY = DISMISS_KEY_DINGDONG

    def _apply(self) -> tuple[bool, str]:
        return apply_dingdong_install()


# ===========================================================================
#  Auto-modal trigger helpers — called from bin/gccfork's on_mount
# ===========================================================================

def maybe_show_claude_slash_modal(app) -> None:
    if _is_dismissed(DISMISS_KEY_CLAUDE_SLASH):
        return
    if not claude_slash_needs_install():
        return
    try:
        app.push_screen(ClaudeSlashInstallScreen())
    except Exception:
        pass


def maybe_show_codex_wrapper_modal(app) -> None:
    if _is_dismissed(DISMISS_KEY_CODEX_WRAPPER):
        return
    if not codex_wrapper_needs_install():
        return
    try:
        app.push_screen(CodexWrapperInstallScreen())
    except Exception:
        pass


def maybe_show_dingdong_modal(app) -> None:
    if _is_dismissed(DISMISS_KEY_DINGDONG):
        return
    if not dingdong_needs_install():
        return
    # Only auto-prompt when dependencies are actually available — otherwise
    # the install would fail anyway. Settings tab still lets the user try.
    ok, _ = dingdong_dependencies_ok()
    if not ok:
        return
    try:
        app.push_screen(DingdongInstallScreen())
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Status summary — used by the Settings "권고 설치" tab to render cards.
# ---------------------------------------------------------------------------

def install_status_summary() -> dict:
    """Snapshot of the three integrations' install state.

    Returns:
      {
        "claude_slash": {"installed": bool, "dismissed": bool},
        "codex_wrapper": {"installed": bool, "dismissed": bool, "available": bool},
        "dingdong": {"installed": bool, "dismissed": bool, "deps_ok": bool, "deps_reason": str},
      }
    """
    deps_ok, deps_reason = dingdong_dependencies_ok()
    codex_available = False
    rel = _release_dir()
    if rel is not None:
        codex_available = (rel / "codex-integration").is_dir()
    return {
        "claude_slash": {
            "installed": not claude_slash_needs_install(),
            "dismissed": _is_dismissed(DISMISS_KEY_CLAUDE_SLASH),
            "details": claude_slash_check_details(),
        },
        "codex_wrapper": {
            "installed": codex_wrapper_installed(),
            "dismissed": _is_dismissed(DISMISS_KEY_CODEX_WRAPPER),
            "available": codex_available,
            "details": codex_wrapper_check_details(),
        },
        "dingdong": {
            "installed": not dingdong_needs_install(),
            "dismissed": _is_dismissed(DISMISS_KEY_DINGDONG),
            "deps_ok": deps_ok,
            "deps_reason": deps_reason,
            "details": dingdong_check_details(),
            "standard_path_present": DINGDONG_DST.exists(),
        },
        "codex_dingdong": {
            "installed": codex_dingdong_installed(),
            "dismissed": False,
            "deps_ok": deps_ok,
            "deps_reason": deps_reason,
            "details": codex_dingdong_check_details(),
        },
        "vscode_scrollback": {
            "installed": vscode_scrollback_installed(),
            "dismissed": False,
            "value": vscode_scrollback_value(),
            "recommended": VSCODE_SCROLLBACK_RECOMMENDED,
            "details": vscode_scrollback_check_details(),
        },
    }


__all__ = [
    # helpers
    "patch_settings_hook",
    "remove_settings_hook",
    "hook_entry_exists",
    # Claude slash
    "claude_slash_needs_install",
    "claude_slash_check_details",
    "apply_claude_slash_install",
    "uninstall_claude_slash",
    "ClaudeSlashInstallScreen",
    "maybe_show_claude_slash_modal",
    # Codex wrapper
    "codex_wrapper_needs_install",
    "codex_wrapper_installed",
    "codex_wrapper_check_details",
    "apply_codex_wrapper_install",
    "uninstall_codex_wrapper",
    "CodexWrapperInstallScreen",
    "maybe_show_codex_wrapper_modal",
    # Dingdong
    "dingdong_needs_install",
    "dingdong_dependencies_ok",
    "dingdong_check_details",
    "apply_dingdong_install",
    "uninstall_dingdong",
    "codex_dingdong_installed",
    "codex_dingdong_check_details",
    "apply_codex_dingdong_install",
    "uninstall_codex_dingdong",
    "vscode_scrollback_installed",
    "vscode_scrollback_check_details",
    "apply_vscode_scrollback_install",
    "VSCODE_SCROLLBACK_RECOMMENDED",
    "DingdongInstallScreen",
    "maybe_show_dingdong_modal",
    # Summary
    "install_status_summary",
    # Dismiss keys (for settings tab UI)
    "DISMISS_KEY_CLAUDE_SLASH",
    "DISMISS_KEY_CODEX_WRAPPER",
    "DISMISS_KEY_DINGDONG",
]
