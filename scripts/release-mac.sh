#!/usr/bin/env bash
# Build penrose's Mac installer on this Mac and add it to a GitHub release the release workflow already made.
#
#   scripts/release-mac.sh              # the newest release, draft or published
#   scripts/release-mac.sh v2026.1124.1947
#
# For when GitHub has no Mac machine free and the workflow's macOS build was cancelled. The build uses the release's
# own commit and build ID, so its version and update feed match the Windows and Linux installers already there.
# It builds in a fresh copy of the repository in a temporary folder: your own checkout is not touched.
#
# Needs, once: Xcode's command-line tools (xcode-select --install), Node 22, Python 3.14 and the GitHub CLI signed in
# to an account that can edit the repository's releases. With Homebrew: brew install node@22 python@3.14 gh, then
# gh auth login.
set -euo pipefail

REPO="${PENROSE_REPO:-penroseailabs/penrose}"
TAG="${1:-}"

say() { printf '\n== %s\n' "$*"; }
fail() { printf 'error: %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = Darwin ] || fail "run this on a Mac"
[ "$(uname -m)" = arm64 ] || fail "run this on an Apple Silicon Mac (the release is for Apple Silicon)"
command -v gh > /dev/null || fail "the GitHub CLI is missing: brew install gh, then gh auth login"
gh auth status > /dev/null 2>&1 || fail "the GitHub CLI is not signed in: gh auth login"
# Homebrew keeps node@22 off the PATH (it is "keg-only"): use it when it is there, the version the release builds use.
for dir in /opt/homebrew/opt/node@22/bin /usr/local/opt/node@22/bin; do
  [ -x "$dir/node" ] && { PATH="$dir:$PATH"; break; }
done
command -v node > /dev/null || fail "Node is missing: brew install node@22"
NODE_MAJOR=$(node -p 'process.versions.node.split(".")[0]' 2> /dev/null || true)
[ -n "$NODE_MAJOR" ] || fail "node is installed but does not run (try: brew install node@22, then run this again)"
[ "$NODE_MAJOR" -ge 22 ] || fail "Node 22 or newer is needed (node --version says $(node --version))"
PYTHON="${RELAY_PYTHON:-$(command -v python3.14 || true)}"
[ -n "$PYTHON" ] || fail "Python 3.14 is missing: brew install python@3.14"

if [ -z "$TAG" ]; then
  TAG=$(gh api "repos/$REPO/releases" --jq '.[0].tag_name')
  [ -n "$TAG" ] || fail "no release found in $REPO"
fi
field() { gh api "repos/$REPO/releases" --jq ".[] | select(.tag_name == \"$TAG\") | $1"; }
COMMIT=$(field .target_commitish)
[ -n "$COMMIT" ] || fail "no release $TAG in $REPO"
BUILD_ID=$(field .body | grep -oE 'Build [0-9]{14}' | grep -oE '[0-9]{14}' | head -1 || true)
[ -n "$BUILD_ID" ] || fail "the notes of $TAG do not say its build ID (\"Build yyyymmddhhmmss.\")"
VERSION="${TAG#v}"
say "penrose $VERSION, build $BUILD_ID, commit ${COMMIT:0:7}"

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
say "Fetching the source at ${COMMIT:0:7}"
git clone --quiet --filter=blob:none "https://github.com/$REPO.git" "$WORK/penrose"
git -C "$WORK/penrose" checkout --quiet "$COMMIT"
cd "$WORK/penrose"

say "Installing what the build needs"
"$PYTHON" -m venv .venv
npm --prefix desktop ci --no-audit --no-fund
npm --prefix frontend ci --no-audit --no-fund

say "Building (about ten minutes)"
(cd desktop && RELAY_BUILD_ID="$BUILD_ID" RELAY_PYTHON="$PWD/../.venv/bin/python" npm run dist)

FEED=desktop/dist/latest-mac.yml
[ -f "$FEED" ] || fail "the build made no $FEED"
BUILT=$(sed -n 's/^version: //p' "$FEED")
[ "$BUILT" = "$VERSION" ] || fail "built version $BUILT, but the release is $VERSION"

say "Adding it to $TAG"
gh release upload "$TAG" desktop/dist/*.dmg desktop/dist/*.zip desktop/dist/*.blockmap "$FEED" --repo "$REPO" --clobber

# The notes list each platform the release now holds, as the release workflow writes them.
ASSETS=$(field '.assets[].name')
NEW="Build $BUILD_ID."
MISSING=""
if echo "$ASSETS" | grep -qx 'latest.yml'; then NEW="$NEW
- **Windows:** run the \`Setup .exe\`."; else MISSING="$MISSING Windows"; fi
if echo "$ASSETS" | grep -qx 'latest-mac.yml'; then NEW="$NEW
- **macOS (Apple Silicon):** open the \`.dmg\` and drag penrose into Applications. If your Mac says it can't verify penrose, run \`xattr -cr /Applications/penrose.app\` in Terminal, then open it again."; else MISSING="$MISSING macOS"; fi
if echo "$ASSETS" | grep -qx 'latest-linux.yml'; then NEW="$NEW
- **Linux (x64, preview):** make the \`.AppImage\` executable and run it."; else MISSING="$MISSING Linux"; fi
[ -z "$MISSING" ] || NEW="$NEW

Not in this release yet:$MISSING."
gh release edit "$TAG" --repo "$REPO" --notes "$NEW"

say "Done: the Mac installer is in $TAG"
echo "$ASSETS" | sed 's/^/  /'
