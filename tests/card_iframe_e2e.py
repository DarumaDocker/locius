"""Card fields in separate iframes after many other frames (2026-10-05 decathlon.sg / Adyen): the snapshot lists every
card box, and typing is key by key (Adyen ignores a value set at once)."""
import re
import sys

import httpx

BR = "http://127.0.0.1:8082"
H = {"X-Browser-Token": "bt-test"}
c = httpx.Client(timeout=60, trust_env=False)
fails = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name, "" if cond else str(info)[:1500])
    if not cond:
        fails.append(name)


T = "task_cardframes"
r = c.post(BR + "/agent/navigate", json={"task_id": T, "url": "http://shop.test:8099/cardframes.html"}, headers=H).json()
snap = c.post(BR + "/agent/snapshot", json={"task_id": T}, headers=H).json()["snapshot"]
refs = {k: re.search(r"\[(f\d+e\d+)\][^\n]*" + k, snap) for k in ("Card number", "Expiry date", "Security code")}
check("all three card boxes are in the snapshot with their own iframe refs", all(refs.values()) and
      len({m.group(1)[:3] for m in refs.values() if m}) == 3, snap[-1500:])
if all(refs.values()):
    for k, val, extra in (("Card number", "4111111111111111", {}), ("Expiry date", "12/2028", {"expiry": True}), ("Security code", "737", {})):
        x = c.post(BR + "/agent/type", json={"task_id": T, "ref": refs[k].group(1), "text": val, "keys": True, **extra}, headers=H)
        check(f"typed {k}", x.status_code == 200, x.text[:300])
    b = re.search(r"\[(e\d+)\] button \"Pay\"", c.post(BR + "/agent/snapshot", json={"task_id": T}, headers=H).json()["snapshot"])
    c.post(BR + "/agent/click", json={"task_id": T, "ref": b.group(1)}, headers=H)
    res = c.post(BR + "/agent/page_text", json={"task_id": T}, headers=H).json()["text"]
    check("the card iframes accepted the key-by-key values (expiry as MM/YY)", "yes:4111111111111111,yes:12/28,yes:737" in res, res[-300:])
print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
