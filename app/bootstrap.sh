#!/bin/sh
# Locius container entrypoint. Usage: bootstrap.sh sentinel|runtime|browser
set -e
ROLE="$1"
echo "[persona:$ROLE] unpacking bundle"
rm -rf /tmp/persona && mkdir -p /tmp/persona /cache/pip
base64 -d /bundle/app.tgz.b64 | tar xz -C /tmp/persona
# dev hot-patch: files placed in $PATCH_DIR/app/... override the bundle (used for quick fixes)
if [ -n "$PATCH_DIR" ] && [ -d "$PATCH_DIR/app" ]; then echo "[persona:$ROLE] applying patches from $PATCH_DIR"; cp -r "$PATCH_DIR/app/." /tmp/persona/app/; fi
if [ "$ROLE" = "browser" ]; then REQ=/tmp/persona/requirements-browser.txt; PYDIR=/cache/py-browser; else REQ=/tmp/persona/requirements.txt; PYDIR=/cache/py; fi
REQ_HASH="$(md5sum $REQ | cut -c1-12)"
mkdir -p "$PYDIR"
LOCK="$PYDIR/.lock"
# the two python:3.12-slim containers share $PYDIR; install once
i=0
while ! mkdir "$LOCK" 2>/dev/null; do i=$((i+1)); if [ $i -gt 300 ]; then rm -rf "$LOCK"; fi; sleep 2; done
if [ ! -f "$PYDIR/.ok-$REQ_HASH" ]; then
  echo "[persona:$ROLE] installing python deps ($REQ_HASH)"
  rm -rf "$PYDIR"/* "$PYDIR"/.ok-* 2>/dev/null || true
  PIP_CACHE_DIR=/cache/pip pip install --no-warn-script-location --disable-pip-version-check --target "$PYDIR" -r "$REQ" || { rmdir "$LOCK"; exit 1; }
  touch "$PYDIR/.ok-$REQ_HASH"
fi
rmdir "$LOCK" 2>/dev/null || true
export PYTHONPATH="/tmp/persona:$PYDIR" PYTHONUNBUFFERED=1
cd /tmp/persona
case "$ROLE" in
  sentinel) exec python3 -m uvicorn app.sentinel.main:app --host 0.0.0.0 --port 8080 --proxy-headers --forwarded-allow-ips='*' --timeout-keep-alive 75 ;;
  runtime)  exec python3 -m uvicorn app.runtime.main:app --host 127.0.0.1 --port 8081 ;;
  browser)
    python3 -c "import playwright" 2>/dev/null || pip install --target "$PYDIR" playwright==1.56.0
    exec python3 -m uvicorn app.browser.main:app --host 127.0.0.1 --port 8082 ;;
esac
