# GccSlim

Current release: `v2026.05.26.1`

If you use Claude Code or Codex CLI, have you ever:

- lost track of a conversation because the session is just a random ID — and wished you could **give it a real name**?
- wanted to **duplicate a session as-is** to try another direction while keeping the original?
- wanted to take a session you started in Claude Code and **hand it straight to Codex** to keep going?
- wished you could see every session scattered across projects **in one place** and tidy them up?

**That's what GccSlim is for.** Manage all your Claude Code and Codex sessions from one terminal UI — name them, duplicate them, move them, merge/split them, and pass them between Claude Code and Codex.

> 💡 Keep GccSlim open as your session manager in your main terminal, and open any session you pick straight into your VS Code terminal. Browse and pick on one side, work the session on the other.

_(And when a session gets too big, you can "slim" it down too — trim the old, heavy parts while keeping your recent work, so it's fast again without starting over.)_

![GccSlim](assets/screenshot.png)

<sub>Every feature on one screen:</sub>

![GccSlim feature guide](assets/gccslim-guide-en-white.png)

## Choose your language

GccSlim ships separate **Korean** and **English** distributions. Pick the asset matching your language and platform from the [latest release](https://github.com/insung8150/GccSlim/releases/latest):

| Language | Platform | Asset |
|---|---|---|
| English | Linux x86_64 | `gccslim-en-linux-x86_64-<version>.tar.gz` |
| English | macOS arm64 | `gccslim-en-macos-arm64-<version>.tar.gz` |
| Korean  | Linux x86_64 | `gccslim-ko-linux-x86_64-<version>.tar.gz` |
| Korean  | macOS arm64 | `gccslim-ko-macos-arm64-<version>.tar.gz` |

```bash
# English on Linux
tar xzf gccslim-en-linux-x86_64-*.tar.gz
cd gccslim-en-linux-x86_64-*
bash install.sh
gccslim    # English UI

# Korean on Linux
tar xzf gccslim-ko-linux-x86_64-*.tar.gz
cd gccslim-ko-linux-x86_64-*
bash install.sh
gccslim    # Korean UI
```

**Switching languages later**

Both language tarballs install into the same `~/.local/bin/gccslim`. To switch, download the other language tarball and run `install.sh` again — it overwrites the binary and updates `~/.local/share/gccslim/default-language` accordingly.

The settings panel has a `Language` radio that refreshes a few labels in place, but a complete UI switch still requires installing the matching tarball.

한국어 안내는 [README.ko.md](README.ko.md) 참고.

## Run

Without installing:

```bash
./bin/gccslim
```

Install into `~/.local/bin`:

```bash
./install.sh
gccslim
```

Direct slim command:

```bash
gccslim-now
```

Codex helper commands installed by this release:

```bash
codex-slim-loop
codex-slim-now
```

## How it works with Claude Code

GccSlim sticks to Claude Code's official extension points, and anything it changes is optional and reversible:

- The `/slim` command works through Claude Code's official hook — installing it just adds one line to `~/.claude/settings.json`, and you can remove it anytime.
- Slimming only edits your own session files. It never changes Claude Code itself, and the originals go to a trash you can restore from.
- Optionally, it can refresh a running session right after slimming, using Claude's own resume command. This is opt-in and reversible; plain slimming works without it.
- Everything runs locally. It doesn't touch your login, usage limits, or billing, and nothing is uploaded.

## Included

- `bin/gccslim`: public TUI entrypoint.
- `bin/gccslim-now`: direct slim wrapper for the active Claude session.
- `bin/gccslim-slim`: platform-selecting slim wrapper.
- `bin/gccslim-claude-patch`: platform-selecting Claude patch wrapper.
- `bin/codex-slim-loop`: Codex same-terminal slim/restart wrapper.
- `bin/codex-slim-now`: Codex active-session slim request helper.
- `bin/gccfork_codex_slim_loop.py`: Codex wrapper loop sidecar.
- `bin/gccfork_codex_slim_reload.py`: Codex JSONL slim plan/apply sidecar.
- `bin/linux-x86_64/`: stripped Linux x86_64 Rust binaries.
- `bin/macos-arm64/`: stripped macOS arm64 Rust binaries.
- `bin/gccfork_*.py`: Python sidecar modules kept under compatibility names.
- `share/gccslim/brain-system-prompt.md`: sanitized runtime prompt.

Compatibility wrappers named `gccfork-slim` and `gccfork-claude-patch` are included because some internal dispatch paths still call those legacy names.

## About this distribution

This repo is a **binary distribution**, not the internal development source tree. The Rust core ships as stripped binaries; the Python layer, install scripts, and integration are included as source. Deliberately excluded:

- Rust source code.
- Rust tests or internal regression fixtures.
- Private session logs.
- Local `.claude`, `.codex`, `.gccfork` state.
- Internal Korean work logs and migration notes.
- Machine-specific SSH, hostname, IP, and absolute-path notes.

## License

MIT License. See `LICENSE`.
