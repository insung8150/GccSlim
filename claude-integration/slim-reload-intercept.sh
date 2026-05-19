#!/usr/bin/env bash
# UserPromptSubmit hook — intercept /slim, /slim:dry.
# Only operates while the GccSlim TUI is running (see TUI-only policy).

set -uo pipefail

input=$(cat)
# Parse JSON via python3 (no jq dependency — macOS default doesn't ship jq).
prompt=$(printf '%s' "$input" | python3 -c 'import json, sys; d=json.load(sys.stdin); print(d.get("prompt",""))' 2>/dev/null)

case "$prompt" in
  "/slim")     action=slim-default ;;
  "/slim:dry") action=slim-dry ;;
  *) exit 0 ;;
esac

emit_block() {
  REASON="$1" python3 -c '
import json, os
print(json.dumps({
    "decision": "block",
    "suppressOutput": True,
    "reason": os.environ["REASON"],
}))
'
}

# PPID chain → claude PID → sessions/<PID>.json sessionId
# Cross-platform: strip leading/trailing whitespace from ps output
# (macOS BSD ps adds spaces; Linux usually doesn't).
sid=""
walk_pid=$$
while [ "$walk_pid" != "1" ] && [ -n "$walk_pid" ]; do
  comm=$(ps -p "$walk_pid" -o comm= 2>/dev/null | tr -d ' \t\n')
  comm_base="${comm##*/}"  # strip any path prefix
  if [ "$comm" = "claude" ] || [ "$comm_base" = "claude" ]; then
    sid=$(HOME="$HOME" PID="$walk_pid" python3 -c '
import json, os
try:
    d = json.load(open(f"{os.environ[\"HOME\"]}/.claude/sessions/{os.environ[\"PID\"]}.json"))
    print(d.get("sessionId", ""))
except Exception:
    pass
' 2>/dev/null)
    break
  fi
  walk_pid=$(ps -o ppid= -p "$walk_pid" 2>/dev/null | tr -d ' \t\n')
done

# Fallbacks when PPID walk fails (hook spawned outside normal claude tree, etc.)
if [ -z "$sid" ]; then
  sid="${CLAUDE_CODE_SESSION_ID:-${CLAUDE_SESSION_ID:-${GCCSLIM_PARENT_SESSION_ID:-${GCCFORK_PARENT_SESSION_ID:-}}}}"
fi
# Last resort: most-recent jsonl in the current project's session dir
if [ -z "$sid" ] && [ -n "${CLAUDE_PROJECT_DIR:-}" ]; then
  slug=$(printf '%s' "$CLAUDE_PROJECT_DIR" | sed 's|/|-|g')
  proj_dir="$HOME/.claude/projects/$slug"
  if [ -d "$proj_dir" ]; then
    # `ls -t` sorts by mtime newest-first; portable across Linux/macOS.
    recent=$(ls -t "$proj_dir"/*.jsonl 2>/dev/null | head -1)
    [ -n "$recent" ] && sid=$(basename "$recent" .jsonl)
  fi
fi

if [ -z "$sid" ]; then
  emit_block "❌ sid 추출 실패. hook 내부 오류."
  exit 0
fi
sid_short="${sid:0:8}"

# Detect the main TUI process (subcommands share the same binary).
# Cross-platform: use `ps -p <pid> -o args=` (no /proc dependency, no path
# hardcoding — works on both Linux and macOS).
GCCSLIM_BIN="${GCCSLIM_BIN:-$HOME/.local/bin/gccslim}"
TUI_PID=""
for pid in $(pgrep -f "$GCCSLIM_BIN" 2>/dev/null); do
    cmdline=$(ps -p "$pid" -o args= 2>/dev/null)
    # Must invoke the gccslim binary directly (no subcommand suffix) — that
    # marks the interactive TUI process.
    if echo "$cmdline" | grep -qE "(^| )${GCCSLIM_BIN}( |$)" \
       && ! echo "$cmdline" | grep -qE "(slim-and-reload|slim-inplace|hot-reload|search|list|detail|ancestry|parent-of|rename|hard-fork|delete|prefs|stats|patch-claude|reconcile-live|live-sessions|--help|-h)"; then
        TUI_PID="$pid"
        break
    fi
done

if [ -z "$TUI_PID" ]; then
    emit_block "❌ GccSlim TUI 가 떠 있지 않습니다.

먼저 외부 터미널에서 실행:

  \$ gccslim

이 도구는 TUI 가 떠 있는 상태에서만 작동합니다."
    exit 0
fi

# IPC request directory — preserved as gccfork-* for Python sidecar compatibility.
TUI_REQ_DIR="$HOME/.claude/gccfork-tui-requests"
mkdir -p "$TUI_REQ_DIR" 2>/dev/null

REQ_ID="req-$(uuidgen 2>/dev/null | tr -d '-' | head -c 12 || date +%s%N)"
REQ_FILE="$TUI_REQ_DIR/$REQ_ID.json"
TMP_FILE="$REQ_FILE.tmp"

ACTION="$action" SID="$sid" PROMPT="$prompt" REQ_CWD="$PWD" python3 -c '
import json, os, time
payload = {
    "version": 1,
    "ts": int(time.time()),
    "action": os.environ["ACTION"],
    "sid": os.environ["SID"],
    "mode": "",
    # cwd of the calling claude process — used by TUI poller to read
    # project-local prefs override (<cwd>/.gccfork/ccfork-prefs.json).
    "cwd": os.environ.get("REQ_CWD", ""),
    "source": "claude-hook ({})".format(os.environ["PROMPT"]),
}
print(json.dumps(payload, ensure_ascii=False, indent=2))
' > "$TMP_FILE" 2>/dev/null

if [ ! -s "$TMP_FILE" ]; then
    rm -f "$TMP_FILE"
    emit_block "❌ TUI request publish 실패."
    exit 0
fi
mv "$TMP_FILE" "$REQ_FILE"

if [ "$action" = "slim-dry" ]; then
    emit_block "🔍 Slim analyzed (dry)"
else
    emit_block "🔻 Slim processed"
fi
exit 0
