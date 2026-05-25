---
description: 🔻 Slim — automatic by GccSlim settings (mode/turns/reload)
allowed-tools: []
---

`/slim` is a delegation signal to the GccSlim TUI.

It has no inline options. Mode, protected recent turns, and hot-reload behavior all follow **GccSlim settings** (GccSlim TUI -> Settings -> Slim -> `/slim` defaults).

Flow:
1. The UserPromptSubmit hook (`slim-reload-intercept.sh`) intercepts the command and publishes an `action=slim-default` payload under `~/.claude/gccfork-tui-requests/`.
2. The GccSlim TUI `TuiRequestPollerMixin` polls once per second, reads prefs, then dispatches either `slim-and-reload` (`reload=true`) or `slim-inplace` (`reload=false`).
3. If the GccSlim TUI is not running, the hook only shows guidance and stops.

Claude itself does not print extra text; the hook block response is shown to the user.

**No variant commands are supported anymore** (`/slim:medium`, `/slim:reload`, `/slim:weak-reload`, and `/slim:medium-reload` were removed). Change GccSlim settings, then run `/slim`.

Only dry-run remains separate: `/slim:dry` (no changes, statistics only).
