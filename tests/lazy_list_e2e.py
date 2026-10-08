"""2026-10-06 golden G04: decathlon.sg's search page showed "Loading..." where the results go; the snapshot was taken
before they arrived. The broker now waits a little for such a page."""
import sys
import time

import httpx

BR = "http://127.0.0.1:8082"
H = {"X-Browser-Token": "bt-test"}
c = httpx.Client(timeout=60, trust_env=False)
t0 = time.time()
r = c.post(BR + "/agent/navigate", json={"task_id": "task_lazy", "url": "http://shop.test:8099/lazylist.html"}, headers=H).json()
snap = r.get("snapshot") or c.post(BR + "/agent/snapshot", json={"task_id": "task_lazy"}, headers=H).json()["snapshot"]
dt = time.time() - t0
ok = "Kiprun socks" in snap and "Loading" not in snap
print(("PASS" if ok else "FAIL") + f" results that load late are in the snapshot ({dt:.1f}s)", "" if ok else snap[:500])
t0 = time.time()
c.post(BR + "/agent/navigate", json={"task_id": "task_lazy2", "url": "http://shop.test:8099/page.html"}, headers=H)
dt2 = time.time() - t0
ok2 = dt2 < 6
print(("PASS" if ok2 else "FAIL") + f" a normal page is not slowed down ({dt2:.1f}s)")
sys.exit(0 if ok and ok2 else 1)
