# Session Memory

One searchable memory of your Claude Code and Codex sessions, kept on your own computer.

Ask your assistant "what did we decide about the login page last week?" or "which session changed `config.toml`?" and it can look it up, across every Claude Code session and Codex thread on this computer.

## Why it helps

- **Each new session starts blank.** Earlier decisions, fixes and the reasons behind them sit in old logs that nobody reads. With smem, your assistant can look them up.
- **Claude Code and Codex don't see each other's work.** smem gives them one shared memory.
- **Old logs are long and costly to read.** Searching the memory returns a short list of matching moments, with dates, so an assistant reads only the session it needs. That saves time and tokens.
- **Claude Code deletes its session logs after 30 days by default.** The memory can keep those sessions searchable afterwards, or forget them too. You choose.

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
- **Stays current your way:** automatically every 15 minutes, only when needed, or never. See below.
- **Health checks** that notice when Claude Code or Codex change how they write their logs, so the memory doesn't quietly fall behind.

## Built with care

- **Stays on your computer.** No network, no account, no telemetry. Your history is only read, and the memory file is readable only by your account.
- **Secrets are removed before storing.** Common forms of passwords, keys and tokens are replaced with `[REDACTED]`, checked against 99 realistic fake secrets.
- **Asks before it changes anything.** Installing through your assistant starts with a question to you. Setup, refresh, settings, skills and uninstall can all show what they would do first (`--dry-run`), and uninstall removes only what smem created.
- **Tested on real use.** Built and checked against a real history of more than 6,000 sessions: the first build takes about two minutes, and later updates about two seconds. 68 automated tests run on Python 3.11 to 3.14.

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

## Keeping sessions after their logs are deleted

A log can disappear for three reasons: the assistant's own cleanup (Claude Code deletes logs after 30 days by default, its `cleanupPeriodDays` setting; Codex never cleans up on its own), you deleting a session yourself (both apps let you), or a change in an app's cleanup rule.

smem tells these apart as well as it can: a Claude Code log older than its cleanup period looks like the cleanup; a younger one, or any Codex log, looks deleted by hand. You choose what the memory does:

- **all** (the default): keep every session, so nothing is lost by accident.
- **auto**: keep sessions the assistants' own cleanup removed, and forget the ones you deleted yourself.
- **main**: keep main sessions and forget the helper sessions they started, for the leanest memory.
- **none**: forget deleted sessions too, so the memory mirrors what's on disk.

Set it with `smem settings retention.keep all|auto|main|none`. Keeping costs little: the memory stores only requests and answers, about a hundredth of the raw logs' size. `smem forget ID` erases any one session (and the helpers it started) for good, and `smem forget --deleted` erases every kept session that looks deleted by hand.

`smem health` reports what happened: how many logs each assistant's cleanup took, how many look deleted by hand, and a warning when many younger logs vanish in one update, which suggests an app changed its cleanup rule. To keep Claude Code's full original logs as well, raise `cleanupPeriodDays` in `~/.claude/settings.json`.

## Keeping it current, and what it costs

Updating the memory uses no AI and no tokens. It's your computer reading log files, and it reads only the ones that changed since last time.

- **Auto** (every 15 minutes): the memory is always ready, and answers come instantly.
- **Manual**: nothing runs in the background. When results are out of date, they end with a plain note, for example "Since then 2 Claude Code sessions changed." The assistant can then update first, which takes seconds. Claude Code may do this without asking you; in Codex, its sandbox setting decides whether it asks.
- **Off**: nothing updates. Search still works on what's stored.

Either way, the memory saves tokens. Without it, an assistant looking for an old task has to read long raw log files. With it, the assistant reads a short list of matches and then only the session it needs.

## Your data stays yours

- It only reads your Claude Code and Codex history and never changes it. The one thing it adds in `~/.claude` is its skill (`~/.claude/skills/smem`); Codex's skill goes in `~/.agents/skills/smem`.
- The memory is one SQLite file in `~/.local/share/session-memory/`, readable only by your account. No network, no account, no telemetry. It grows with your history, roughly 120 KB per session.
- Common forms of passwords, keys and tokens are replaced with `[REDACTED]` before anything is stored. This is best effort: a secret in an unusual format can get through.
- To keep folders out: `smem settings privacy.exclude_paths '["~/clients/acme"]'`. This sets the whole list (`smem settings privacy.exclude_paths` shows it). The next update removes those sessions and erases their text from the file.
- When an assistant reads results, they go to that assistant's provider like anything else it reads. For example, Claude Code sessions that Codex looks up are sent to OpenAI.
- `smem forget ID` erases a session from the memory for good; updates won't read it back (`--undo` reverses that).
- `smem uninstall` lists, then removes, its skills, background job, settings and memory. Your Claude Code and Codex history is never touched. The program itself goes with `uv tool uninstall session-memory` (or `pipx uninstall session-memory`).

## Control

- **Settings:** one file, `~/.config/session-memory/config.toml`. `smem settings` shows it; `smem settings KEY VALUE` changes one value. You can also edit the file by hand, but `smem` rewrites it on changes, so comments you add aren't kept.
- **Refresh:** `smem refresh auto|manual|off --every 30`. Auto refresh is a standard background job: launchd on macOS, a systemd user timer on Linux. `smem refresh --show` prints it. On macOS, turning it on shows a "Background Items Added" notice naming Python; that's this job. On Linux the timer runs while you're logged in.
- **Skills:** `smem skills`. It never overwrites a skill it didn't create, and upgrades leave a skill you edited alone.
- **Previews:** `setup`, `refresh`, `settings`, `skills`, `forget` and `uninstall` take `--dry-run`.
- **Your own queries:** `smem sql` runs read-only SQL (it can't write anything), and `smem schema` explains the tables.

## How it works

Claude Code and Codex save each session as a log file on your computer. `smem update` reads the logs that changed since last time and keeps your requests, the answers, the files changed, and the command counts. It links helper sessions and forks to their parents, and indexes the text for full-text search.

## How it's checked

- `smem health` checks that the memory is current, that every log on disk is in it, that counts match Codex's own index, and that the log formats haven't changed.
- `smem health --selftest` shows the checks catching five kinds of log-format change in built-in sample logs, and staying quiet in two normal situations.
- 68 automated tests run on Python 3.11 to 3.14.

## Requirements and limits

- macOS or Linux, with [uv](https://docs.astral.sh/uv/) or pipx. uv fetches Python 3.11+ if you don't have it. Windows hasn't been tested, and auto refresh isn't available there.
- Only sessions whose logs are or were on this computer are included. A session deleted before you install smem can't be recovered.
- Not stored: hidden reasoning, command output and tool results, images. Commands are counted, not kept.
- Search matches words and their endings, not meaning.
- Some Linux systems have an unrelated memory-reporting tool also called `smem`. If yours does, the one found first on your PATH runs.

## License

MIT
