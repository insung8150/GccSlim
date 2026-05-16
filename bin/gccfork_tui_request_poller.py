"""TUI 가 외부 (claude hook) 의 작업 요청을 polling 으로 받는 사이드카.

포크_무조건확인.md 정책 구현:

  claude hook 은 gccfork CLI 를 직접 spawn 하지 않는다 (self-injection race).
  대신 ~/.claude/gccfork-tui-requests/<uuid>.json 으로 작업 요청만 publish 하고 종료.
  TUI 가 떠 있으면 polling 으로 그 파일을 발견 → subprocess 로 작업 실행.
  TUI 자체는 외부 process 이므로 spawn 해도 self-injection 발생 안 함.

요청 파일 schema:
    {
        "version": 1,
        "ts": <epoch seconds>,
        "action": "slim-and-reload" | "slim" | "slim-dry",
        "sid": "<full session id>",
        "mode": "strong" | "medium" | "weak",
        "source": "<누가 보냈는지>"  # 로그/디버그용
    }

처리 흐름:
  1. _tui_request_poll_tick (1초마다)
  2. TUI_REQUEST_DIR 의 *.json scan, mtime 5분 이내만 처리 (stale 차단)
  3. 파일 lock (rename → .processing) 으로 다른 TUI 인스턴스와 race 방지
  4. action dispatch → subprocess.Popen 으로 gccfork CLI 호출 (textual @work)
  5. 처리 완료 → 파일 unlink, 사용자 notify
  6. 실패 → .failed 로 rename + notify

CLAUDE 메모리: feedback_module_separation.md (사이드카 + Mixin).
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
STALE_AFTER_SEC = 300  # 5분 지난 요청은 무시 (TUI 가 죽었다 살아난 경우 옛 요청 처리 방지)
POLL_INTERVAL_SEC = 1.0


class TuiRequestPollerMixin:
    """GCCForkApp 에 mixin 으로 추가. on_mount 에서 _tui_request_poller_start() 호출.

    아래 set_interval / run_worker / notify attribute 는 textual.App 이 제공 —
    mixin 정적 검사 통과를 위해 type hint 만 선언.
    """

    set_interval: Any
    run_worker: Any
    notify: Any

    _tui_request_poller_timer = None
    _tui_request_processing: set[str] = set()  # 처리 중인 file stem (중복 dispatch 방지)

    def _tui_request_poller_start(self) -> None:
        """on_mount 에서 호출. polling timer 시작."""
        try:
            TUI_REQUEST_DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            return  # 디렉토리 못 만들면 polling 비활성 (조용히)
        if not hasattr(self, "_tui_request_processing") or self._tui_request_processing is None:
            self._tui_request_processing = set()
        self._tui_request_poller_timer = self.set_interval(
            POLL_INTERVAL_SEC, self._tui_request_poll_tick
        )

    def _tui_request_poll_tick(self) -> None:
        """1초마다 호출. TUI_REQUEST_DIR scan 후 새 요청 발견 시 dispatch.

        부수 효과: hook 이 안 읽고 죽은 stale .result 파일 5분 후 삭제.
        """
        now = time.time()
        # stale .result cleanup (hook 폴링이 못 읽고 끝난 경우)
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
            # .result / .tmp / .processing / .failed / .stale 등은 skip
            if f.suffix != ".json":
                continue
            stem = f.stem
            if stem in self._tui_request_processing:
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            # stale 차단 — 5분 지난 요청은 무시
            if now - st.st_mtime > STALE_AFTER_SEC:
                self._tui_request_archive(f, suffix=".stale")
                continue
            self._tui_request_processing.add(stem)
            self._tui_request_dispatch(f)

    def _tui_request_dispatch(self, request_path: Path) -> None:
        """request 파일 1개 처리. 파싱 → action handler 호출."""
        try:
            data = json.loads(request_path.read_text())
        except Exception as exc:
            self._tui_request_archive(request_path, suffix=".failed")
            self._tui_request_notify(f"❌ TUI request 파싱 실패: {exc}")
            return

        action = data.get("action", "")
        sid = data.get("sid", "")
        mode = data.get("mode", "")          # "" 또는 누락 = prefs 기본값 사용
        source = data.get("source", "?")
        # cwd from /slim hook — used to read project-local prefs override
        # (<cwd>/.gccfork/ccfork-prefs.json). Falls back to TUI's own cwd.
        request_cwd = data.get("cwd")

        if not sid or len(sid) < 8:
            self._tui_request_archive(request_path, suffix=".failed")
            self._tui_request_notify(f"❌ TUI request: sid 없음/짧음 ({sid!r})")
            return

        # `/slim` 과 `/slim:dry` 둘 다 mode 없이 옴 → gccfork prefs 에서 결정.
        # 모든 슬림 결정 로직은 여기 한 곳에. claude 는 메신저일 뿐.
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
            # slim-dry 는 그대로 유지 (아래 dispatch 에서 처리)
            keep_turns_arg = ["--keep-recent-turns", str(chosen_turns)]
            # Restore the TUI's own active project cwd
            if set_active_project_cwd and saved_cwd is not None:
                try:
                    set_active_project_cwd(saved_cwd)
                except Exception:
                    pass
        else:
            keep_turns_arg = []

        # action 별 명령 빌드 — 모두 gccfork CLI 직접 호출 (slim-now 래퍼는 turn 옵션 미지원)
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
            f"🔔 TUI request 수신: {action} (sid={sid[:8]}, mode={mode}, src={source})"
        )
        # textual worker 로 subprocess 실행 (UI block 없음)
        self._tui_request_run_command(request_path, argv, action, sid[:8])

    def _tui_request_run_command(
        self, request_path: Path, argv: list[str], action: str, sid_short: str
    ) -> None:
        """textual worker (thread) 에서 subprocess 실행 후 결과 처리.

        worker 데코레이터를 동적으로 import — textual.work 의 import 위치는 버전마다
        조금씩 달라 try/except 로 fallback.
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
                # stderr trace 라인 제거 (claude hook 노이즈 방지)
                merged = (proc.stdout or "") + (proc.stderr or "")
                stdout_text = "\n".join(
                    ln for ln in merged.splitlines() if not ln.lstrip().startswith("[sar")
                )
                # 디버그용 풀로그도 /tmp 에 보존
                try:
                    log_path.write_text(merged)
                except OSError:
                    pass
            except subprocess.TimeoutExpired:
                err_text = "타임아웃 (120s)"
            except Exception as exc:
                err_text = f"예외: {exc}"

            # result write back — atomic rename. claude hook 이 폴링으로 읽음.
            self._tui_request_write_result(
                result_path,
                ok=(rc == 0 and not err_text),
                action=action,
                rc=rc,
                stdout=stdout_text,
                error=err_text,
                sid_short=sid_short,
            )

            # 정상 종료 후엔 request 도 archive (성공=unlink, 실패=.failed)
            if rc == 0 and not err_text:
                self._tui_request_archive(request_path, suffix=None)
                self._tui_request_notify(f"✅ TUI request 완료: {action} sid={sid_short}")
            else:
                self._tui_request_archive(request_path, suffix=".failed")
                tag = err_text or f"rc={rc}"
                self._tui_request_notify(f"❌ TUI request 실패: {action} sid={sid_short} {tag}")
            self._tui_request_processing.discard(request_path.stem)

        if work is not None:
            # textual worker 로 격리. UI block 없음.
            self.run_worker(_run, thread=True, exclusive=False)
        else:
            # fallback — 같은 thread (UI 잠깐 멈춤)
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
        """결과를 atomic 하게 write — claude hook 이 폴링으로 읽음.

        스키마: { ok, action, rc, stdout, error, sid, ts }
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
        """처리 끝난 파일 정리. suffix=None 이면 unlink, 있으면 rename.

        .failed / .stale 은 디버그용으로 남기고, 성공은 깔끔하게 삭제.
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
        """TUI 화면 우상단에 notify. notify() 가 없으면 조용히 패스."""
        try:
            self.notify(msg, timeout=4.0)
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────────────────
# Helper — claude hook 쪽에서 쓸 수 있는 publish 함수
# (intercept.sh 가 직접 jq + python3 로 처리하지만, 향후 hook 을 python 으로
#  바꿀 때 사용)
# ──────────────────────────────────────────────────────────────────────────
def publish_tui_request(action: str, sid: str, mode: str = "strong", source: str = "claude-hook") -> Path:
    """TUI request 파일 publish. atomic rename 으로 race 회피."""
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
