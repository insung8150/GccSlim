"""Same-terminal slim/restart loop for Codex CLI.

Codex does not need Claude-style in-place `/clear` + `/resume`.  The stable
path is:

1. run Codex under this wrapper,
2. slim the active JSONL from inside the Codex process,
3. write a marker,
4. terminate the current Codex process,
5. let the wrapper run `codex resume <sid>` in the same terminal.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid
from typing import Any

try:
    from .codex_slim_reload import (
        CODEX_ROOT,
        apply_slim_plan,
        build_slim_plan,
        fmt_size,
        _proc_codex_jsonl,
        _read_proc_cmdline,
        _read_proc_stat_ppid,
        _cwd_from_rows,
        _load_jsonl_rows,
    )
except ImportError:
    # Public GccSlim installs this module as a ~/.local/bin sidecar instead of
    # a package under src/gccfork.
    from gccfork_codex_slim_reload import (
        CODEX_ROOT,
        apply_slim_plan,
        build_slim_plan,
        fmt_size,
        _proc_codex_jsonl,
        _read_proc_cmdline,
        _read_proc_stat_ppid,
        _cwd_from_rows,
        _load_jsonl_rows,
    )


DEFAULT_RUNTIME_DIR = Path("/tmp")
DEFAULT_MARKER = DEFAULT_RUNTIME_DIR / f"gccfork-codex-slim-reload-{os.getuid()}.json"
DEFAULT_CODEX_BIN = Path.home() / ".local" / "opt" / "codex-patched" / "bin" / "codex"
PROJECT_PREFS_FILE = Path(".gccfork") / "ccfork-prefs.json"
GLOBAL_REGISTRY_PATH = Path.home() / ".claude" / "gccfork-registry.json"


@dataclass(frozen=True)
class SlimReloadMarker:
    request_id: str
    session_id: str
    session_file: str
    cwd: str | None
    mode: str
    keep_recent: int
    created_at: float
    source_pid: int
    wrapper_id: str | None


@dataclass(frozen=True)
class SlimRuntimeState:
    wrapper_id: str
    codex_pid: int
    session_id: str | None
    session_file: str | None
    cwd: str | None
    updated_at: float


def _sanitize_env_for_codex(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    if extra:
        env.update(extra)
    return env


def _read_json_dict(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _read_gccfork_prefs(cwd: Path | None = None) -> dict[str, Any]:
    """Mirror gccfork pref policy: project prefs if present, else global prefs."""
    base = cwd or Path.cwd()
    project_prefs = _read_json_dict(base / PROJECT_PREFS_FILE)
    if project_prefs is not None:
        return project_prefs
    registry = _read_json_dict(GLOBAL_REGISTRY_PATH) or {}
    prefs = registry.get("prefs", {})
    return prefs if isinstance(prefs, dict) else {}


def _pref_get(key: str, default: Any = None) -> Any:
    return _read_gccfork_prefs().get(key, default)


def _positive_int(value: Any, default: int | None = None) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _bool_pref(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _default_codex_slim_mode(*, prefer_prefs: bool = False) -> str:
    pref_value = _pref_get("codex_slim_default_mode", None)
    env_value = os.environ.get("CODEX_SLIM_MODE")
    if prefer_prefs:
        return str(pref_value or env_value or "strong")
    return str(env_value or pref_value or "strong")


def _default_codex_keep_recent(*, prefer_prefs: bool = False) -> int | None:
    env_value = (os.environ.get("CODEX_SLIM_KEEP_RECENT") or "").strip()
    pref_value = _pref_get("codex_slim_keep_recent", None)
    if prefer_prefs:
        return _positive_int(pref_value) or _positive_int(env_value)
    return _positive_int(env_value) or _positive_int(pref_value)


def _default_include_compact_summaries(*, prefer_prefs: bool = False) -> bool:
    pref_value = _pref_get("codex_slim_include_compact_summaries", None)
    env_value = os.environ.get("CODEX_SLIM_INCLUDE_COMPACT_SUMMARIES")
    if prefer_prefs:
        return _bool_pref(pref_value, _bool_pref(env_value, True))
    return _bool_pref(env_value, _bool_pref(pref_value, True))


def _default_codex_dingdong_enabled() -> bool:
    return _bool_pref(
        os.environ.get("CODEX_DINGDONG_ENABLED"),
        _bool_pref(_pref_get("codex_dingdong_enabled", None), False),
    )


def _dingdong_command() -> list[str] | None:
    configured = os.environ.get("CODEX_DINGDONG_CMD")
    if configured:
        return ["bash", "-lc", configured]
    path = Path.home() / ".local" / "share" / "gccslim" / "dingdong.sh"
    if path.exists():
        return ["bash", str(path)]
    return None


def _play_dingdong() -> None:
    cmd = _dingdong_command()
    if not cmd:
        return
    try:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pass


def _iter_new_jsonl_rows(path: Path, offset: int) -> tuple[int, list[dict[str, Any]]]:
    try:
        size = path.stat().st_size
    except OSError:
        return offset, []
    if size < offset:
        offset = 0
    rows: list[dict[str, Any]] = []
    try:
        with path.open("rb") as fh:
            fh.seek(offset)
            data = fh.read()
            new_offset = fh.tell()
    except OSError:
        return offset, []
    for raw in data.splitlines():
        try:
            line = raw.decode("utf-8")
            item = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(item, dict):
            rows.append(item)
    return new_offset, rows


def _row_is_task_complete(row: dict[str, Any]) -> bool:
    if row.get("type") != "event_msg":
        return False
    payload = row.get("payload")
    return isinstance(payload, dict) and payload.get("type") == "task_complete"


def _read_marker(path: Path) -> SlimReloadMarker | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return SlimReloadMarker(**data)
    except TypeError:
        return None


def _write_marker(path: Path, marker: SlimReloadMarker) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(asdict(marker), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _read_state(path: Path) -> SlimRuntimeState | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return SlimRuntimeState(**data)
    except TypeError:
        return None


def _write_state(path: Path, state: SlimRuntimeState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(asdict(state), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _clear_marker(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _is_codex_cmdline(cmdline: list[str]) -> bool:
    if not cmdline:
        return False
    name = Path(cmdline[0]).name
    text = " ".join(cmdline)
    if "codex_slim_now.py" in text or "codex_slim_loop.py" in text:
        return False
    return name == "codex" or cmdline[0].endswith("/codex")


def _parent_chain(start_pid: int) -> list[int]:
    chain: list[int] = []
    pid = start_pid
    for _ in range(64):
        if pid <= 1 or pid in chain:
            break
        chain.append(pid)
        pid = _read_proc_stat_ppid(pid)
    return chain


def find_parent_codex_process() -> tuple[int, Path, str]:
    """Find the active parent Codex TUI and its open session JSONL."""
    for pid in _parent_chain(os.getpid()):
        cmdline = _read_proc_cmdline(pid)
        if not _is_codex_cmdline(cmdline):
            continue
        jsonl_path, deleted, session_id = _proc_codex_jsonl(pid)
        if deleted:
            raise RuntimeError(f"parent Codex PID {pid} has a deleted JSONL fd")
        if not jsonl_path or not session_id:
            raise RuntimeError(f"parent Codex PID {pid} has no open session JSONL")
        return pid, jsonl_path, session_id
    raise RuntimeError("parent Codex process not found; run this from inside Codex")


def find_current_codex_session_from_state() -> tuple[int, Path, str] | None:
    state_path_text = os.environ.get("CODEX_SLIM_STATE")
    wrapper_id = os.environ.get("CODEX_SLIM_WRAPPER_ID")
    if not state_path_text:
        return None
    state = _read_state(Path(state_path_text))
    if not state:
        return None
    if wrapper_id and state.wrapper_id != wrapper_id:
        return None
    if not state.session_id or not state.session_file:
        return None
    session_file = Path(state.session_file)
    if not session_file.exists():
        return None
    return state.codex_pid, session_file, state.session_id


def request_slim_reload(
    *,
    mode: str,
    keep_recent: int | None,
    include_compact_summaries: bool,
    marker_path: Path,
    exit_codex: bool,
    signal_name: str,
    dry_run: bool,
    require_wrapper: bool,
) -> int:
    if require_wrapper and not os.environ.get("CODEX_SLIM_WRAPPER_ID"):
        print(
            "오류: /slim은 codex-slim-loop로 시작한 Codex 안에서만 동작합니다.",
            file=sys.stderr,
        )
        print("먼저 실행: codex-slim-loop --", file=sys.stderr)
        return 2

    resolved = find_current_codex_session_from_state()
    if resolved is None:
        resolved = find_parent_codex_process()
    codex_pid, session_file, session_id = resolved
    plan = build_slim_plan(
        session_file,
        mode=mode,
        keep_recent=keep_recent,
        codex_root=CODEX_ROOT,
        include_compact_summaries=include_compact_summaries,
    )
    print(f"codex_pid: {codex_pid}", file=sys.stderr)
    print(f"source_session_id: {plan.session_id}", file=sys.stderr)
    print(f"source_session_file: {plan.session_file}", file=sys.stderr)
    print(f"mode: {plan.mode}  keep_recent: {plan.keep_recent}", file=sys.stderr)
    print(f"compact_summaries: {plan.compact_summary_count}", file=sys.stderr)
    print(
        f"size: {fmt_size(plan.original_bytes)} -> {fmt_size(plan.slim_bytes)} "
        f"(save {fmt_size(plan.saved_bytes)}, {plan.saved_percent:.1f}%)",
        file=sys.stderr,
    )
    if dry_run:
        print("--dry-run: no file changes, no restart marker", file=sys.stderr)
        return 0

    apply_slim_plan(plan)
    print(f"slimmed_session_id: {plan.session_id}", file=sys.stderr)
    print(f"backup: {plan.backup_path}", file=sys.stderr)
    marker = SlimReloadMarker(
        request_id=f"codex-slim-{uuid.uuid4().hex[:10]}",
        session_id=plan.session_id,
        session_file=str(plan.session_file),
        cwd=plan.cwd or _cwd_from_rows(_load_jsonl_rows(plan.session_file)),
        mode=plan.mode,
        keep_recent=plan.keep_recent,
        created_at=time.time(),
        source_pid=codex_pid,
        wrapper_id=os.environ.get("CODEX_SLIM_WRAPPER_ID"),
    )
    _write_marker(marker_path, marker)
    print(f"marker: {marker_path}", file=sys.stderr)

    if exit_codex and not os.environ.get("CODEX_SLIM_WRAPPER_ID"):
        sig = signal.SIGTERM if signal_name.upper() == "TERM" else signal.SIGINT
        print(f"terminating Codex PID {codex_pid} with SIG{signal_name.upper()}", file=sys.stderr)
        os.kill(codex_pid, sig)
    else:
        print("restart marker written; wrapper will terminate/restart Codex.", file=sys.stderr)
    return 0


def run_loop(
    *,
    marker_path: Path,
    codex_args: list[str],
    mode: str,
    keep_recent: int | None,
    include_compact_summaries: bool,
) -> int:
    wrapper_id = uuid.uuid4().hex
    state_path = marker_path.with_name(marker_path.name + f".state-{wrapper_id}")
    _clear_marker(marker_path)
    codex_bin = os.environ.get("CODEX_REAL_BIN") or str(DEFAULT_CODEX_BIN)
    args = [codex_bin, *codex_args]
    dingdong_enabled = _default_codex_dingdong_enabled()
    while True:
        env = _sanitize_env_for_codex(
            {
                "CODEX_SLIM_WRAPPER_ID": wrapper_id,
                "CODEX_SLIM_MARKER": str(marker_path),
                "CODEX_SLIM_STATE": str(state_path),
                "CODEX_SLIM_MODE": mode,
                "CODEX_SLIM_KEEP_RECENT": "" if keep_recent is None else str(keep_recent),
                "CODEX_SLIM_INCLUDE_COMPACT_SUMMARIES": "1" if include_compact_summaries else "0",
                "CODEX_GCCSLIM_PLAINTEXT_COMPACT": os.environ.get(
                    "CODEX_GCCSLIM_PLAINTEXT_COMPACT",
                    "1",
                ),
            }
        )
        proc = subprocess.Popen(args, env=env)
        marker: SlimReloadMarker | None = None
        code: int | None = None
        last_jsonl: Path | None = None
        last_cwd: str | None = None
        jsonl_offsets: dict[Path, int] = {}
        while True:
            jsonl_path, _deleted, session_id = _proc_codex_jsonl(proc.pid)
            if jsonl_path and jsonl_path.exists() and jsonl_path != last_jsonl:
                try:
                    last_cwd = _cwd_from_rows(_load_jsonl_rows(jsonl_path))
                    last_jsonl = jsonl_path
                    jsonl_offsets.setdefault(jsonl_path, jsonl_path.stat().st_size)
                except Exception:
                    last_cwd = None
            if dingdong_enabled and jsonl_path and jsonl_path.exists():
                offset = jsonl_offsets.get(jsonl_path)
                if offset is None:
                    offset = jsonl_path.stat().st_size
                    jsonl_offsets[jsonl_path] = offset
                new_offset, new_rows = _iter_new_jsonl_rows(jsonl_path, offset)
                jsonl_offsets[jsonl_path] = new_offset
                if any(_row_is_task_complete(row) for row in new_rows):
                    _play_dingdong()
            _write_state(
                state_path,
                SlimRuntimeState(
                    wrapper_id=wrapper_id,
                    codex_pid=proc.pid,
                    session_id=session_id,
                    session_file=str(jsonl_path) if jsonl_path else None,
                    cwd=last_cwd,
                    updated_at=time.time(),
                ),
            )

            marker = _read_marker(marker_path)
            if marker and marker.wrapper_id in {None, wrapper_id}:
                print("\n[gccfork-codex-slim] slim marker detected; stopping current Codex...\n", file=sys.stderr)
                proc.terminate()
                try:
                    code = proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    code = proc.wait()
                break

            code = proc.poll()
            if code is not None:
                break
            time.sleep(0.25)

        try:
            state_path.unlink()
        except FileNotFoundError:
            pass
        marker = _read_marker(marker_path)
        if not marker or marker.wrapper_id not in {None, wrapper_id}:
            return int(code or 0)

        _clear_marker(marker_path)
        cwd = marker.cwd if marker.cwd and Path(marker.cwd).exists() else None
        print(
            f"\n[gccfork-codex-slim] restarting same terminal: codex resume {marker.session_id}\n",
            file=sys.stderr,
            flush=True,
        )
        args = [codex_bin, "resume", marker.session_id]
        if cwd:
            try:
                os.chdir(cwd)
            except OSError:
                pass


def parse_loop_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Codex in a same-terminal slim/restart loop.")
    parser.add_argument("--marker", type=Path, default=DEFAULT_MARKER)
    parser.add_argument("--mode", default=_default_codex_slim_mode())
    parser.add_argument("--keep-recent", type=int, default=_default_codex_keep_recent())
    parser.add_argument(
        "--include-compact-summaries",
        action=argparse.BooleanOptionalAction,
        default=_default_include_compact_summaries(),
    )
    parser.add_argument("codex_args", nargs=argparse.REMAINDER, help="Arguments passed to codex after --")
    args = parser.parse_args(argv)
    if args.codex_args and args.codex_args[0] == "--":
        args.codex_args = args.codex_args[1:]
    return args


def parse_now_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Slim the current parent Codex session and request same-terminal restart.")
    parser.add_argument("--mode", default=_default_codex_slim_mode(prefer_prefs=True))
    parser.add_argument("--keep-recent", type=int, default=_default_codex_keep_recent(prefer_prefs=True))
    parser.add_argument(
        "--include-compact-summaries",
        action=argparse.BooleanOptionalAction,
        default=_default_include_compact_summaries(prefer_prefs=True),
    )
    parser.add_argument("--marker", type=Path, default=Path(os.environ.get("CODEX_SLIM_MARKER", DEFAULT_MARKER)))
    parser.add_argument("--no-exit", action="store_true", help="Do not terminate the current Codex process")
    parser.add_argument("--signal", choices=["TERM", "INT"], default="TERM")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--allow-no-wrapper",
        action="store_true",
        help="Allow slim marker creation outside codex-slim-loop. Mostly for debugging.",
    )
    return parser.parse_args(argv)


def main_loop(argv: list[str] | None = None) -> int:
    args = parse_loop_args(argv)
    return run_loop(
        marker_path=args.marker,
        codex_args=args.codex_args,
        mode=args.mode,
        keep_recent=args.keep_recent,
        include_compact_summaries=args.include_compact_summaries,
    )


def main_now(argv: list[str] | None = None) -> int:
    args = parse_now_args(argv)
    return request_slim_reload(
        mode=args.mode,
        keep_recent=args.keep_recent,
        include_compact_summaries=args.include_compact_summaries,
        marker_path=args.marker,
        exit_codex=not args.no_exit,
        signal_name=args.signal,
        dry_run=args.dry_run,
        require_wrapper=not args.allow_no_wrapper,
    )
