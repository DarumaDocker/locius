"""0.2.27: typing into <input type=time/date> converts free text to the input's format (R4-18: "12:00 PM" failed)."""
import sys

import httpx

BR = "http://127.0.0.1:8082"
HB = {"X-Browser-Token": "bt-test"}
c = httpx.Client(timeout=60, trust_env=False)
r = c.post(f"{BR}/agent/navigate", json={"task_id": "ft", "url": "http://shop.test:8099/form_time.html"}, headers=HB).json()
snap = str(r)
import re
refs = dict((m.group(2).strip(), m.group(1)) for m in re.finditer(r"\[(e\d+)\][^\n]*?\"([^\"]+)\"", snap))
print(refs)
fails = []
for label, text, want in (("Preferred delivery time", "12:00 PM", "12:00"), ("Date", "2026/10/3", "2026-10-03")):
    ref = next((v for k, v in refs.items() if label in k), None)
    r = c.post(f"{BR}/agent/type", json={"task_id": "ft", "ref": ref, "text": text}, headers=HB)
    val = c.post(f"{BR}/agent/eval_test", json={}, headers=HB) if False else None
    ok = r.status_code == 200 and want in r.text
    print(("PASS " if ok else "FAIL ") + label, r.status_code, r.text[:300] if not ok else "")
    if not ok:
        fails.append(label)
c.post(f"{BR}/agent/release", json={"task_id": "ft"}, headers=HB)
sys.exit(1 if fails else 0)
