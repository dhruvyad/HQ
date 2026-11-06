#!/bin/sh
# Install HQ, a local inbox and floating pill for your AI agents.
#
#   curl -fsSL https://raw.githubusercontent.com/dhruvyad/HQ/main/install.sh | sh
#
# Run it again to update. From a clone, ./install.sh installs that clone instead.
# HQ_NO_SETUP=1 installs the command only; HQ_REF picks a branch or tag.
# It installs the hq command with uv, in its own environment with its own Python, then
# runs hq setup, which shows each change before making it. hq setup --undo reverses it.
set -eu

REPO="dhruvyad/HQ"
REF="${HQ_REF:-main}"

say() { printf '%s\n' "$*"; }
die() { printf 'HQ: %s\n' "$*" >&2; exit 1; }

case "$(uname -s)" in
  Darwin) ;;
  Linux) say "On Linux you get the dashboard; the floating pill is macOS only." ;;
  *) die "HQ runs on macOS, or on Linux without the pill." ;;
esac

if ! command -v uv >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    say "Installing uv, which keeps HQ and its Python apart from yours…"
    brew install uv
  else
    say "Installing uv (docs.astral.sh/uv), which keeps HQ and its Python apart from yours…"
    curl -LsSf https://astral.sh/uv/install.sh | sh
  fi
  PATH="$HOME/.local/bin:$PATH"
fi

# Piped from curl, $0 is the shell, so only a script run from a clone installs that clone.
source="https://github.com/$REPO/archive/$REF.tar.gz"
case "$0" in
  *install.sh)
    here="$(cd "$(dirname "$0")" && pwd)"
    if [ -f "$here/pyproject.toml" ] && [ -f "$here/hq/cli.py" ]; then source="$here"; fi ;;
esac
source="${HQ_SOURCE:-$source}"

# Braces matter: macOS's sh, in a UTF-8 locale, reads "…" after $source as part of its name.
say "Installing hq from ${source}…"
if ! uv tool install --quiet --reinstall --python 3.12 --from "$source" hq-local; then
  case "$source" in
    https://*) die "Could not download HQ. If you were given access to the private repository:
  gh repo clone $REPO && ./HQ/install.sh" ;;
    *) die "Could not install HQ from $source" ;;
  esac
fi

bin="$(uv tool dir --bin)"
case ":$PATH:" in
  *":$bin:"*) ;;
  *) uv tool update-shell >/dev/null 2>&1 || true
     say "Added $bin to your PATH for new terminals." ;;
esac

if [ -n "${HQ_NO_SETUP:-}" ]; then
  say "Installed. Run hq setup when you are ready."
  exit 0
fi

if [ "$(uname -s)" = Darwin ] && ! xcode-select -p >/dev/null 2>&1; then
  say "The pill needs Apple's command line tools. A window will offer to install them;"
  say "when that finishes, run: hq setup"
  xcode-select --install >/dev/null 2>&1 || true
fi

say ""
exec "$bin/hq" setup
