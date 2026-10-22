"""Background refresh for `refresh.mode = auto`: a launchd agent on macOS, a systemd user timer on Linux.

Everything this module adds is one plain file the user can read (shown by `smem refresh --show`), and
`smem refresh manual` or `smem uninstall` removes it again. Nothing runs as root.
"""
import os
import platform
import subprocess
import sys

from . import config, skills

LABEL = "com.palis8d.session-memory"
RAN = []  # commands recorded instead of run when SESSION_MEMORY_DRY_SYSTEM is set


def _command():
    """What the background job runs: this same installation, refreshing quietly."""
    return skills.program() + ["update", "--scheduled"]


def _log_path():
    return os.path.join(config.data_dir(), "refresh.log")


def _env():
    """Pin every folder the job uses. Background jobs don't load the shell's settings, so without this a
    user who moved Claude Code's or Codex's folder would have the job read (and reconcile against) the
    default one."""
    env = {"SESSION_MEMORY_HOME": config.data_dir(), "SESSION_MEMORY_CONFIG": config.config_path(),
           "CODEX_HOME": config.codex_home(), "CLAUDE_CONFIG_DIR": config.claude_home()}
    if os.environ.get("SESSION_MEMORY_DB_NAME"):
        env["SESSION_MEMORY_DB_NAME"] = os.environ["SESSION_MEMORY_DB_NAME"]
    return env


def plan(interval_minutes):
    """The files and commands auto-refresh needs on this system, without doing anything.
    Returns (kind, [(path, text)], [commands to enable], [commands to disable])."""
    cmd = _command()
    system = platform.system()
    if system == "Darwin":
        path = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
        args = "\n".join(f"    <string>{_xml(c)}</string>" for c in cmd)
        env = "\n".join(f"    <key>{k}</key><string>{_xml(v)}</string>" for k, v in _env().items())
        text = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>
{args}
  </array>
  <key>EnvironmentVariables</key>
  <dict>
{env}
  </dict>
  <key>StartInterval</key><integer>{interval_minutes * 60}</integer>
  <key>RunAtLoad</key><true/>
  <key>LowPriorityIO</key><true/>
  <key>Umask</key><integer>63</integer>
  <key>Nice</key><integer>10</integer>
  <key>StandardOutPath</key><string>{_xml(_log_path())}</string>
  <key>StandardErrorPath</key><string>{_xml(_log_path())}</string>
</dict>
</plist>
"""
        domain = f"gui/{os.getuid()}"
        return ("launchd", [(path, text)],
                [["launchctl", "bootout", f"{domain}/{LABEL}"], ["launchctl", "bootstrap", domain, path]],
                [["launchctl", "bootout", f"{domain}/{LABEL}"]])
    if system == "Linux":
        base = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "systemd", "user")
        service = os.path.join(base, "session-memory.service")
        timer = os.path.join(base, "session-memory.timer")
        exec_line = " ".join(_quote(c) for c in cmd)
        env_lines = "\n".join(f"Environment={_quote(f'{k}={v}')}" for k, v in _env().items())
        return ("systemd", [
            (service, f"""[Unit]
Description=Session Memory refresh (smem)

[Service]
Type=oneshot
Nice=10
UMask=0077
{env_lines}
ExecStart={exec_line}
"""),
            (timer, f"""[Unit]
Description=Refresh Session Memory every {interval_minutes} minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec={interval_minutes}min

[Install]
WantedBy=timers.target
"""),
        ], [["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", "--now", "session-memory.timer"]],
            [["systemctl", "--user", "disable", "--now", "session-memory.timer"]])
    return ("unsupported", [], [], [])


def enable(interval_minutes, dry_run=False):
    kind, files, on, _ = plan(interval_minutes)
    if kind == "unsupported":
        raise SystemExit("Automatic refresh supports macOS and Linux. Use `smem refresh manual` here; "
                         "your assistants can refresh the memory when they use it.")
    if dry_run:
        return kind, files, on
    for path, text in files:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
    os.makedirs(config.data_dir(), exist_ok=True)
    for c in on:
        r = _run(c)
        if r.returncode and "bootout" not in c:  # a first `bootout` fails harmlessly when nothing is loaded
            disable()
            raise SystemExit(f"The system refused the background job ({' '.join(c)}: "
                             f"{(r.stderr or r.stdout).strip() or r.returncode}). Nothing was left installed; "
                             "`smem refresh manual` works everywhere.")
    return kind, files, on


def disable(dry_run=False):
    kind, files, _, off = plan(15)
    if not any(os.path.exists(p) for p, _ in files):
        return kind, [], []  # nothing installed: touch nothing
    if dry_run:
        return kind, [p for p, _ in files if os.path.exists(p)], off
    for c in off:
        _run(c)
    removed = []
    for path, _ in files:
        if os.path.exists(path):
            os.remove(path)
            removed.append(path)
    if kind == "systemd":
        _run(["systemctl", "--user", "daemon-reload"])
    return kind, removed, off


def status():
    kind, files, _, _ = plan(config.load()["refresh"]["interval_minutes"])
    present = [p for p, _ in files if os.path.exists(p)]

    def same(path, text):
        try:
            with open(path) as f:
                return f.read() == text
        except OSError:
            return False
    return {"system": kind, "installed": bool(files) and len(present) == len(files), "files": present,
            "current": all(same(p, t) for p, t in files)}


def _run(c):
    if os.environ.get("SESSION_MEMORY_DRY_SYSTEM"):  # tests: record the command instead of running it
        RAN.append(c)
        return subprocess.CompletedProcess(c, 0, "", "")
    try:
        return subprocess.run(c, capture_output=True, text=True)
    except OSError as e:  # the command itself is missing, e.g. no systemd
        return subprocess.CompletedProcess(c, 1, "", str(e))


def _xml(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _quote(s):
    """One argument for a systemd unit line: quoted, with systemd's own specials (%, $, \\) escaped."""
    if "\n" in s or "\r" in s:
        raise SystemExit(f"Automatic refresh can't use a path with a line break in it: {s!r}")
    s = s.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{s}"'
