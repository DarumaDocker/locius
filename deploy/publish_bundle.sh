#!/bin/sh
# Publish an OMuse app bundle (dist/bundles/<app>-<version>-<sha12>.tar.xz from build.py) to the `bundles` branch of the
# public repo, where the chart downloads it from (raw.githubusercontent.com / jsDelivr / github.com). Run this BEFORE
# installing or upgrading the chart. Usage: publish_bundle.sh <bundle-file> [owner/repo]
set -e
F="$1"; REPO="${2:-Drlucaslu/locius}"
[ -f "$F" ] || { echo "no bundle file: $F"; exit 1; }
NAME=$(basename "$F")
W="${BUNDLE_WORKTREE:-/root/px/bundles-wt}"
if [ ! -d "$W/.git" ]; then
  rm -rf "$W"; mkdir -p "$W"; cd "$W"
  git init -q -b bundles
  git remote add origin "https://github.com/$REPO.git"
fi
cd "$W"
if git fetch -q --depth 1 origin bundles 2>/dev/null; then git reset -q --hard FETCH_HEAD; fi
[ -f README.md ] || printf '%s\n' "# OMuse app bundles" "" "Code bundles downloaded by the OMuse Olares app at startup (checked against the sha256 pinned in the chart)." "Built from the main branch by build.py; nothing else lives on this branch." > README.md
cp "$F" "$NAME"
git add README.md "$NAME"
git -c user.name="${GIT_NAME:-Drlucaslu}" -c user.email="${GIT_EMAIL:-sixwings@gmail.com}" commit -qm "bundle $NAME" || true
git push -q origin HEAD:bundles
SHA=$(sha256sum "$F" | cut -d' ' -f1)
i=0
while [ $i -lt 12 ]; do
  if curl -sfL "https://raw.githubusercontent.com/$REPO/bundles/$NAME" | sha256sum | grep -q "$SHA"; then echo "BUNDLE_PUBLISHED $NAME"; exit 0; fi
  i=$((i+1)); sleep 5
done
echo "BUNDLE_NOT_REACHABLE $NAME"; exit 1
