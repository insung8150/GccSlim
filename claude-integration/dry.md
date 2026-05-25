---
description: 🔍 Slim dry-run — statistics only, no changes (uses GccSlim settings)
allowed-tools: []
---

`/slim:dry` is a **dry-run simulation** delegation signal to the GccSlim TUI.

It has no inline options. Mode and protected recent turns follow **GccSlim settings** (same settings as `/slim`, so the preview stays consistent). It makes **no JSONL changes**.

Flow:
1. The UserPromptSubmit hook publishes an `action=slim-dry` payload to `~/.claude/gccfork-tui-requests/<id>.json`.
2. The GccSlim TUI `TuiRequestPollerMixin` reads prefs and runs `gccslim slim-inplace --mode <m> --keep-recent-turns <t> --dry-run`.
3. The TUI writes the result to `<id>.json.result`.
4. The hook polls that result and displays KEEP/STUB/DROP/REBIND statistics plus size changes as the block reason.

Claude itself contains no slim logic; mode selection, protected turn policy, command execution, and output formatting are all handled by GccSlim.

Use `/slim` to apply changes (same settings; reload behavior also follows prefs).
