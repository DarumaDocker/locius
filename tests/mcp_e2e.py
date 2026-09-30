"""End-to-end: MCP connector framework against the local stack + tests/fake_mcp.py (port 8093)."""
import sys, time, httpx
B, M = "http://127.0.0.1:8080", "http://127.0.0.1:8093"
H = {"X-Persona-UI": "1"}
RT = {"X-Persona-Runtime": "rt-test"}
c = httpx.Client(timeout=60, trust_env=False)
fails = []
def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name, "" if cond else str(info)[:500]); (None if cond else fails.append(name))
def cat_names(): return [t["function"]["name"] for t in c.get(B + "/internal/catalog", headers=RT).json()["tools"]]
def act(tool, args, task="mcp-t1"):
    return c.post(B + "/internal/act", headers=RT, json={"task_id": task, "call_id": "c" + str(time.time()), "tool": tool, "args": args}).json()
def wait_for(pred, timeout=40):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred(): return True
        time.sleep(0.5)
    return False

c.post(M + "/_mutate", json={"reset": True})
# --- add: wrong token, public http, then OK
r = c.post(B + "/sentinel/api/mcp/servers", headers=H, json={"name": "Notes", "url": M + "/mcp", "auth_type": "bearer", "token": "wrong"})
check("wrong token rejected with auth message", r.status_code == 400 and "认证失败" in r.text, r.text)
r = c.post(B + "/sentinel/api/mcp/servers", headers=H, json={"name": "X", "url": "http://example.com/mcp"})
check("plain http to public host rejected", r.status_code == 400 and "https" in r.text, r.text)
r = c.post(B + "/sentinel/api/mcp/servers", headers=H, json={"name": "Notes", "url": M + "/mcp", "auth_type": "bearer", "token": "test-mcp-token"})
check("server added (streamable http)", r.status_code == 200 and r.json()["transport"] == "streamable_http", r.text)
srv = r.json() if r.status_code == 200 else {"tools": []}
tools = {t["name"]: t for t in srv["tools"]}
check("pagination: all 5 tools listed", len(tools) == 5, list(tools))
check("read-only tool -> auto", tools.get("notes_search", {}).get("mode") == "auto" and tools["notes_search"]["kind"] == "read")
check("write tool -> ask", tools.get("notes_create", {}).get("mode") == "ask" and tools["notes_create"]["kind"] == "write")
check("destructive tool -> ask", tools.get("notes_delete", {}).get("kind") == "destructive" and tools["notes_delete"]["mode"] == "ask")
check("unannotated get_* guessed read", tools.get("get_weather", {}).get("kind") == "read" and tools["get_weather"]["guessed"])
check("poisoned tool description flagged + off", tools.get("helper", {}).get("mode") == "off" and tools["helper"]["flags"], tools.get("helper"))
check("token never returned", "test-mcp-token" not in r.text)
names = cat_names()
check("catalog publishes enabled tools", "mcp_notes__notes_search" in names and "mcp_notes__notes_create" in names, names)
check("catalog hides poisoned tool", "mcp_notes__helper" not in names)
cat = c.get(B + "/internal/catalog", headers=RT).json()
check("catalog status lists MCP server", cat["connections"]["mcp"]["servers"][0]["prefix"] == "mcp_notes__", cat["connections"].get("mcp"))

# --- calls
res = act("mcp_notes__notes_search", {"query": "q3"})
check("read tool runs without approval (SSE response parsed)", res.get("status") == "ok" and "OMuse 0.2" in str(res), res)
check("result wrapped as untrusted", res.get("result", {}).get("trust") == "untrusted")
res = act("mcp_notes__notes_create", {"title": "t", "text": "x"})
check("write tool needs approval", res.get("status") == "approval_required", res)
aid = res.get("approval_id")
check("approval summary names server + tool", "MCP · Notes" in str(res.get("summary", {}).get("title")), res.get("summary"))
r = c.post(B + f"/sentinel/api/approvals/{aid}/resolve", headers=H, json={"decision": "approve", "scope": "ONCE"})
check("approved write executed on MCP server", r.json().get("status") == "approved" and any(x["name"] == "notes_create" for x in c.get(M + "/_log").json()["calls"]), r.text)
c.put(B + "/sentinel/api/mcp/servers/notes/tools/notes_create", headers=H, json={"mode": "auto"})
res = act("mcp_notes__notes_create", {"title": "t2"}, task="mcp-t2")
check("write tool set to auto runs directly", res.get("status") == "ok", res)
c.put(B + "/sentinel/api/mcp/servers/notes/tools/notes_search", headers=H, json={"mode": "off"})
check("tool set off -> hidden + denied", "mcp_notes__notes_search" not in cat_names() and act("mcp_notes__notes_search", {"query": "a"}).get("status") == "denied")
c.put(B + "/sentinel/api/mcp/servers/notes/tools/notes_search", headers=H, json={"mode": "auto"})

