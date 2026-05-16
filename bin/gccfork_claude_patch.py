"""Claude Code binary patch helper for gccfork.

The hidden slim-reload path needs Claude's /clear command to return
``{type:"skip",value:""}`` instead of ``{type:"text",value:""}``.
Claude Code is distributed as a versioned single-file binary, so this helper
does a same-length byte patch with a backup and reports whether running Claude
processes need a restart.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import shutil
import stat
import time
from typing import Iterable


OLD_CLEAR_RESULT = b'{type:"text",value:""}'
NEW_CLEAR_RESULT = b'{type:"skip",value:""}'
CLEAR_NAME = b'name:"clear"'
BAD_TEST_NAME = b'name:"xxxxx"'
DEFAULT_EXPECTED_COUNT = 2


@dataclass
class BinaryStatus:
    path: str
    exists: bool
    old_count: int = 0
    new_count: int = 0
    clear_name_count: int = 0
    bad_test_name_count: int = 0
    executable: bool = False
    patched: bool = False
    needs_patch: bool = False
    suspicious: bool = False
    error: str | None = None
    backup: str | None = None
    changed: bool = False


@dataclass
class RunningClaude:
    pid: int
    exe: str
    deleted: bool
    old_count: int = 0
    new_count: int = 0
    patched: bool = False
    restart_required: bool = False
    error: str | None = None


@dataclass
class PatchReport:
    target: BinaryStatus | None
    candidates: list[BinaryStatus]
    running: list[RunningClaude]
    action: str
    restart_required: bool
    ok: bool
    message: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)


def _versions_dir() -> Path:
    return Path.home() / ".local" / "share" / "claude" / "versions"


def _is_version_binary(path: Path) -> bool:
    if not path.is_file():
        return False
    name = path.name
    if ".bak" in name or ".tmp" in name or name.startswith("."):
        return False
    return os.access(path, os.X_OK)


def discover_claude_binaries() -> list[Path]:
    """Return versioned Claude Code binaries, newest mtime first."""
    root = _versions_dir()
    if not root.exists():
        return []
    paths = [p for p in root.iterdir() if _is_version_binary(p)]
    paths.sort(key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    return paths


def resolve_target_binary(path: str | os.PathLike[str] | None = None) -> Path | None:
    if path:
        return Path(path).expanduser().resolve()
    bins = discover_claude_binaries()
    return bins[0] if bins else None


def inspect_binary(path: Path) -> BinaryStatus:
    status = BinaryStatus(path=str(path), exists=path.exists())
    if not path.exists():
        status.error = "not found"
        status.suspicious = True
        return status
    try:
        status.executable = os.access(path, os.X_OK)
        data = path.read_bytes()
    except OSError as exc:
        status.error = str(exc)
        status.suspicious = True
        return status

    status.old_count = data.count(OLD_CLEAR_RESULT)
    status.new_count = data.count(NEW_CLEAR_RESULT)
    status.clear_name_count = data.count(CLEAR_NAME)
    status.bad_test_name_count = data.count(BAD_TEST_NAME)
    status.patched = status.old_count == 0 and status.new_count >= DEFAULT_EXPECTED_COUNT
    status.needs_patch = status.old_count == DEFAULT_EXPECTED_COUNT and status.new_count == 0
    status.suspicious = (
        status.bad_test_name_count > 0
        or status.clear_name_count == 0
        or not (status.patched or status.needs_patch)
    )
    return status


def patch_binary(
    path: Path,
    *,
    expected_count: int = DEFAULT_EXPECTED_COUNT,
    force: bool = False,
) -> BinaryStatus:
    status = inspect_binary(path)
    if status.error:
        return status
    if status.patched:
        return status
    if status.bad_test_name_count:
        status.error = "test command-name patch remains in binary; restore from backup first"
        status.suspicious = True
        return status
    if status.old_count != expected_count and not force:
        status.error = f"unexpected clear result count: {status.old_count} (expected {expected_count})"
        status.suspicious = True
        return status

    data = path.read_bytes()
    ts = int(time.time())
    backup = path.with_name(path.name + f".bak-gccfork-clear-skip-{ts}")
    shutil.copy2(path, backup)

    st = path.stat()
    tmp = path.with_name(path.name + f".tmp-gccfork-clear-skip-{os.getpid()}")
    try:
        tmp.write_bytes(data.replace(OLD_CLEAR_RESULT, NEW_CLEAR_RESULT))
        os.chmod(tmp, stat.S_IMODE(st.st_mode))
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass

    status = inspect_binary(path)
    status.backup = str(backup)
    status.changed = True
    return status


def _iter_proc_pids() -> Iterable[int]:
    proc = Path("/proc")
    for item in proc.iterdir():
        if item.name.isdigit():
            yield int(item.name)


def _read_proc_bytes(pid: int, name: str, limit: int = 8192) -> bytes:
    try:
        return (Path("/proc") / str(pid) / name).read_bytes()[:limit]
    except OSError:
        return b""


def discover_running_claude(pids: Iterable[int] | None = None) -> list[RunningClaude]:
    pid_filter = set(pids) if pids is not None else None
    running: list[RunningClaude] = []
    for pid in _iter_proc_pids():
        if pid_filter is not None and pid not in pid_filter:
            continue
        cmdline = _read_proc_bytes(pid, "cmdline")
        comm = _read_proc_bytes(pid, "comm")
        if b"claude" not in cmdline and b"claude" not in comm:
            continue
        exe_link = Path("/proc") / str(pid) / "exe"
        try:
            exe = os.readlink(exe_link)
        except OSError:
            continue
        if "/.local/share/claude/versions/" not in exe and "claude" not in exe:
            continue

        deleted = exe.endswith(" (deleted)")
        clean_exe = exe.removesuffix(" (deleted)")
        item = RunningClaude(pid=pid, exe=exe, deleted=deleted)
        try:
            data = exe_link.read_bytes()
            item.old_count = data.count(OLD_CLEAR_RESULT)
            item.new_count = data.count(NEW_CLEAR_RESULT)
            item.patched = item.old_count == 0 and item.new_count >= DEFAULT_EXPECTED_COUNT
        except OSError as exc:
            item.error = str(exc)
        item.restart_required = deleted or not item.patched
        if clean_exe:
            running.append(item)
    running.sort(key=lambda x: x.pid)
    return running


def check_and_patch(
    *,
    target_path: str | os.PathLike[str] | None = None,
    auto: bool = False,
    force: bool = False,
    running_pids: Iterable[int] | None = None,
) -> PatchReport:
    candidates = [inspect_binary(p) for p in discover_claude_binaries()]
    target = resolve_target_binary(target_path)
    target_status = inspect_binary(target) if target else None
    action = "check"
    ok = True
    message = "Claude clear-skip patch is ready."

    if target_status is None:
        ok = False
        message = "No Claude Code version binary found."
    elif auto and target_status.needs_patch:
        action = "patch"
        target_status = patch_binary(target, force=force)
        ok = target_status.patched and not bool(target_status.error)
        message = "Claude clear-skip patch applied." if ok else f"Claude patch failed: {target_status.error}"
    elif target_status.needs_patch:
        ok = False
        message = "Claude clear-skip patch is missing."
    elif target_status.patched:
        message = "Claude clear-skip patch is already applied."
    else:
        ok = False
        message = target_status.error or "Claude binary patch state is suspicious."

    running = discover_running_claude(running_pids)
    restart_required = any(item.restart_required for item in running)
    if ok and restart_required:
        message = (
            message
            + " Running Claude Code process uses an old/unpatched/deleted binary; restart Claude Code."
        )

    return PatchReport(
        target=target_status,
        candidates=candidates,
        running=running,
        action=action,
        restart_required=restart_required,
        ok=ok,
        message=message,
    )


def format_report(report: PatchReport) -> str:
    lines: list[str] = []
    lines.append(report.message)
    if report.target:
        t = report.target
        lines.append(
            f"target: {t.path} old={t.old_count} skip={t.new_count} "
            f"clear={t.clear_name_count} bad_xxxxx={t.bad_test_name_count}"
        )
        if t.backup:
            lines.append(f"backup: {t.backup}")
        if t.error:
            lines.append(f"error: {t.error}")
    if report.running:
        lines.append("running Claude:")
        for item in report.running:
            state = "restart-required" if item.restart_required else "ok"
            lines.append(
                f"  pid={item.pid} {state} old={item.old_count} skip={item.new_count} exe={item.exe}"
            )
    else:
        lines.append("running Claude: none detected")
    if report.restart_required:
        lines.append("알림: Claude Code를 재시작하면 hidden slim-and-reload가 활성화됩니다.")
    return "\n".join(lines)
