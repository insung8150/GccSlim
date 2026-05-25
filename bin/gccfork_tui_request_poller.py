"""Sidecar that lets the TUI poll work requests from external Claude hooks.

Implements the forced-confirmation policy:

  The Claude hook does not spawn the gccfork CLI directly, avoiding the
  self-injection race. Instead, it publishes a work request to
  ~/.claude/gccfork-tui-requests/<uuid>.json and exits.
  If the TUI is running, it discovers the file by polling and runs the work in
  a subprocess. Because the TUI is an external process, spawning from it does
  not cause self-injection.

Request file schema:
    {
        "version": 1,
        "ts": <epoch seconds>,
        "action": "slim-and-reload" | "slim" | "slim-dry",
        "sid": "<full session id>",
        "mode": "strong" | "medium" | "weak",
        "source": "<sender>"  # for logging/debugging
    }

Processing flow:
  1. _tui_request_poll_tick runs every second.
  2. Scan *.json in TUI_REQUEST_DIR and process only files whose mtime is
     within five minutes, blocking stale requests.
  3. Use a file lock (rename to .processing) to avoid races with other TUI
     instances.
  4. Dispatch the action and call the gccfork CLI through subprocess.Popen
     (Textual @work).
  5. On success, unlink the file and notify the user.
  6. On failure, rename to .failed and notify.

Claude memory reference: feedback_module_separation.md (sidecar + mixin).
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Optional


TUI_REQUEST_DIR = Path.home() / ".claude" / "gccfork-tui-requests"
# Cross-platform: resolve to $HOME at import time, no user-path hardcoding.
GCCFORK_BIN = str(Path.home() / ".local" / "bin" / "gccfork")
STALE_AFTER_SEC = 300  # ignore requests older than five minutes to avoid processing old work after TUI restart
POLL_INTERVAL_SEC = 1.0


class TuiRequestPollerMixin:
    """Mixin added to GCCForkApp. Call _tui_request_poller_start() from on_mount.

    The set_interval / run_worker / notify attributes are provided by textual.App; they are declared only for static checks on this mixin.
    """

    set_interval: Any
    run_worker: Any
    notify: Any

    _tui_request_poller_timer = None
    _tui_request_processing: set[str] = set()  # file stems currently being processed, to avoid duplicate dispatch

    def _tui_request_poller_start(self) -> None:
        """Called from on_mount to start the polling timer."""
        try:
            TUI_REQUEST_DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            return  # disable polling quietly if the directory cannot be created
        if not hasattr(self, "_tui_request_processing") or self._tui_request_processing is None:
            self._tui_request_processing = set()
        self._tui_request_poller_timer = self.set_interval(
            POLL_INTERVAL_SEC, self._tui_request_poll_tick
        )

    def _tui_request_poll_tick(self) -> None:
        """Called every second. Scan TUI_REQUEST_DIR and dispatch new requests.

        Side effect: delete stale .result files after five minutes when the hook died before reading them.
        """
        now = time.time()
        # stale .result cleanup when hook polling ended before reading it
        try:
            for r in TUI_REQUEST_DIR.glob("*.json.result"):
                try:
                    if now - r.stat().st_mtime > STALE_AFTER_SEC:
                        r.unlink()
                except OSError:
                    pass
        except OSError:
            pass
        try:
            files = sorted(TUI_REQUEST_DIR.glob("*.json"))
        except OSError:
            return
        for f in files:
            # skip .result / .tmp / .processing / .failed / .stale files
            if f.suffix != ".json":
                continue
            stem = f.stem
            if stem in self._tui_request_processing:
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            # stale guard: ignore requests older than five minutes
            if now - st.st_mtime > STALE_AFTER_SEC:
                self._tui_request_archive(f, suffix=".stale")
                continue
            self._tui_request_processing.add(stem)
            self._tui_request_dispatch(f)

    def _tui_request_dispatch(self, request_path: Path) -> None:
        """Process one request file: parse it and call the action handler."""
        try:
            data = json.loads(request_path.read_text())
        except Exception as exc:
            self._tui_request_archive(request_path, suffix=".failed")
            self._tui_request_notify(f"❌ TUI request parse failed: {exc}")
            return

        action = data.get("action", "")
        sid = data.get("sid", "")
        mode = data.get("mode", "")          # empty or missing means use prefs default
        source = data.get("source", "?")
        # cwd from /slim hook — used to read project-local prefs override
        # (<cwd>/.gccfork/ccfork-prefs.json). Falls back to TUI's own cwd.
        request_cwd = data.get("cwd")

        if not sid or len(sid) < 8:
            self._tui_request_archive(request_path, suffix=".failed")
            self._tui_request_notify(f"❌ TUI request: missing/short sid ({sid!r})")
            return

        # Both `/slim` and `/slim:dry` can arrive without mode, so gccfork prefs decide it.
        # All slim decision logic lives here; Claude is only the messenger.
        chosen_turns = 5
        chosen_reload = True
        if action in ("slim-default", "slim-dry") or (action == "slim" and not mode):
            try:
                from gccfork_sessions import (
                    pref_get,
                    set_active_project_cwd,
                    get_active_project_cwd,
                )
                from gccfork_settings import SLIM_DEFAULT_PREFS
            except ImportError:
                pref_get = None  # type: ignore
                set_active_project_cwd = None  # type: ignore
                get_active_project_cwd = None  # type: ignore
                SLIM_DEFAULT_PREFS = {                      # type: ignore[assignment]
                    "slim_default_mode": "strong",
                    "slim_default_reload": True,
                    "slim_strong_keep_recent_turns": 5,
                    "slim_medium_keep_recent_turns": 10,
                    "slim_weak_keep_recent_turns": 30,
                }
            # Temporarily switch active project cwd to the requester's cwd so
            # pref_get() reads <request_cwd>/.gccfork/ccfork-prefs.json first.
            # Restore TUI's own cwd after reading.
            saved_cwd = get_active_project_cwd() if get_active_project_cwd else None
            if request_cwd and set_active_project_cwd:
                try:
                    set_active_project_cwd(request_cwd)
                except Exception:
                    pass
            def _pf(k):
                return pref_get(k, SLIM_DEFAULT_PREFS.get(k)) if pref_get else SLIM_DEFAULT_PREFS.get(k)
            chosen_mode = str(_pf("slim_default_mode") or "strong")
            if chosen_mode not in ("strong", "medium", "weak"):
                chosen_mode = "strong"
            turns_key = f"slim_{chosen_mode}_keep_recent_turns"
            chosen_turns = int(_pf(turns_key) or SLIM_DEFAULT_PREFS.get(turns_key, 5))
            chosen_reload = bool(_pf("slim_default_reload"))
            mode = chosen_mode
            if action == "slim-default":
                action = "slim-and-reload" if chosen_reload else "slim"
            # keep slim-dry unchanged; dispatch below handles it
            keep_turns_arg = ["--keep-recent-turns", str(chosen_turns)]
            # Restore the TUI's own active project cwd
            if set_active_project_cwd and saved_cwd is not None:
                try:
                    set_active_project_cwd(saved_cwd)
                except Exception:
                    pass
        else:
            keep_turns_arg = []

        # Build command per action. All commands call the gccfork CLI directly because the slim-now wrapper does not support turn options.
        argv: Optional[list[str]] = None
        if action == "slim-and-reload":
            argv = [
                GCCFORK_BIN, "slim-and-reload",
                "--sid", sid, "--no-preflight",
                "--mode", mode, "--initial-delay", "0",
            ] + keep_turns_arg
        elif action == "slim":
            argv = [GCCFORK_BIN, "slim-inplace", sid, "--mode", mode] + keep_turns_arg
        elif action == "slim-dry":
            argv = [GCCFORK_BIN, "slim-inplace", sid, "--mode", mode, "--dry-run"] + keep_turns_arg
        else:
            self._tui_request_archive(request_path, suffix=".failed")
            self._tui_request_notify(f"❌ TUI request: unknown action {action!r}")
            return

        self._tui_request_notify(
            f"🔔 TUI request received: {action} (sid={sid[:8]}, mode={mode}, src={source})"
        )
        # run subprocess in a Textual worker so the UI does not block
        self._tui_request_run_command(request_path, argv, action, sid[:8])

    def _tui_request_run_command(
        self, request_path: Path, argv: list[str], action: str, sid_short: str
    ) -> None:
        """Run subprocess in a Textual worker thread and process the result.

        Import the worker decorator dynamically. The textual.work import location varies by version, so use a try/except fallback.
        """
        try:
            from textual import work
        except ImportError:
            work = None  # type: ignore

        def _run() -> None:
            log_path = Path(f"/tmp/gccfork-tui-request-{int(time.time())}-{sid_short}.log")
            result_path = request_path.with_suffix(request_path.suffix + ".result")
            rc = -1
            stdout_text = ""
            err_text = ""
            try:
                proc = subprocess.run(
                    argv,
                    capture_output=True,
                    stdin=subprocess.DEVNULL,
                    timeout=120,
                    text=True,
                )
                rc = proc.returncode
                # remove stderr trace lines to avoid Claude hook noise
                merged = (proc.stdout or "") + (proc.stderr or "")
                stdout_text = "\n".join(
                    ln for ln in merged.splitlines() if not ln.lstrip().startswith("[sar")
                )
                # also keep a full debug log under /tmp
                try:
                    log_path.write_text(merged)
                except OSError:
                    pass
            except subprocess.TimeoutExpired:
                err_text = "timeout (120s)"
            except Exception as exc:
                err_text = f"exception: {exc}"

            # write result back with atomic rename; the Claude hook reads it by polling
            self._tui_request_write_result(
                result_path,
                ok=(rc == 0 and not err_text),
                action=action,
                rc=rc,
                stdout=stdout_text,
                error=err_text,
                sid_short=sid_short,
            )

            # after completion, archive the request too: unlink on success, .failed on failure
            if rc == 0 and not err_text:
                self._tui_request_archive(request_path, suffix=None)
                self._tui_request_notify(f"✅ TUI request complete: {action} sid={sid_short}")
            else:
                self._tui_request_archive(request_path, suffix=".failed")
                tag = err_text or f"rc={rc}"
                self._tui_request_notify(f"❌ TUI request failed: {action} sid={sid_short} {tag}")
            self._tui_request_processing.discard(request_path.stem)

        if work is not None:
            # isolate in a Textual worker; no UI block
            self.run_worker(_run, thread=True, exclusive=False)
        else:
            # fallback: same thread, briefly blocks UI
            _run()

    def _tui_request_write_result(
        self,
        result_path: Path,
        *,
        ok: bool,
        action: str,
        rc: int,
        stdout: str,
        error: str,
        sid_short: str,
    ) -> None:
        """Write results atomically; the Claude hook reads them by polling.

        Schema: { ok, action, rc, stdout, error, sid, ts }
        """
        payload = {
            "ok": ok,
            "action": action,
            "rc": rc,
            "stdout": stdout,
            "error": error,
            "sid": sid_short,
            "ts": int(time.time()),
        }
        tmp = result_path.with_suffix(result_path.suffix + ".tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
            os.rename(tmp, result_path)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass

    def _tui_request_archive(self, request_path: Path, suffix: Optional[str]) -> None:
        """Clean up a processed file. suffix=None unlinks it; otherwise it is renamed.

        .failed and .stale remain for debugging; successful files are deleted cleanly.
        """
        try:
            if suffix is None:
                request_path.unlink()
            else:
                target = request_path.with_suffix(request_path.suffix + suffix)
                request_path.rename(target)
        except OSError:
            pass

    def _tui_request_notify(self, msg: str) -> None:
        """Notify at the top-right of the TUI. If notify() is unavailable, pass quietly."""
        try:
            self.notify(msg, timeout=4.0)
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────────────────
# Helper publish function for Claude hooks
# (intercept.sh currently uses jq + python3 directly, but this is available if the hook moves to Python later)
# ──────────────────────────────────────────────────────────────────────────
def publish_tui_request(action: str, sid: str, mode: str = "strong", source: str = "claude-hook") -> Path:
    """Publish a TUI request file. Atomic rename avoids races."""
    TUI_REQUEST_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "ts": int(time.time()),
        "action": action,
        "sid": sid,
        "mode": mode,
        "source": source,
    }
    req_id = uuid.uuid4().hex[:12]
    out = TUI_REQUEST_DIR / f"req-{req_id}.json"
    tmp = TUI_REQUEST_DIR / f"req-{req_id}.json.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    os.rename(tmp, out)
    return out
