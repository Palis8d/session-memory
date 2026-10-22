---
name: smem
description: Search and read this computer's past Claude Code and Codex sessions (requests, answers, files changed, command counts, goals, and how sessions connect) with the `smem` command. Use when a request refers to earlier work, past sessions or threads, or to the session memory itself (its health, settings or refresh).
{{FRONTMATTER_EXTRA}}---

# Session Memory (`smem`)

`smem` keeps one local, searchable record of the Claude Code and Codex sessions on this computer. It reads the assistants' own log files and never changes them. The record stays on this computer.

This skill describes what the record holds and how to read it. What to look up, and what to do with the results, is up to the user and you.
{{COMMAND_NOTE}}
## Reading

These commands only read. They work inside a sandbox that blocks writing.

| Command | Returns |
| --- | --- |
| `smem search "WORDS"` | Matching turns, best match first: local date and time, assistant, session name, `[session id, turn number]`, and the matching part of the request (`YOU:`) and of the answer (`AGENT:`), with matched words in `[brackets]`. Options: `--agent claude\|codex`, `--project NAME`, `--days N`, `--helpers`, `--limit N` (default 15). |
| `smem session ID` | One session: name, assistant, project, folder, model, branch, dates, counts, parent, goal, links, the last 5 turns, files changed. `ID` can be a whole id, its first characters, or part of the session name. `--turns N` (0 = all), `--full` (untrimmed text), `--agent`. |
| `smem recent` | Sessions active in the last 3 days, newest first. `--days N`, `--agent`, `--helpers`, `--limit N`. |
| `smem project NAME` | Sessions whose project or folder contains NAME, newest first. `--answers` adds each one's last answer. |
| `smem files PATH` | Sessions that changed a file whose path contains PATH, with the change and the turn. |
| `smem goals` | Codex goals with status and objective. `--status S`. |
| `smem links ID` | Sessions connected to this one: `helper` (a sub-agent it started), `fork` (a copy it branched from), `mentions` (a Claude Code session that names a Codex thread id). |
| `smem stats` | Totals per assistant, the time of the last update, the refresh mode, and where the record is. |
| `smem sql "SELECT …"` | Any read-only SQL query. Tab-separated by default; `--json` gives one object per row; `--limit N` (default 1000, 0 = all). |
| `smem schema` | Every table and column, with a note on each. |

### Search syntax

- All words must match, in any order. Endings are ignored, so `deploy` also finds `deployed` and `deploying`.
- `"exact phrase"`, `this OR that`, `this NOT that`, and `prefix*` work.
- `request: (words)` searches only the user's requests, and `answer: (words)` only the assistants' answers.
- Put words that contain punctuation in double quotes, inside single quotes for the shell: `smem search '"config.toml" error'`.

### Reading the output

- When sessions changed after the last update, results end with a note saying when the memory was last updated and how many Claude Code and Codex sessions changed since.
- Times are local, `YYYY-MM-DD HH:MM`. In `smem sql`, columns ending in `_at` are UTC (ISO 8601).
- List rows show: date, assistant (`·h` marks a helper session, a sub-agent another session started), name, `t` turns, `c` commands, `f` files changed, and the start of the session id.
- `≈` before a command count: estimated from code the assistant ran to issue commands, rather than counted from finished commands.
- `(turn was cancelled)`: stopped before an answer.
- Rewound turns (Codex): on a branch that was later rewound. They happened, but they aren't in the thread's visible history.
- Forks start with a copy of their parent's turns. Those copies are left out, and their number is shown as inherited turns.
- A helper's "inherited context" is its parent's text, not the helper's own request. A Codex helper's own brief is often not readable in its log.
- Text longer than 200,000 characters is cut; the original log has the rest.
- Values that look like passwords, keys or tokens are stored as `[REDACTED]`.

### Not recorded

Hidden reasoning, command output and tool results, images and attachments. Commands are counted, not stored. Changed files are listed by path and kind of change, not content.

## Freshness and health

- The record is as current as its last update; `smem stats` shows when that was. `smem refresh` shows the mode: `auto` updates in the background every few minutes, `manual` updates only when `smem update` runs, and `off` never updates.
- `smem update` reads only new and changed logs, usually in seconds. It writes to the memory folder, so where writing outside the project is blocked it needs the user's permission. `--full` re-reads everything, and the current record stays until the new one is complete.
- `smem health` lists checks as GREEN, YELLOW or RED, each with what it found. The checks cover staleness, logs missing from the record, and changes in the assistants' log formats (records of an unknown type, fields that stopped reading, a sudden drop in what's captured, a tool name never seen before). `--fast` skips the comparison with Codex's own index. `--json` is available.
- `smem health --selftest` runs the checks on built-in sample logs to show they work. `smem health --ack NAME` marks a new tool name as reviewed.

## Settings

The settings belong to the user. They live in one TOML file.

- `smem settings` shows every setting and the file's path. `smem settings KEY VALUE` changes one, for example `smem settings privacy.exclude_paths '["~/private"]'`.
- `sources.*`: which assistants' history is read, and from which folders.
- `privacy.exclude_paths`, `privacy.exclude_projects`: sessions never stored (they are removed at the next update).
- `refresh.mode`, `refresh.interval_minutes`: also set with `smem refresh auto|manual|off --every N`.
- `skills.*`: which assistants get this skill. `smem skills` shows where each skill is.
- `smem uninstall` lists what it would remove, then removes the skills, the background job, the settings and the record. It never touches the assistants' own history. `--keep-data` keeps the record, and `--dry-run` only lists.
- Commands that change something accept `--dry-run`.
