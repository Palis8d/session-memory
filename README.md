# Session Memory

One searchable memory of your Claude Code and Codex sessions, kept on your own computer.

Ask your assistant "what did we decide about the login page last week?" or "which session changed `config.toml`?" and it can look it up, across every Claude Code session and Codex thread on this computer.

## Install

Paste this into Claude Code or Codex:

> Install Session Memory for me by following https://github.com/Palis8d/session-memory/blob/main/AGENT_INSTALL.md

Your assistant checks your computer, asks you before changing anything, installs it, and shows you that it works.

Or, in a terminal:

```bash
curl -fsSL https://raw.githubusercontent.com/Palis8d/session-memory/main/install.sh | sh
```

*If you are an AI assistant, follow [AGENT_INSTALL.md](AGENT_INSTALL.md).*

## What you get

- **One memory for both assistants.** Your requests, their answers, files changed, goals, and how sessions connect. Claude Code can find what Codex did, and the other way round.
- **Skills for Claude Code and Codex**, so each assistant knows how to use the memory without you explaining it.
- **Stays current if you want it to:** automatically every 15 minutes, only when asked, or never. Your choice, changeable any time.
- **Health checks** that notice when Claude Code or Codex change how they write their logs, so the memory doesn't quietly fall behind.

## Use it

Ask your assistant in your own words. Or run it yourself:

```bash
smem search "login page"      # words from any request or answer
smem recent                   # what you worked on in the last few days
smem session 7f3a             # one session, by id or name
smem files config.toml        # which sessions changed a file
smem project myapp            # one project's sessions
```

`smem --help` lists every command.

## Your data stays yours

- It reads `~/.claude` and `~/.codex` and never writes there.
- The memory is one SQLite file in `~/.local/share/session-memory/`. No network, no account, no telemetry.
- Passwords, keys and tokens are replaced with `[REDACTED]` before anything is stored.
- To keep folders out: `smem settings privacy.exclude_paths '["~/clients/acme"]'`.
- `smem uninstall` removes everything it added. Your Claude Code and Codex history is never touched.

## Control

- **Settings:** one commented file, `~/.config/session-memory/config.toml`. Run `smem settings` to see it, or edit it by hand.
- **Refresh:** `smem refresh auto|manual|off --every 30`. Auto refresh is a standard background job: launchd on macOS, a systemd user timer on Linux. `smem refresh --show` prints it.
- **Skills:** `smem skills`. It never overwrites a skill it didn't create.
- **Everything else:** every command that changes something takes `--dry-run`. `smem sql` runs read-only queries, and `smem schema` explains the tables.

## How it works

Claude Code and Codex save each session as a log file on your computer. `smem update` reads the logs that changed since last time and keeps your requests, the answers, the files changed, and the command counts. It links helper sessions and forks to their parents, and indexes the text for full-text search.

## How it's checked

- `smem health` checks that the memory is current, that every log on disk is in it, that counts match Codex's own index, and that the log formats haven't changed.
- `smem health --selftest` shows the checks catching seven kinds of breakage in built-in sample logs.
- 47 automated tests run on Python 3.11 to 3.14.

## Requirements and limits

- macOS or Linux, with [uv](https://docs.astral.sh/uv/) or pipx. uv fetches Python 3.11+ if you don't have it. Windows hasn't been tested, and auto refresh isn't available there.
- Only sessions whose logs are on this computer are included.
- Not stored: hidden reasoning, command output, tool results, images. Commands are counted, not kept.
- Search matches words and their endings, not meaning.

## License

MIT
