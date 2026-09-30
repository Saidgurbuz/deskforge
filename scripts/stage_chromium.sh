#!/usr/bin/env bash
# Make the extracted Chromium runnable, so the browser tests can use it.
#
#   scripts/stage_chromium.sh            # prints the path it staged
#   DESKSHOT_CHROMIUM=$(scripts/stage_chromium.sh) PYTHONPATH=src python -m pytest \
#       tests/test_audit_browser.py -q
#
# `tools/extracted/.../chromium-browser` ships mode 664 and is owned by root, so
# it cannot be chmod-ed in place and cannot be exec-ed. The generation pipeline
# solves this by staging a copy under `/tmp/deskshot_bin_fix`, which is a shared
# flock-guarded cache this script deliberately does not touch. Instead it builds
# its own directory of symlinks to the resource files - the .pak, .dat and .so
# next to the binary, which Chromium resolves relative to the executable - and
# copies only the three files that need the execute bit.
#
# On /proj rather than /tmp: /tmp has a per-user quota here, and 305MB of it is
# not a good way to spend it.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$REPO/tools/extracted/usr/lib64/chromium-browser"
STAGE="${DESKSHOT_CHROMIUM_STAGE:-${TMPDIR:-/tmp}/deskshot_chromium}"
NEEDS_EXEC=(chromium-browser chrome_crashpad_handler chrome-sandbox)

if [ ! -d "$SRC" ]; then
  echo "no extracted chromium at $SRC" >&2
  exit 1
fi

mkdir -p "$STAGE"
for path in "$SRC"/*; do
  name="$(basename "$path")"
  skip=""
  for exe in "${NEEDS_EXEC[@]}"; do
    [ "$name" = "$exe" ] && skip=1
  done
  [ -n "$skip" ] && continue
  [ -e "$STAGE/$name" ] || ln -s "$path" "$STAGE/$name"
done
for exe in "${NEEDS_EXEC[@]}"; do
  if [ -f "$SRC/$exe" ] && [ ! -x "$STAGE/$exe" ]; then
    cp "$SRC/$exe" "$STAGE/$exe"
    chmod +x "$STAGE/$exe"
  fi
done

# The extracted tree is not on the loader path and chromium pulls in enough of
# it that the list is worth pinning; it is the same one tests/ uses.
LIBS=""
for dir in usr/lib64 usr/lib64/samba usr/lib64/samba/wbclient \
           usr/lib64/chromium-browser lib64 usr/lib; do
  [ -d "$REPO/tools/extracted/$dir" ] && LIBS="$LIBS:$REPO/tools/extracted/$dir"
done
export LD_LIBRARY_PATH="${LIBS#:}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

if ! "$STAGE/chromium-browser" --version >/dev/null 2>&1; then
  echo "staged copy at $STAGE will not run; check LD_LIBRARY_PATH" >&2
  exit 1
fi
echo "$STAGE/chromium-browser"
