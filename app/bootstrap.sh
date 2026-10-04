#!/bin/sh
# OMuse container entrypoint. Usage: bootstrap.sh sentinel|runtime|browser
set -e
ROLE="$1"
echo "[persona:$ROLE] unpacking bundle"
rm -rf /tmp/persona && mkdir -p /tmp/persona "${CACHE_DIR:-/cache}/pip"
# the bundle is xz-compressed (keeps the Helm release under Kubernetes' 1 MB Secret limit); python's tarfile
# auto-detects xz/gzip, so no xz binary is needed in the images
BD="${BUNDLE_DIR:-/bundle}"; CD="${CACHE_DIR:-/cache}"
if [ -f "$BD/app.tgz.b64" ]; then
  base64 -d "$BD/app.tgz.b64" > /tmp/persona-bundle
else
  # remote bundle (keeps the Helm release tiny): download once into /cache, verified against the pinned sha256
  BD="$BD" CD="$CD" python3 - <<'PYEOF' || exit 1
import hashlib, os, sys, time, urllib.request
bd, cd = os.environ["BD"], os.environ["CD"]
sha = open(f"{bd}/bundle.sha256").read().strip()
urls = [u.strip() for u in open(f"{bd}/bundle.urls") if u.strip()]
os.makedirs(f"{cd}/bundles", exist_ok=True)
dst = f"{cd}/bundles/{sha}.tar.xz"
def ok(p):
    try:
        return hashlib.sha256(open(p, "rb").read()).hexdigest() == sha
    except OSError:
        return False
if not ok(dst):
    for attempt in range(6):
        for u in urls:
            try:
                with urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "OMuse-bootstrap"}), timeout=90) as r:
                    data = r.read()
                if hashlib.sha256(data).hexdigest() != sha:
                    print(f"[persona] bundle from {u}: checksum mismatch", flush=True)
                    continue
                tmp = f"{dst}.{os.getpid()}"
                open(tmp, "wb").write(data)
                os.replace(tmp, dst)
                print(f"[persona] bundle downloaded from {u} ({len(data)} bytes)", flush=True)
                break
            except Exception as e:
                print(f"[persona] bundle download failed from {u}: {e}", flush=True)
        if ok(dst):
            break
        time.sleep(5 * (attempt + 1))
if not ok(dst):
    sys.exit("[persona] could not download the app bundle; check the network and restart the app")
PYEOF
  cp "$CD/bundles/$(cat "$BD/bundle.sha256").tar.xz" /tmp/persona-bundle
fi
python3 -c "import sys, tarfile; t = tarfile.open(sys.argv[1]); t.extractall(sys.argv[2], **({'filter': 'fully_trusted'} if hasattr(tarfile, 'fully_trusted_filter') else {}))" /tmp/persona-bundle /tmp/persona \
  || tar xf /tmp/persona-bundle -C /tmp/persona
rm -f /tmp/persona-bundle
# dev hot-patch: files placed in $PATCH_DIR/app/... override the bundle (used for quick fixes)
if [ -n "$PATCH_DIR" ] && [ -d "$PATCH_DIR/app" ]; then echo "[persona:$ROLE] applying patches from $PATCH_DIR"; cp -r "$PATCH_DIR/app/." /tmp/persona/app/; fi
if [ "$ROLE" = "browser" ]; then REQ=/tmp/persona/requirements-browser.txt; PYDIR="${CACHE_DIR:-/cache}/py-browser"; else REQ=/tmp/persona/requirements.txt; PYDIR="${CACHE_DIR:-/cache}/py"; fi
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
