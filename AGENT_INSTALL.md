# Installing Session Memory: instructions for an AI assistant

You are installing Session Memory (`smem`) for the user. It gives Claude Code and Codex one shared, searchable memory of their past sessions on this computer. Follow these steps in order. Keep your messages to the user short and in plain words.

## 1. Check the computer (no changes yet)

Run these and note the results:

```bash
uname -s                                   # Darwin (macOS) or Linux; on anything else, tell the user it isn't supported yet
command -v uv || command -v pipx           # an installer; if neither exists, see step 3
ls -d ~/.claude/projects ~/.codex 2>/dev/null   # whose history exists here
command -v smem && smem --version          # already installed?
```

If `$CLAUDE_CONFIG_DIR` or `$CODEX_HOME` is set, those folders are used instead of `~/.claude` and `~/.codex`.

## 2. Ask the user (one message, then wait)

Tell them, in about these words, naming only the assistants you found:

> Session Memory reads your Claude Code and Codex history on this computer and builds a private, searchable memory of it in `~/.local/share/session-memory`. It never changes your history, and nothing leaves this computer. It adds the `smem` program, and a skill so Claude Code and Codex know how to use it.
>
> 1. OK to install it and connect your Claude Code and Codex history?
> 2. Keep it up to date automatically every 15 minutes (fastest answers), only when needed (nothing runs in the background; I can update it first when it's behind), or never? Updating uses no AI and no tokens.
> 3. Claude Code deletes its session logs after 30 days, and you can delete sessions yourself in either app. When a log is deleted, should the memory keep the session (recommended), keep only what the apps' own cleanup deleted and forget what you delete yourself, or forget it too?
> 4. Any folders whose sessions should never be stored? (Optional.)

Wait for a clear yes before going on. If they say no, stop.

## 3. Install the program

```bash
uv tool install git+https://github.com/Palis8d/session-memory
# or, without uv:
pipx install git+https://github.com/Palis8d/session-memory
```

If neither `uv` nor `pipx` exists, ask the user before installing one. uv's own installer is `curl -LsSf https://astral.sh/uv/install.sh | sh`.

Check it's this program: `smem --version` should print `smem 0.… (Session Memory)`. If `smem` isn't found, or something else answers (some Linux systems have an unrelated memory tool called `smem`), use the full path `~/.local/bin/smem` for the rest of this guide. If it simply wasn't found, tell the user a new terminal will find it.

Inside a sandbox that blocks writing outside the project (Codex, for example), installing and the next step need the user's approval.

## 4. Set it up

Match `--refresh` to the user's answer (`auto`, `manual` or `off`) and `--keep` to theirs (`all`, `auto` for "only what the apps' cleanup deleted", or `none`).

```bash
smem setup --refresh auto --keep all --skills all
```

If the user named folders to keep out, add `--exclude FOLDER` once for each one, using its full path (for example `--exclude ~/clients/acme`).

This one step:
- saves the settings;
- writes the skill for each assistant found;
- builds the memory (the first time can take a few minutes);
- runs the self-test and health checks;
- turns on the background refresh last.

To show the user exactly what will happen first, run it with `--dry-run`.

If they chose `auto` on a Mac, tell them macOS may show a "Background Items Added" notice naming Python. That notice is this background update.

## 5. Check that it works

From the setup output, confirm:
- `Self-test: 7 of 7 cases passed.`
- a `Health:` line;
- the session counts.

Then run:

```bash
smem stats
smem recent --days 30
smem search "WORDS"      # two or three distinctive words from the user's recent work
```

GREEN means all is well, and a fresh install should be GREEN. A YELLOW line is a note worth reading; each one says what it found. RED means something is wrong: tell the user what the RED line says. `smem health` exits with 0, 1 or 2 for GREEN, YELLOW or RED, so a YELLOW exit code isn't a failed install.

## 6. Report to the user (three sentences)

Say what is installed, how many sessions are in memory and from what date, and the refresh mode. Tell them they can now ask Claude Code or Codex about past work in their own words. New sessions pick up the skill.

If the user has Claude Code, also tell them: Claude Code deletes its own session logs after 30 days by default. The memory keeps what their `--keep` answer says, but it stores only requests and answers, not the full logs. If they want the full logs kept too, offer to raise `cleanupPeriodDays` in `~/.claude/settings.json` (for example to 36500), and change it only if they say yes.

## Later

- Change the refresh: `smem refresh auto|manual|off --every 30`
- See or change settings: `smem settings`, `smem settings KEY VALUE` (for example `smem settings retention.keep main`)
- Erase one session for good: `smem forget ID --dry-run`, then with the user's yes `smem forget ID --yes`
- Remove everything it added: `smem uninstall` lists what it would remove; with the user's yes, run `smem uninstall --yes`. The user's history is never touched. Then remove the program with `uv tool uninstall session-memory` (or `pipx uninstall session-memory`).