# --- injection in results escalates writes
res = act("mcp_notes__notes_search", {"query": "inject"}, task="mcp-t3")
check("injection in MCP result flagged", "injection_warning" in str(res), res)
res = act("mcp_notes__notes_create", {"title": "t3"}, task="mcp-t3")
check("after injection, auto write requires approval", res.get("status") == "approval_required", res)

# --- rug pull + new tool
c.post(M + "/_mutate", json={"rug": True, "new_tool": True})
r = c.post(B + "/sentinel/api/mcp/servers/notes/refresh", headers=H)
d = r.json().get("diff", {})
check("refresh detects changed + added tools", d.get("changed") == ["notes_search"] and d.get("added") == ["notes_export"], r.text)
names = cat_names()
check("changed & new tools hidden until reviewed", "mcp_notes__notes_search" not in names and "mcp_notes__notes_export" not in names, names)
check("changed tool call denied", act("mcp_notes__notes_search", {"query": "a"}).get("status") == "denied")
c.put(B + "/sentinel/api/mcp/servers/notes/tools/notes_search", headers=H, json={"accept": True})
c.put(B + "/sentinel/api/mcp/servers/notes/tools/notes_export", headers=H, json={"mode": "auto"})
names = cat_names()
check("accepted/enabled tools back in catalog", "mcp_notes__notes_search" in names and "mcp_notes__notes_export" in names, names)
c.post(M + "/_mutate", json={"reset": True})
c.post(B + "/sentinel/api/mcp/servers/notes/refresh", headers=H)
c.put(B + "/sentinel/api/mcp/servers/notes/tools/notes_search", headers=H, json={"accept": True})

# --- legacy SSE transport
r = c.post(B + "/sentinel/api/mcp/servers", headers=H, json={"name": "Old Notes", "url": M + "/sse"})
check("legacy SSE server added", r.status_code == 200 and r.json()["transport"] == "sse", r.text)
res = act("mcp_old_notes__notes_search", {"query": "groceries"})
check("legacy SSE call works", res.get("status") == "ok" and "milk" in str(res), res)
check("ping test works", c.post(B + "/sentinel/api/mcp/servers/old_notes/test", headers=H).json().get("ok"))

# --- agent loop through the runtime
c.put(B + "/sentinel/api/mcp/servers/notes/tools/notes_create", headers=H, json={"mode": "ask"})
r = c.post(B + "/api/chat", headers=H, json={"message": "MCPNOTES 帮我查笔记并写总结"})
tid = r.json().get("task_id")
def task(): return c.get(B + f"/api/tasks/{tid}", headers=H).json()
ok = wait_for(lambda: (task().get("task") or task()).get("status") in ("WAITING_APPROVAL", "COMPLETED", "FAILED"), 60)
t = task(); t = t.get("task") or t
check("agent called MCP read tool then paused for write approval", t.get("status") == "WAITING_APPROVAL", str(t)[:600])
pend = [a for a in c.get(B + "/sentinel/api/approvals?status=pending", headers=H).json()["approvals"] if a["task_id"] == tid]
if pend:
    c.post(B + f"/sentinel/api/approvals/{pend[0]['id']}/resolve", headers=H, json={"decision": "approve"})
ok = wait_for(lambda: (task().get("task") or task()).get("status") == "COMPLETED", 60)
t = task(); t = t.get("task") or t
check("agent finished after approval", ok and "MCP done" in str(t.get("result")), str(t)[:600])

# --- disable / delete
c.put(B + "/sentinel/api/mcp/servers/old_notes", headers=H, json={"enabled": False})
check("disabled server hidden", not any(n.startswith("mcp_old_notes__") for n in cat_names()))
c.delete(B + "/sentinel/api/mcp/servers/old_notes", headers=H)
c.delete(B + "/sentinel/api/mcp/servers/notes", headers=H)
check("servers removed", c.get(B + "/sentinel/api/mcp/servers", headers=H).json()["servers"] == [] and not any(n.startswith("mcp_") for n in cat_names()))
aud = c.get(B + "/sentinel/api/audit?limit=300", headers=H).json()
check("audit has mcp.add / mcp.refresh / mcp tool calls", all(any(e["action"] == a for e in aud["events"]) for a in ("mcp.add", "mcp.refresh", "mcp_notes__notes_search")), [e["action"] for e in aud["events"]][:30])
print(f"\n{'ALL PASS' if not fails else 'FAILED: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)
