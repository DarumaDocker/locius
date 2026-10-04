"""Build the Olares chart: bundle app code into the ConfigMap and package persona-<ver>.tgz."""
import base64, hashlib, io, lzma, os, re, shutil, subprocess, sys, tarfile

ROOT = os.path.dirname(os.path.abspath(__file__))
APP = (sys.argv[1] if len(sys.argv) > 1 else "omuse").strip().lower()  # app id: omuse (current install); persona = the old pre-2026-10 install
if not re.fullmatch(r"[a-z][a-z0-9]{1,29}", APP):
    sys.exit("app id must be lowercase letters/digits")
ver = re.search(r"VERSION = \"(.+?)\"", open(f"{ROOT}/app/common/util.py").read()).group(1)
buf = io.BytesIO()
# xz, not gzip: the bundle sits in the Helm release twice (chart + rendered manifest) and the release Secret
# must stay under Kubernetes' 1 MB limit; gzip had 0.2.20 at ~1.06 MB, xz brings it to ~0.87 MB
with tarfile.open(fileobj=buf, mode="w:xz", preset=9 | lzma.PRESET_EXTREME) as tar:
    def filt(ti):
        if "__pycache__" in ti.name or ti.name.endswith(".pyc"):
            return None
        ti.uid = ti.gid = 0; ti.uname = ti.gname = "root"; ti.mtime = 0
        return ti
    import ast

    def _strip_docstrings(src: str) -> str:
        """Drop docstrings and comments from the shipped .py files (ast round trip): ~20 KB less in the Helm release."""
        tree = ast.parse(src)
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)) and n.body \
                    and isinstance(n.body[0], ast.Expr) and isinstance(getattr(n.body[0], "value", None), ast.Constant) \
                    and isinstance(n.body[0].value.value, str):
                n.body = n.body[1:] or [ast.Pass()]
        out = ast.unparse(tree) + "\n"
        # ast.unparse writes every string literal on one line, so leading indentation is pure code: 1 space per level
        return re.sub(r"(?m)^((?:    )+)", lambda m: " " * (len(m.group(1)) // 4), out)

    def _strip_js(src: str) -> str:
        """Drop indentation and whole-line // comments (no multi-line template literals in our JS; checked)."""
        if any(l.count("`") % 2 for l in src.split("\n")):
            return src
        return "\n".join(l.strip() for l in src.split("\n") if l.strip() and not l.strip().startswith("//")) + "\n"

    for r, ds, fs in os.walk(f"{ROOT}/app"):
        ds[:] = sorted(d for d in ds if d != "__pycache__")
        for f in sorted(fs):
            if f.endswith(".pyc"):
                continue
            p = os.path.join(r, f)
            data = open(p, "rb").read()
            if f.endswith(".py") and os.environ.get("KEEP_DOCSTRINGS") != "1":
                data = _strip_docstrings(data.decode()).encode()
                compile(data, p, "exec")
            elif f in ("app.js", "app.css") and os.environ.get("KEEP_DOCSTRINGS") != "1":
                data = _strip_js(data.decode()).encode()
            ti = tarfile.TarInfo(os.path.relpath(p, ROOT))
            ti.size, ti.mode, ti.mtime = len(data), os.stat(p).st_mode & 0o777, 0
            ti.uid = ti.gid = 0; ti.uname = ti.gname = "root"
            tar.addfile(ti, io.BytesIO(data))
    tar.add(f"{ROOT}/requirements.txt", arcname="requirements.txt", filter=filt)
    tar.add(f"{ROOT}/requirements-browser.txt", arcname="requirements-browser.txt", filter=filt)
raw = buf.getvalue()
b64 = base64.b64encode(raw).decode()
# Bundle delivery. "remote" (default): the bundle is published as dist/bundles/<file> (pushed to the repo's `bundles`
# branch by the deploy script) and the chart only pins its sha256 + download URLs, so the Helm release stays tiny.
# "embed": the old way, the whole bundle inside the ConfigMap (Helm release ~1 MB).
MODE = os.environ.get("BUNDLE_MODE", "remote")
REPO = os.environ.get("BUNDLE_REPO", "Drlucaslu/locius")
sha = hashlib.sha256(raw).hexdigest()
if MODE == "embed":
    lines = "  app.tgz.b64: |\n" + "\n".join("    " + b64[i:i + 76] for i in range(0, len(b64), 76))
else:
    bname = f"{APP}-{ver}-{sha[:12]}.tar.xz"
    os.makedirs(f"{ROOT}/dist/bundles", exist_ok=True)
    open(f"{ROOT}/dist/bundles/{bname}", "wb").write(raw)
    urls = [u.replace("{repo}", REPO).replace("{file}", bname) for u in os.environ.get("BUNDLE_URLS", "").split(",") if u] or [
        f"https://raw.githubusercontent.com/{REPO}/bundles/{bname}",
        f"https://cdn.jsdelivr.net/gh/{REPO}@bundles/{bname}",
        f"https://github.com/{REPO}/raw/bundles/{bname}"]
    lines = f"  bundle.sha256: \"{sha}\"\n  bundle.urls: |\n" + "\n".join("    " + u for u in urls)
    print(f"bundle file dist/bundles/{bname} -> push it to the `bundles` branch of {REPO} before installing")
boot = "\n".join("    " + l for l in open(f"{ROOT}/app/bootstrap.sh").read().splitlines())
tpl = open(f"{ROOT}/chart/deployment.tpl.yaml").read()
out = tpl.replace("__BOOTSTRAP__", boot).replace("__BUNDLE__", lines).replace("__HASH__", hashlib.sha256(raw).hexdigest()[:16])
if APP != "persona":
    out = out.replace("persona-voice", f"{APP}-voice")
    out = out.replace("persona-bundle", f"{APP}-bundle").replace(".Values.workloads.persona.", f".Values.workloads.{APP}.")
    out = re.sub(r"^(\s*(?:name|app): )persona$", rf"\g<1>{APP}", out, flags=re.M)
    out = out.replace("{name: APP_ID, value: persona}", f"{{name: APP_ID, value: {APP}}}")
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


def helm_release_estimate(chart_dir, manifest):
    """Approximate size of Helm's release Secret: base64(gzip(json(chart files as base64 + rendered manifest)))."""
    import gzip, json
    files = []
    for r, _, fs in os.walk(chart_dir):
        for f in fs:
            p = os.path.join(r, f)
            files.append({"name": os.path.relpath(p, chart_dir), "data": base64.b64encode(open(p, "rb").read()).decode()})
    j = json.dumps({"chart": {"files": files}, "manifest": manifest}).encode()
    return len(base64.b64encode(gzip.compress(j, 9)))


est = helm_release_estimate(dist, out)
print(f"helm release ~{est} bytes (Kubernetes Secret limit 1048576)")
if est > 980_000:
    sys.exit(f"helm release too large ({est} bytes): the upgrade would fail; shrink the bundle")
