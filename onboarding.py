#!/usr/bin/env python3
"""Local setup page for timelens: pick a goal, edit categories, add context, connect a model, start tracking.

Runs on http://127.0.0.1:8766 and only talks to this Mac (plus one test request to the model you choose).
`timelens setup` (or ./install.sh in a git checkout) starts it; rerun it any time to change your setup.
"""
import json, os, plistlib, subprocess, sys, tempfile, threading, time, tomllib, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent                    # where presets, the page and timelens.py are
HOME = Path(os.environ.get("TIMELENS_HOME") or ROOT)      # stable install path for launchd (Homebrew's opt/ link)
DATA = Path(os.environ.get("TIMELENS_DATA", Path.home() / "Library/Application Support/timelens"))
AGENTS = Path.home() / "Library/LaunchAgents"
PORT = int(os.environ.get("TIMELENS_PORT", 8766))
DRY_RUN = bool(os.environ.get("TIMELENS_DRY_RUN"))  # walk through setup without touching the Keychain or launchd
LOCAL = Path(tempfile.mkdtemp()) / "config.toml" if DRY_RUN else DATA / "config.toml"  # where the setup is saved
EXISTING = next((p for p in (DATA / "config.toml", ROOT / "config.local.toml") if p.exists()), None)
AGENT_FILE = DATA / "agent-context.json"  # written by the user's AI agent, see AGENT_SETUP.md

PROVIDERS = {  # what the setup page offers; any System One / Decisions endpoint works if you edit the config by hand
    "openrouter": {
        "label": "OpenRouter", "blurb": "One key, several decision models",
        "api_url": "https://openrouter.ai/api/alpha/decisions", "keychain_service": "timelens-openrouter",
        "models": [
            # tokens: typical input per call with a screenshot (measured for Perplexity; others scaled from eval.py ratios)
            {"id": "perplexity/pplx-decider-v1.2", "label": "Perplexity Decider v1.2", "pin": "Perplexity", "price": 0.020, "tokens": 2350, "note": "recommended"},
            {"id": "typesafe/jev-1.14", "label": "TypeSafe Jev 1.14", "pin": "TypeSafe", "price": 0.042, "tokens": 5300, "note": "fastest"},
            {"id": "cloudflare/clef-flash", "label": "Cloudflare Clef-flash", "pin": "Cloudflare", "price": 0.038, "tokens": 2700, "note": "open weights"},
        ]},
    "typesafe": {
        "label": "TypeSafe", "blurb": "Jev, straight from its makers",
        "api_url": "https://api.typesafe.ai/v1/systemone", "keychain_service": "timelens-typesafe",
        "models": [{"id": "jev-1.14.0", "label": "TypeSafe Jev 1.14", "pin": "", "price": 0.042, "tokens": 5300, "note": ""}]},
    "local": {
        "label": "On this Mac", "blurb": "Nothing leaves your Mac. Labels minutes while you're away",
        "api_url": "http://127.0.0.1:8000/v1/systemone", "keychain_service": "",
        "models": [{"id": "clef-flash-oQ3e", "label": "Cloudflare Clef-flash (5.5 GB, via oMLX)", "pin": "", "price": 0, "tokens": 0, "note": "experimental"}]},
}
DEFAULT_PRIVATE_APPS = ["com.apple.Passwords", "com.apple.keychainaccess", "com.1password.1password",
                        "com.bitwarden.desktop", "com.dashlane.dashlanephonefinal"]
DEFAULT_PRIVATE_WORDS = ["bank", "paypal", "revolut", "wise.com", "stripe.com/dashboard"]


def presets():
    return tomllib.loads((ROOT / "presets.toml").read_text())["preset"]


LAUNCHER = HOME / "timelens.app/Contents/MacOS/timelens"


def python_path():
    """What macOS lists under Screen Recording: "timelens" with the app, else the real file behind this python."""
    return "timelens" if LAUNCHER.exists() else os.path.realpath(sys.executable)


def folders(dirs):
    out = {}
    for d in dirs:
        p = Path(d).expanduser()
        out[d] = sorted(s.name for s in p.glob("*/") if not s.name.startswith(".")) if p.is_dir() else None
    return out


def toml_str(v):
    return json.dumps(v, ensure_ascii=False)  # a JSON string is a valid TOML basic string


