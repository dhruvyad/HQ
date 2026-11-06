#!/bin/sh
# Install HQ, a local inbox and floating pill for your AI agents.
#
#   curl -fsSL https://raw.githubusercontent.com/dhruvyad/HQ/main/install.sh | sh
#
# Run it again, or hq update, to update. From a clone, ./install.sh installs that clone instead.
# HQ_NO_SETUP=1 installs the command only; HQ_REF picks a branch, tag or commit.
# It installs the hq command with uv, in its own environment with its own Python, then
# runs hq setup, which shows each change before making it. hq setup --undo reverses it.
# It records which build it installed in install.json, in HQ's data folder, for hq --version.
set -eu

REPO="dhruvyad/HQ"
REF="${HQ_REF:-main}"

say() { printf '%s\n' "$*"; }
die() { printf 'HQ: %s\n' "$*" >&2; exit 1; }

# Everything runs from main, called on the last line, so a download cut short runs nothing.
main() {
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

# A Mac without the command line tools has a git that only offers to install them.
have_git() {
  command -v git >/dev/null 2>&1 || return 1
  [ "$(uname -s)" != Darwin ] || [ "$(command -v git)" != /usr/bin/git ] || xcode-select -p >/dev/null 2>&1
}

is_commit() {
  case "$1" in *[!0-9a-f]*) return 1 ;; esac
  [ "${#1}" -eq 40 ]
}

# The commit $REF names on GitHub right now, or nothing if it cannot be found.
resolve() {
  if is_commit "$REF"; then say "$REF"; return 0; fi
  found=""
  if have_git; then
    found="$(GIT_TERMINAL_PROMPT=0 git ls-remote "https://github.com/$REPO" \
      "refs/heads/$REF" "refs/tags/$REF" "refs/tags/$REF^{}" 2>/dev/null | awk -v ref="$REF" '
        $2 == "refs/heads/" ref { head = $1 }
        $2 == "refs/tags/" ref { tag = $1 }
        $2 == "refs/tags/" ref "^{}" { peeled = $1 }
        END { if (head != "") print head; else if (peeled != "") print peeled; else print tag }')" || found=""
  fi
  if ! is_commit "$found"; then
    found="$(curl -fsS --max-time 20 -H 'Accept: application/vnd.github.sha' \
      "https://api.github.com/repos/$REPO/commits/$REF" 2>/dev/null)" || found=""
  fi
  if is_commit "$found"; then say "$found"; fi
  return 0
}

# Piped from curl, $0 is the shell, so only a script run from a clone installs that clone.
clone=""
case "$0" in
  *install.sh)
    here="$(cd "$(dirname "$0")" && pwd)"
    if [ -f "$here/pyproject.toml" ] && [ -f "$here/hq/cli.py" ]; then clone="$here"; fi ;;
esac
source="${HQ_SOURCE:-$clone}"
commit="" ref="$REF" dirty=false
if [ -z "$source" ]; then
  # Install the commit itself, not the branch, so the code and the build recorded match.
  commit="$(resolve)"
  source="https://github.com/$REPO/archive/${commit:-$REF}.tar.gz"
elif [ -d "$source" ]; then
  source="$(cd "$source" && pwd)"
  if [ -e "$source/.git" ] && have_git; then
    commit="$(git -C "$source" rev-parse HEAD 2>/dev/null)" || commit=""
    ref="$(git -C "$source" rev-parse --abbrev-ref HEAD 2>/dev/null)" || ref=""
    if [ -n "$(git --no-optional-locks -C "$source" status --porcelain --untracked-files=no 2>/dev/null)" ]; then
      dirty=true
    fi
  fi
fi

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
tools="$(uv tool dir)"

# Which build this is, for hq --version, hq doctor and hq update. Kept beside your inbox,
# which this never opens.
json() { printf '"%s"' "$(printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g')"; }
data="${HQ_HOME:-$HOME/.local/share/hq}"
if ! (umask 077 && mkdir -p "$data" &&
  printf '{"commit": %s, "dirty": %s, "ref": %s, "source": %s, "installed_at": %s, "hq": %s, "env": %s}\n' \
    "$(json "$commit")" "$dirty" "$(json "$ref")" "$(json "$source")" \
    "$(json "$(date -u +%Y-%m-%dT%H:%M:%SZ)")" "$(json "$bin/hq")" "$(json "$tools/hq-local")" \
    > "$data/install.json.partial" &&
  mv -f "$data/install.json.partial" "$data/install.json"); then
  say "Installed, but could not record which build in $data"
fi

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
}

main "$@"
