#!/bin/sh
# Session Memory installer: installs the `smem` program with uv or pipx, then runs `smem setup`.
# It reads your Claude Code and Codex history and never changes it; nothing leaves your computer.
# Remove everything later with `smem uninstall`.
set -eu

SOURCE="${SMEM_SOURCE:-git+https://github.com/Palis8d/session-memory}"

if command -v uv >/dev/null 2>&1; then
  echo "Installing smem with uv…"
  uv tool install --reinstall --quiet "$SOURCE"
elif command -v pipx >/dev/null 2>&1; then
  echo "Installing smem with pipx…"
  pipx install --force "$SOURCE" >/dev/null
else
  echo "smem installs with uv or pipx, and neither is on this computer."
  echo "Install uv (https://docs.astral.sh/uv/), then run this again:"
  echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
  exit 1
fi

# The program just installed: uv and pipx put it in ~/.local/bin by default. Some Linux systems have an
# unrelated `smem`, so check that the one found is this program.
SMEM=""
for candidate in "$(command -v smem 2>/dev/null || true)" "${UV_TOOL_BIN_DIR:-$HOME/.local/bin}/smem" "${PIPX_BIN_DIR:-$HOME/.local/bin}/smem"; do
  if [ -n "$candidate" ] && [ -x "$candidate" ] && "$candidate" --version 2>/dev/null | grep -q "(Session Memory)"; then
    SMEM="$candidate"
    break
  fi
done
if [ -z "$SMEM" ]; then
  echo "Installed, but the new smem wasn't found. Open a new terminal and run: smem setup"
  exit 1
fi

REFRESH=manual
if (exec </dev/tty) 2>/dev/null; then
  printf 'Keep the memory up to date automatically, every 15 minutes? [Y/n] ' >/dev/tty
  read -r answer </dev/tty || answer=""
  case "$answer" in
    [nN]*) REFRESH=manual ;;
    *) REFRESH=auto ;;
  esac
fi

"$SMEM" setup --refresh "$REFRESH"
if [ "$REFRESH" = manual ]; then
  echo "Refresh is manual: run 'smem update' when you want, or 'smem refresh auto' to automate it."
fi