def build_config(c):
    """The choices from the page -> a config dict (same shape timelens.py reads)."""
    prov = PROVIDERS[c["provider"]]
    m = next(x for x in prov["models"] if x["id"] == c["model"])
    cats = [x for x in c["categories"] if x["name"].strip()]
    return {
        "api_url": prov["api_url"], "model": m["id"], "provider": m["pin"], "keychain_service": prov["keychain_service"],
        "price_per_mtok": m["price"],
        "interval_seconds": 60, "screenshots": bool(c["screenshots"]), "idle_minutes": 5, "min_confidence": 0.25,
        "nudges": True, "break_after_minutes": 120,
        "processing": "batch" if c["provider"] == "local" else "live",
        "batch_window": c.get("batch_window", "") if c["provider"] == "local" else "", "batch_idle_minutes": 10,
        "about": c["about"].strip(),
        "categories": {x["name"].strip(): x["description"].strip() for x in cats},
        "examples": {x["name"].strip(): x["example"].strip() for x in cats if x.get("example", "").strip()},
        "report": {"focus": [x["name"] for x in cats if x["kind"] == "focus"],
                   "intentional": [x["name"] for x in cats if x["kind"] in ("focus", "neutral")],
                   "distractions": [x["name"] for x in cats if x["kind"] == "distraction"]},
        "privacy": {"apps": DEFAULT_PRIVATE_APPS, "url_words": DEFAULT_PRIVATE_WORDS},
        "projects": {"dirs": c["dirs"], "ignore": c.get("ignore", []),
                     "known": {k: v for k, v in c["known"].items() if v.strip() and k not in c.get("ignore", [])}},
    }


def write_config(cfg):
    lines = ["# Written by the timelens setup page. Rerun ./install.sh to change it, or edit by hand.", ""]
    for k in ("api_url", "model", "provider", "keychain_service", "price_per_mtok", "interval_seconds",
              "screenshots", "idle_minutes", "min_confidence", "nudges", "break_after_minutes", "processing",
              "batch_window", "batch_idle_minutes"):
        v = cfg[k]
        lines.append(f"{k} = {str(v).lower() if isinstance(v, bool) else toml_str(v) if isinstance(v, str) else v}")
    lines += [f"about = {toml_str(cfg['about'])}", "", "[categories]"]
    lines += [f"{toml_str(k)} = {toml_str(v)}" for k, v in cfg["categories"].items()]
    lines += ["", "# A real-life example per category, sent to the model with the description", "[examples]"]
    lines += [f"{toml_str(k)} = {toml_str(v)}" for k, v in cfg["examples"].items()]
    lines += ["", "[report]"] + [f"{k} = {json.dumps(v)}" for k, v in cfg["report"].items()]
    lines += ["", "[privacy]"] + [f"{k} = {json.dumps(v)}" for k, v in cfg["privacy"].items()]
    lines += ["", "[projects]", f"dirs = {json.dumps(cfg['projects']['dirs'])}",
              f"ignore = {json.dumps(cfg['projects']['ignore'])}  # folders that aren't projects", "", "[projects.known]"]
    lines += [f"{toml_str(k)} = {toml_str(v)}" for k, v in cfg["projects"]["known"].items()]
    text = "\n".join(lines) + "\n"
    assert tomllib.loads(text)["categories"] == cfg["categories"]  # round-trip check before anything is replaced
    LOCAL.parent.mkdir(parents=True, exist_ok=True)
    LOCAL.write_text(text)


def store_key(service, key):
    # ponytail: the key is passed to `security` as an argument for an instant; fine on a single-user Mac
    subprocess.run(["security", "add-generic-password", "-U", "-s", service, "-a", "timelens", "-w", key], check=True)


def install_agents():
    """The sampler's launchd agent (always on). It also shows yesterday's recap at your first minute each day."""
    DATA.mkdir(parents=True, exist_ok=True)
    AGENTS.mkdir(parents=True, exist_ok=True)
    py, script = sys.executable, str(HOME / "timelens.py")  # the venv this setup runs in, the stable install path
    # with timelens.app (Homebrew installs it) macOS shows "timelens" in Privacy settings instead of python3.13
    sampler = [str(LAUNCHER), "run"] if LAUNCHER.exists() else [py, script, "run"]
    jobs = {
        "com.timelens.sampler": {"ProgramArguments": sampler, "RunAtLoad": True, "KeepAlive": True},
    }
    # earlier versions sent the report at a fixed evening time; the sampler shows it in the morning now
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/com.timelens.report"], capture_output=True)
    (AGENTS / "com.timelens.report.plist").unlink(missing_ok=True)
    for label, job in jobs.items():
        path = AGENTS / f"{label}.plist"
        path.write_bytes(plistlib.dumps({"Label": label, "ProcessType": "Background",
                                         "StandardErrorPath": str(DATA / f"{label}.log"), **job}))
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], capture_output=True)
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)], check=True)


def test_key(c, key):
    """One text-only request with the user's own categories, to prove the key and the categories work."""
    timelens = load_timelens()
    cfg = build_config(c)
    body = timelens.build_request("Safari", "Inbox (3) - Gmail", "https://mail.google.com/mail/u/0/#inbox", None, cfg)
    t0 = time.time()
    try:
        r = timelens.call(body, cfg, key)
    except OSError as e:
        if cfg["api_url"].startswith("http://127.0.0.1") and "refused" in str(e).lower():
            raise RuntimeError("Can't reach the model server on 127.0.0.1:8000. Is oMLX running? Press ? for the steps.")
        raise
    a = r["answers"]["activity"]
    return {"choice": a["choice"], "confidence": round(a["confidence"], 2), "seconds": round(time.time() - t0, 1)}


