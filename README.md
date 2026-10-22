# Session Memory

One memory, two assistants, every session on your machine. Claude Code and Codex each get a skill to search it: what was asked, answered, changed and decided, by either of them, with dates.

## Ask the way you'd ask a colleague

- "What was that tool we built that could do X? I forget the name."
- "What have we worked on over the last ten days: projects, progress, what's unfinished?"
- "Remind me what we got done yesterday, what's still open, and what we planned but haven't decided."
- "Did we already try this? What happened?"
- "What did Codex do on this project while I was in Claude Code?"

Your assistant turns the question into searches, reads the sessions that match, and answers in its own words. smem provides the record; your assistant does the summarizing. Search matches words, not meaning, so for something whose name you've forgotten the assistant tries a few describing words. That usually finds it, but not always.

## Memory beyond one conversation

An assistant's context holds one conversation at a time. smem lets it reach the rest of your history, a piece at a time, at whatever level of detail the question needs.

- **From overview to exact words.** It can start with recent sessions or a project's history, open one session, then read a single turn in full. For anything else, `smem sql` lets it ask its own questions of the whole record: timelines, work per project, the files changed most often.
- **Fewer tokens.** On a real history of 6,571 sessions, a search takes under a quarter of a second and returns about 5 KB of text, around 1,300 tokens. The raw log of a single main session averages several megabytes. Updating the memory uses no AI and no tokens.
- **Across parallel work.** Sessions that ran at the same time, in either assistant, are all in one place, and helper sessions are linked to the sessions that started them, so the assistant can follow work that was split across them.

## What it gives you

- **One memory for both assistants.** Claude Code finds what Codex did, and the other way round.
- **Skills, so they just use it.** Each assistant learns how to search the memory; you don't explain anything.
- **History that outlasts deletion.** Claude Code deletes its logs after 30 days by default; smem can keep those sessions, at a hundredth of the raw size or less.
- **Private by design.** No network, no account, files readable only by you, and common secrets redacted before anything is stored.
- **You stay in control.** Exclude folders, forget any session, preview every change, and refresh automatically, on demand, or never.
- **It checks itself.** Health checks flag log-format changes and missing logs, and tests break each key safety rule on purpose to prove they'd notice.
- **Installed by your assistant.** Paste one sentence; it asks before changing anything and verifies that it works.

## Install

Paste this into Claude Code or Codex:

> Install Session Memory for me by following https://github.com/Palis8d/session-memory/blob/main/AGENT_INSTALL.md

Your assistant checks your computer, asks you before changing anything, installs it, and shows you that it works.

Or, in a terminal:

```bash
curl -fsSL https://raw.githubusercontent.com/Palis8d/session-memory/main/install.sh | sh
```

To check first: read [install.sh](install.sh) (49 lines), or install and run `smem setup --dry-run` to see every change before it's made.

*If you are an AI assistant, follow [AGENT_INSTALL.md](AGENT_INSTALL.md).*

## Verify it yourself

```bash
smem health --selftest      # the checks catch 5 kinds of breakage in built-in samples, and stay quiet in 2 normal ones
smem setup --dry-run        # every change setup would make, without making it
```

And in a copy of this repository, this prints nothing, because no file imports a network library:

```bash
grep -rnE "^\s*(import|from)\s+(socket|ssl|http|urllib\.request|urllib3|requests|httpx|aiohttp|ftplib|smtplib|xmlrpc|websocket)" src/
```

The only other programs smem starts are `launchctl` or `systemctl` (for auto refresh) and `zstd` (to read compressed Codex logs, when Python can't).

## How it was tested before release

- **A fresh install by an AI agent** that knew nothing about the project, in a clean throwaway home, following only this README and [AGENT_INSTALL.md](AGENT_INSTALL.md). It then used smem to answer questions about sample history.
- **Two separate AI reviews**, one for correctness and one for privacy and security. Each finding was reproduced before it was fixed.
- **Secret redaction** is tested against 124 realistic fake secrets, and 87 ordinary snippets that must stay unchanged.
- **Every key safety rule broken on purpose**, one at a time, to confirm a test notices. All 21 breaks are caught; rerun them with `python3 -I tests/break_on_purpose.py` (a few minutes).
- **A real history of more than 6,000 Claude Code and Codex sessions**, read-only, into a throwaway folder.
- **68 automated tests** on Python 3.11 to 3.14. In daily use, `smem health` checks that the memory is current, that every log on disk is in it, that counts match Codex's own index, and that the log formats haven't changed; `--selftest` shows those checks catching five kinds of breakage (four log-format changes, and logs vanishing from disk) and staying quiet in two normal situations.

[SECURITY.md](SECURITY.md) lists each safety promise with the test that proves it, and what smem does and doesn't protect against.

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

## Keeping it current

Updating the memory uses no AI and no tokens. It's your computer reading log files, and it reads only the ones that changed since last time.

- **Auto** (every 15 minutes): the memory is always ready, and answers come instantly.
- **Manual**: nothing runs in the background. When results are out of date, they end with a plain note, for example "Since then 2 Claude Code sessions changed." The assistant can then update first, which takes seconds. Claude Code may do this without asking you; in Codex, its sandbox setting decides whether it asks.
- **Off**: nothing updates. Search still works on what's stored.

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

## Requirements and limits

- macOS or Linux, with [uv](https://docs.astral.sh/uv/) or pipx. uv fetches Python 3.11+ if you don't have it. Windows hasn't been tested, and auto refresh isn't available there.
- Only sessions whose logs are or were on this computer are included. A session deleted before you install smem can't be recovered.
- Not stored: hidden reasoning, command output and tool results, images. Commands are counted, not kept.
- Search matches words and their endings, not meaning.
- Some Linux systems have an unrelated memory-reporting tool also called `smem`. If yours does, the one found first on your PATH runs.

## License

MIT
