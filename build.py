"""Build the Olares chart: bundle app code into the ConfigMap and package persona-<ver>.tgz."""
import base64, hashlib, io, os, re, shutil, subprocess, sys, tarfile

ROOT = os.path.dirname(os.path.abspath(__file__))
APP = (sys.argv[1] if len(sys.argv) > 1 else "persona").strip().lower()  # app id: persona (dev install) or locius (Market)
if not re.fullmatch(r"[a-z][a-z0-9]{1,29}", APP):
    sys.exit("app id must be lowercase letters/digits")
ver = re.search(r"VERSION = \"(.+?)\"", open(f"{ROOT}/app/common/util.py").read()).group(1)
buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode="w:gz") as tar:
    def filt(ti):
        if "__pycache__" in ti.name or ti.name.endswith(".pyc"):
            return None
        ti.uid = ti.gid = 0; ti.uname = ti.gname = "root"; ti.mtime = 0
        return ti
    tar.add(f"{ROOT}/app", arcname="app", filter=filt)
    tar.add(f"{ROOT}/requirements.txt", arcname="requirements.txt", filter=filt)
    tar.add(f"{ROOT}/requirements-browser.txt", arcname="requirements-browser.txt", filter=filt)
raw = buf.getvalue()
b64 = base64.b64encode(raw).decode()
lines = "\n".join("    " + b64[i:i + 76] for i in range(0, len(b64), 76))
boot = "\n".join("    " + l for l in open(f"{ROOT}/app/bootstrap.sh").read().splitlines())
tpl = open(f"{ROOT}/chart/deployment.tpl.yaml").read()
out = tpl.replace("__BOOTSTRAP__", boot).replace("__BUNDLE__", lines).replace("__HASH__", hashlib.sha256(raw).hexdigest()[:16])
if APP != "persona":
    out = out.replace("persona-voice", f"{APP}-voice")
    out = out.replace("persona-bundle", f"{APP}-bundle").replace(".Values.workloads.persona.", f".Values.workloads.{APP}.")
    out = re.sub(r"^(\s*(?:name|app): )persona$", rf"\g<1>{APP}", out, flags=re.M)
dist = f"{ROOT}/dist/{APP}"
shutil.rmtree(dist, ignore_errors=True); os.makedirs(f"{ROOT}/dist", exist_ok=True)
shutil.copytree(f"{ROOT}/chart/persona", dist)
for f in ("Chart.yaml", "OlaresManifest.yaml", "values.yaml"):
    p = f"{dist}/{f}"
    s = open(p).read()
    if APP != "persona":
        s = re.sub(r"^(\s*(?:-\s+)?(?:name|appid|host): )persona$", rf"\g<1>{APP}", s, flags=re.M)
        s = re.sub(r"^(\s+)persona:( 1)?$", rf"\g<1>{APP}:\g<2>", s, flags=re.M)
        s = s.replace("persona-voice", f"{APP}-voice")
        s = s.replace("persona-appdata", f"{APP}-appdata").replace("persona-appcache", f"{APP}-appcache")
    s = re.sub(r"^version: .*$", f"version: {ver}", s, flags=re.M)
    s = re.sub(r"^appVersion: .*$", f'appVersion: "{ver}"', s, flags=re.M)
    s = re.sub(r"^  version: '.*'$", f"  version: '{ver}'", s, flags=re.M)
    s = re.sub(r"^  versionName: '.*'$", f"  versionName: '{ver}'", s, flags=re.M)
    open(p, "w").write(s)
os.makedirs(f"{dist}/templates", exist_ok=True)
open(f"{dist}/templates/{APP}.yaml", "w").write(out)
tgz = f"{ROOT}/dist/{APP}-{ver}.tgz"
with tarfile.open(tgz, "w:gz") as tar:
    tar.add(dist, arcname=APP)
print(f"bundle {len(raw)} bytes (b64 {len(b64)}), chart {os.path.getsize(tgz)} bytes -> {tgz}")
