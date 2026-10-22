"""The `smem` skill for Claude Code and Codex.

A skill is a folder holding a SKILL.md, which an assistant reads when a request matches its description.
This module writes one such folder per assistant and nothing else. A folder it didn't create (it has no
marker file) is never overwritten or removed.
"""
import hashlib
import os
import shlex
import shutil
import sys

from . import __version__, config, store

NAME = "smem"
MARKER = ".installed-by-smem"
AGENTS = ("claude", "codex")
LABELS = {"claude": "Claude Code", "codex": "Codex"}
TEMPLATE = os.path.join(store.PACKAGE_DIR, "skill_templates", "SKILL.md")
READ_COMMANDS = ("search", "session", "recent", "project", "files", "goals", "links", "stats", "sql", "schema")
# Health checks run without asking only in their read-only forms (`--ack` writes and silences a warning).
READ_EXACT = ("health", "health --fast", "health --json", "health --selftest")


def folder(agent):
    return os.path.join(config.skills_dir(agent), NAME)


def assistant_found(agent):
    """Claude Code keeps ~/.claude and Codex ~/.codex (or wherever settings and their own variables point)."""
    return os.path.isdir(config.claude_home() if agent == "claude" else config.codex_home())


def ours(path):
    return os.path.isfile(os.path.join(path, MARKER))


def _marker(path):
    """{'version', 'command', 'sha256'} recorded when smem last wrote this skill."""
    out = {}
    try:
        with open(os.path.join(path, MARKER)) as f:
            lines = f.read().splitlines()
    except OSError:
        return out
    if lines:
        out["version"] = lines[0].strip()
    for line in lines[1:]:
        key, sep, value = line.partition("=")
        if sep:
            out[key.strip()] = value.strip()
    return out


def _sha(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def program():
    """How to run this installation regardless of the current folder or PATH: its own `smem` script (next
    to the Python running it, where uv and pipx put it), else Python with -P, so that a file in the current
    folder can never stand in for one of its modules."""
    script = os.path.join(os.path.dirname(sys.executable), NAME)
    if os.path.isfile(script) and os.access(script, os.X_OK):
        return [script]
    return [sys.executable, "-P", "-m", "session_memory"]


def command():
    """How an assistant should run smem: the bare name when PATH finds this installation (Linux also has an
    unrelated `smem`), else this installation's full path."""
    prog = program()
    found = shutil.which(NAME)
    if found and len(prog) == 1 and os.path.realpath(found) == os.path.realpath(prog[0]):
        return NAME
    return " ".join(shlex.quote(p) for p in prog)


def render(agent, cmd=None):
    cmd = cmd or command()
    with open(TEMPLATE) as f:
        text = f.read()
    extra = ""
    if agent == "claude":
        # Claude Code may read and update the memory without asking: an update only reads the assistants' logs
        # and writes smem's own memory file. Only the plain `update` is allowed: its options override the user's
        # choices (--force runs when refresh is off, --accept-removals lifts the mass-removal guard), so they ask,
        # as do setup, settings, refresh, skills and uninstall.
        allowed = ([f"Bash({cmd} {c}:*)" for c in READ_COMMANDS] + [f"Bash({cmd} {c})" for c in READ_EXACT]
                   + [f"Bash({cmd} update)"])
        extra = "allowed-tools: " + ", ".join(allowed) + "\n"
    note = "" if cmd == NAME else f"\n`smem` isn't on this computer's PATH, so run it as `{cmd}`.\n"
    return text.replace("{{FRONTMATTER_EXTRA}}", extra).replace("{{COMMAND_NOTE}}", note)


def status():
    out = {}
    for agent in AGENTS:
        path = folder(agent)
        out[agent] = {"folder": path, "assistant_found": assistant_found(agent),
                      "installed": ours(path), "foreign": os.path.exists(path) and not ours(path)}
    return out


def install(agent, dry_run=False, cmd=None):
    """Returns (done, message)."""
    path = folder(agent)
    if os.path.exists(path) and not ours(path):
        return False, f"{LABELS[agent]}: left {path} alone (a different skill named '{NAME}' is already there)"
    if not assistant_found(agent):
        return False, f"{LABELS[agent]}: not found on this computer; skipped"
    if dry_run:
        return True, f"{LABELS[agent]}: would write {os.path.join(path, 'SKILL.md')}"
    cmd = cmd or command()
    text = render(agent, cmd)
    os.makedirs(path, exist_ok=True)
    skill = os.path.join(path, "SKILL.md")
    with open(skill + ".tmp", "w") as f:
        f.write(text)
    os.replace(skill + ".tmp", skill)
    with open(os.path.join(path, MARKER), "w") as f:  # last, so a failed write above is retried next time
        f.write(f"{__version__}\ncommand={cmd}\nsha256={hashlib.sha256(text.encode()).hexdigest()}\n"
                "This folder was written by smem (Session Memory). `smem skills remove` deletes it.\n")
    return True, f"{LABELS[agent]}: skill written to {path}"


def remove(agent, dry_run=False):
    path = folder(agent)
    if not os.path.exists(path):
        return False, f"{LABELS[agent]}: no skill installed"
    if not ours(path):
        return False, f"{LABELS[agent]}: left {path} alone (smem didn't create it)"
    if dry_run:
        return True, f"{LABELS[agent]}: would remove {path}"
    for name in ("SKILL.md", MARKER):  # only the files smem writes; anything the user added stays
        if os.path.lexists(os.path.join(path, name)):
            os.remove(os.path.join(path, name))
    try:
        os.rmdir(path)
    except OSError:
        return True, f"{LABELS[agent]}: removed the skill; left {path} because it holds other files"
    return True, f"{LABELS[agent]}: removed {path}"


def sync(dry_run=False):
    """Make the installed skills match settings: install where turned on, remove ours where turned off."""
    cfg = config.load()
    msgs = []
    for agent in AGENTS:
        if cfg["skills"][agent]:
            msgs.append(install(agent, dry_run)[1])
        elif os.path.exists(folder(agent)) and ours(folder(agent)):
            msgs.append(remove(agent, dry_run)[1])
    return msgs


def refresh_installed():
    """After an upgrade, rewrite skills this tool installed so they describe the current commands. Keeps
    the command chosen at install (a background job sees a different PATH) and leaves a SKILL.md the user
    edited alone."""
    for agent in AGENTS:
        path = folder(agent)
        if not ours(path):
            continue
        mark = _marker(path)
        if mark.get("version") == __version__:
            continue
        if mark.get("sha256") and mark["sha256"] != _sha(os.path.join(path, "SKILL.md")):
            continue  # edited by hand: `smem skills install` replaces it when the user wants that
        install(agent, cmd=mark.get("command"))
