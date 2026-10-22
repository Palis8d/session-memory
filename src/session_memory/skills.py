"""The `smem` skill for Claude Code and Codex.

A skill is a folder holding a SKILL.md, which an assistant reads when a request matches its description.
This module writes one such folder per assistant and nothing else. A folder it didn't create (it has no
marker file) is never overwritten or removed.
"""
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
READ_COMMANDS = ("search", "session", "recent", "project", "files", "goals", "links", "stats", "sql", "schema",
                 "health")
NO_ASK = READ_COMMANDS + ("update",)


def folder(agent):
    return os.path.join(config.skills_dir(agent), NAME)


def assistant_found(agent):
    """Claude Code keeps ~/.claude and Codex ~/.codex (or wherever settings and their own variables point)."""
    return os.path.isdir(config.claude_home() if agent == "claude" else config.codex_home())


def ours(path):
    return os.path.isfile(os.path.join(path, MARKER))


def command():
    """How an assistant should run smem: the bare name when it's on PATH, else this installation's full path."""
    found = shutil.which(NAME)
    if found:
        return NAME
    return f"{shlex.quote(sys.executable)} -m session_memory"


def render(agent, cmd=None):
    cmd = cmd or command()
    with open(TEMPLATE) as f:
        text = f.read()
    extra = ""
    if agent == "claude":
        # Claude Code may read and update the memory without asking: an update only reads the assistants' logs
        # and writes smem's own memory file. Setup, settings, refresh, skills and uninstall still ask.
        extra = "allowed-tools: " + ", ".join(f"Bash({cmd} {c}:*)" for c in NO_ASK) + "\n"
    note = "" if cmd == NAME else f"\n`smem` isn't on this computer's PATH, so run it as `{cmd}`.\n"
    return text.replace("{{FRONTMATTER_EXTRA}}", extra).replace("{{COMMAND_NOTE}}", note)


def status():
    out = {}
    for agent in AGENTS:
        path = folder(agent)
        out[agent] = {"folder": path, "assistant_found": assistant_found(agent),
                      "installed": ours(path), "foreign": os.path.exists(path) and not ours(path)}
    return out


def install(agent, dry_run=False):
    """Returns (done, message)."""
    path = folder(agent)
    if os.path.exists(path) and not ours(path):
        return False, f"{LABELS[agent]}: left {path} alone (a different skill named '{NAME}' is already there)"
    if not assistant_found(agent):
        return False, f"{LABELS[agent]}: not found on this computer; skipped"
    if dry_run:
        return True, f"{LABELS[agent]}: would write {os.path.join(path, 'SKILL.md')}"
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, MARKER), "w") as f:
        f.write(f"{__version__}\nThis folder was written by smem (Session Memory). `smem skills remove` deletes it.\n")
    with open(os.path.join(path, "SKILL.md"), "w") as f:
        f.write(render(agent))
    return True, f"{LABELS[agent]}: skill written to {path}"


def remove(agent, dry_run=False):
    path = folder(agent)
    if not os.path.exists(path):
        return False, f"{LABELS[agent]}: no skill installed"
    if not ours(path):
        return False, f"{LABELS[agent]}: left {path} alone (smem didn't create it)"
    if dry_run:
        return True, f"{LABELS[agent]}: would remove {path}"
    shutil.rmtree(path)
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
    """After an upgrade, rewrite skills this tool installed so they describe the current commands."""
    for agent in AGENTS:
        path = folder(agent)
        if not ours(path):
            continue
        with open(os.path.join(path, MARKER)) as f:
            if f.readline().strip() == __version__:
                continue
        install(agent)
