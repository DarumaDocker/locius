#!/bin/bash
# OMuse in one container: browser broker, agent runtime and Sentinel, the same three processes as the Olares pod.
# They talk over 127.0.0.1; only Sentinel (8080) and the phone port (8083) listen on all interfaces.
set -euo pipefail

if [ "${OMUSE_AUTH:-}" != "off" ] && [ -z "${OMUSE_PASSWORD:-}" ]; then
  echo "OMUSE_PASSWORD is not set. OMuse reads your mail and drives a browser: set a login password" >&2
  echo "(docker run -e OMUSE_PASSWORD=...), or OMUSE_AUTH=off if another proxy in front already does the login." >&2
  exit 64
fi

DATA="${OMUSE_DATA:-/omuse}"
export SENTINEL_DATA="$DATA/sentinel" RUNTIME_DATA="$DATA/runtime" WORKSPACE="$DATA/workspace" BROWSER_PROFILE="$DATA/browser-profile"
mkdir -p "$SENTINEL_DATA" "$RUNTIME_DATA" "$WORKSPACE" "$BROWSER_PROFILE"
chmod 700 "$SENTINEL_DATA"

# internal service tokens: fresh on every start, like the chart's randAlphaNum
RUNTIME_TOKEN="${RUNTIME_TOKEN:-$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')}"
BROWSER_TOKEN="${BROWSER_TOKEN:-$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')}"
export RUNTIME_TOKEN BROWSER_TOKEN
export SENTINEL_URL=http://127.0.0.1:8080 RUNTIME_URL=http://127.0.0.1:8081 BROWSER_URL=http://127.0.0.1:8082
export PYTHONUNBUFFERED=1 PYTHONPATH=/opt/omuse

cd /opt/omuse
python3 -m uvicorn app.browser.main:app --host 127.0.0.1 --port 8082 &
python3 -m uvicorn app.runtime.main:app --host 127.0.0.1 --port 8081 &
python3 -m uvicorn deploy.docker.gate:create --factory --host 0.0.0.0 --port 8080 \
  --proxy-headers --forwarded-allow-ips='*' --timeout-keep-alive 75 &

stop() { trap - TERM INT; kill -TERM $(jobs -p) 2>/dev/null || true; }
trap stop TERM INT
# one process gone = restart the whole container (docker --restart), the way the pod restarts a container
rc=0; wait -n || rc=$?
stop
wait || true
exit "$rc"