def status():
    """What the sampler has done so far, for the live panel on the last step."""
    out = {"running": False, "samples": [], "needs_permission": False}
    r = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/com.timelens.sampler"], capture_output=True, text=True)
    out["running"] = "state = running" in r.stdout
    log = DATA / "com.timelens.sampler.log"
    if log.exists():
        recent = log.read_text()[-4000:]
        out["needs_permission"] = "screenshot failed" in recent.splitlines()[-1] if recent.strip() else False
    db = DATA / "log.sqlite"
    if db.exists():
        import sqlite3
        con = sqlite3.connect(db)
        rows = con.execute("select ts, status, app, activity, activity_conf, project, title from samples "
                           "order by ts desc limit 5").fetchall()
        out["samples"] = [dict(zip(("ts", "status", "app", "activity", "confidence", "project", "title"), r)) for r in rows]
    return out


def load_timelens():
    os.environ.setdefault("TIMELENS_CONFIG", str(ROOT / "config.toml"))  # timelens needs some config to import
    sys.path.insert(0, str(ROOT))
    import timelens
    return timelens


PREVIEW = Path(tempfile.mkdtemp(prefix="timelens-preview-"))


def preview(c, projects):
    """A made-up week drawn with the user's own categories and projects, so they see their report before day one."""
    timelens = load_timelens()
    cats = [{"name": x["name"], "kind": x["kind"], "description": x["description"]} for x in c["categories"] if x["name"]]
    timelens.demo(PREVIEW, cats, (projects or list(c.get("known", {})) or ["project-a", "project-b"])[:4])
    reports = sorted((PREVIEW / "reports").glob("*.html"))
    day = next(r for r in reports if not r.name.startswith("week"))
    return {"day": f"/preview/{day.name}", "week": f"/preview/{next(r for r in reports if r.name.startswith('week')).name}"}


PAGE = (ROOT / "onboarding.html").read_text


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, obj, code=200, ctype="application/json"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            return self.reply(PAGE().encode(), ctype="text/html; charset=utf-8")
        if self.path == "/api/state":
            existing = tomllib.loads(EXISTING.read_text()) if EXISTING else None
            return self.reply({"presets": presets(), "providers": PROVIDERS, "existing": existing, "dry_run": DRY_RUN,
                               "root": str(ROOT), "agent_file": str(AGENT_FILE),
                               "home": str(Path.home()), "python": python_path()})
        if self.path == "/api/agent-context":  # written by the user's coding agent, see AGENT_SETUP.md
            f = AGENT_FILE
            return self.reply(json.loads(f.read_text()) if f.exists() else {})
        if self.path == "/api/status":
            return self.reply(status())
        if self.path.startswith("/preview/"):  # the report preview built from the user's own categories
            f = PREVIEW / "reports" / Path(self.path).name
            return self.reply(f.read_bytes(), ctype="text/html; charset=utf-8") if f.is_file() else self.reply({}, 404)
        self.reply({"error": "not found"}, 404)

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        try:
            if self.path == "/api/folders":
                return self.reply(folders(req["dirs"]))
            if self.path == "/api/test":
                return self.reply(test_key(req["config"], req["key"]))
            if self.path == "/api/install":
                cfg = build_config(req["config"])
                write_config(cfg)
                if not DRY_RUN:
                    if req.get("key") and cfg["keychain_service"]:
                        store_key(cfg["keychain_service"], req["key"])
                    install_agents()
                return self.reply({"ok": True, "python": python_path(), "dry_run": DRY_RUN, "config": str(LOCAL)})
            if self.path == "/api/agent-reset":
                AGENT_FILE.unlink(missing_ok=True)
                return self.reply({"ok": True})
            if self.path == "/api/preview":
                return self.reply(preview(req["config"], req.get("projects") or []))
            if self.path == "/api/open-settings":
                subprocess.run(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"])
                return self.reply({"ok": True})
            if self.path == "/api/restart":
                subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.timelens.sampler"])
                return self.reply({"ok": True})
            if self.path == "/api/report":
                subprocess.run([sys.executable, str(ROOT / "timelens.py"), "report", "--open"])
                return self.reply({"ok": True})
            if self.path == "/api/done":
                threading.Thread(target=self.server.shutdown).start()
                return self.reply({"ok": True})
        except Exception as e:
            msg = e.read().decode()[:300] if hasattr(e, "read") else str(e)
            return self.reply({"error": msg}, 400)
        self.reply({"error": "not found"}, 404)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"\nThe timelens setup page is opening in your browser: {url}\n"
          "(If it didn't open, paste that address into your browser. Press Ctrl-C here to stop.)\n")
    if not os.environ.get("TIMELENS_NO_BROWSER"):
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
