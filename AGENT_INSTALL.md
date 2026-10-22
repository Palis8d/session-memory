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
> 2. Keep it up to date automatically every 15 minutes (fastest answers), only when needed (nothing runs in the background; I update it first when it's behind), or never? Updating uses no AI and no tokens.
> 3. Any folders whose sessions should never be stored? (Optional.)

Wait for a clear yes before going on. If they say no, stop.

## 3. Install the program

```bash
uv tool install git+https://github.com/Palis8d/session-memory
# or, without uv:
pipx install git+https://github.com/Palis8d/session-memory
```

If neither `uv` nor `pipx` exists, ask the user before installing one. uv's own installer is `curl -LsSf https://astral.sh/uv/install.sh | sh`.

If `smem` isn't found afterwards, use `~/.local/bin/smem` for the rest of this guide, and tell the user that a new terminal will find it.

Inside a sandbox that blocks writing outside the project (Codex, for example), installing and the next step need the user's approval.

## 4. Set it up

Match the flags to the user's answers. `auto`, `manual` and `off` are the three refresh modes. Add one `--exclude` for each folder they named.

```bash
smem setup --refresh auto --skills all --exclude ~/clients/acme
```

This one step:
- saves the settings;
- writes the skill for each assistant found;
- builds the memory (the first time can take a few minutes);
- runs the self-test and health checks;
- turns on the background refresh last.

To show the user exactly what will happen first, run it with `--dry-run`.

## 5. Check that it works

From the setup output, confirm:
- `Self-test: 7 of 7 cases passed.`
- a `Health:` line;
- the session counts.

Then run:

```bash
smem stats
smem recent --days 7
smem search "WORDS"      # two or three distinctive words from the user's recent work
```

If health is YELLOW or RED, read the lines it prints. They say what was found and what to run next.

## 6. Report to the user (three sentences)

Say what is installed, how many sessions are in memory and from what date, and the refresh mode. Tell them they can now ask Claude Code or Codex about past work in their own words. New sessions pick up the skill.

## Later

- Change the refresh: `smem refresh auto|manual|off --every 30`
- See or change settings: `smem settings`, `smem settings KEY VALUE`
- Remove everything it added: `smem uninstall` lists it first. The user's history is never touched.
