#!/usr/bin/env python3
"""Local setup page for timelens: pick a goal, edit categories, add context, test a key, start tracking.

Runs on http://127.0.0.1:8766 and only talks to this Mac (plus one test request to the model you choose).
`./install.sh` starts it; you can rerun it any time to change your setup.
"""
import json, os, plistlib, subprocess, sys, threading, time, tomllib, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOCAL = ROOT / "config.local.toml"
DATA = Path.home() / "Library/Application Support/timelens"
AGENTS = Path.home() / "Library/LaunchAgents"
PORT = 8766
DRY_RUN = bool(os.environ.get("TIMELENS_DRY_RUN"))  # walk through setup without touching the Keychain or launchd

MODELS = {  # what the setup page offers; any System One / Decisions model works if you edit the config by hand
    "perplexity": {"label": "Perplexity Decider v1.2 via OpenRouter", "api_url": "https://openrouter.ai/api/alpha/decisions",
                   "model": "perplexity/pplx-decider-v1.2", "provider": "Perplexity", "price_per_mtok": 0.020,
                   "keychain_service": "timelens-openrouter", "key_url": "https://openrouter.ai/settings/keys"},
    "jev": {"label": "TypeSafe Jev 1.14", "api_url": "https://api.typesafe.ai/v1/systemone", "model": "jev-1.14.0",
            "provider": "", "price_per_mtok": 0.042, "keychain_service": "timelens-typesafe",
            "key_url": "https://console.typesafe.ai"},
}
DEFAULT_PRIVATE_APPS = ["com.apple.Passwords", "com.apple.keychainaccess", "com.1password.1password",
                        "com.bitwarden.desktop", "com.dashlane.dashlanephonefinal"]
DEFAULT_PRIVATE_WORDS = ["bank", "paypal", "revolut", "wise.com", "stripe.com/dashboard"]


def presets():
    return tomllib.loads((ROOT / "presets.toml").read_text())["preset"]


def python_path():
    """The binary macOS must allow under Screen Recording (the venv python resolves to it)."""
    py = ROOT / ".venv/bin/python"
    return subprocess.run([py, "-c", "import os,sys;print(os.path.realpath(sys.executable))"],
                          capture_output=True, text=True).stdout.strip()


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
    m = MODELS[c["model"]]
    cats = [x for x in c["categories"] if x["name"].strip()]
    return {
        **{k: m[k] for k in ("api_url", "model", "provider", "keychain_service", "price_per_mtok")},
        "interval_seconds": 60, "screenshots": bool(c["screenshots"]), "idle_minutes": 5, "min_confidence": 0.25,
        "report_time": c["report_time"] or "19:00",
        "about": c["about"].strip(),
        "categories": {x["name"].strip(): x["description"].strip() for x in cats},
        "report": {"focus": [x["name"] for x in cats if x["kind"] == "focus"],
                   "intentional": [x["name"] for x in cats if x["kind"] in ("focus", "neutral")],
                   "distractions": [x["name"] for x in cats if x["kind"] == "distraction"]},
        "privacy": {"apps": DEFAULT_PRIVATE_APPS, "url_words": DEFAULT_PRIVATE_WORDS},
        "projects": {"dirs": c["dirs"], "known": {k: v for k, v in c["known"].items() if v.strip()}},
    }


def write_config(cfg):
    lines = ["# Written by the timelens setup page. Rerun ./install.sh to change it, or edit by hand.", ""]
    for k in ("api_url", "model", "provider", "keychain_service", "price_per_mtok", "interval_seconds",
              "screenshots", "idle_minutes", "min_confidence", "report_time"):
        v = cfg[k]
        lines.append(f"{k} = {str(v).lower() if isinstance(v, bool) else toml_str(v) if isinstance(v, str) else v}")
    lines += [f"about = {toml_str(cfg['about'])}", "", "[categories]"]
    lines += [f"{toml_str(k)} = {toml_str(v)}" for k, v in cfg["categories"].items()]
    lines += ["", "[report]"] + [f"{k} = {json.dumps(v)}" for k, v in cfg["report"].items()]
    lines += ["", "[privacy]"] + [f"{k} = {json.dumps(v)}" for k, v in cfg["privacy"].items()]
    lines += ["", "[projects]", f"dirs = {json.dumps(cfg['projects']['dirs'])}", "", "[projects.known]"]
    lines += [f"{toml_str(k)} = {toml_str(v)}" for k, v in cfg["projects"]["known"].items()]
    text = "\n".join(lines) + "\n"
    assert tomllib.loads(text)["categories"] == cfg["categories"]  # round-trip check before anything is replaced
    LOCAL.write_text(text)


def store_key(service, key):
    # ponytail: the key is passed to `security` as an argument for an instant; fine on a single-user Mac
    subprocess.run(["security", "add-generic-password", "-U", "-s", service, "-a", "timelens", "-w", key], check=True)


def install_agents(report_time):
    """Two launchd agents: the sampler (always on) and the evening report."""
    DATA.mkdir(parents=True, exist_ok=True)
    AGENTS.mkdir(parents=True, exist_ok=True)
    py, script = str(ROOT / ".venv/bin/python"), str(ROOT / "timelens.py")
    hour, minute = (int(x) for x in report_time.split(":"))
    jobs = {
        "com.timelens.sampler": {"ProgramArguments": [py, script, "run"], "RunAtLoad": True, "KeepAlive": True},
        "com.timelens.report": {"ProgramArguments": [py, script, "report", "--notify"],
                                "StartCalendarInterval": {"Hour": hour, "Minute": minute}},
    }
    for label, job in jobs.items():
        path = AGENTS / f"{label}.plist"
        path.write_bytes(plistlib.dumps({"Label": label, "ProcessType": "Background",
                                         "StandardErrorPath": str(DATA / f"{label}.log"), **job}))
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], capture_output=True)
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)], check=True)


def test_key(c, key):
    """One text-only request with the user's own categories, to prove the key and the categories work."""
    os.environ["TIMELENS_CONFIG"] = str(ROOT / "config.toml")  # timelens needs some config to import; we pass ours
    sys.path.insert(0, str(ROOT))
    import timelens
    cfg = build_config(c)
    body = timelens.build_request("Safari", "Inbox (3) - Gmail", "https://mail.google.com/mail/u/0/#inbox", None, cfg)
    t0 = time.time()
    r = timelens.call(body, cfg, key)
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
            existing = tomllib.loads(LOCAL.read_text()) if LOCAL.exists() else None
            return self.reply({"presets": presets(), "models": MODELS, "existing": existing, "dry_run": DRY_RUN,
                               "home": str(Path.home()), "python": python_path()})
        if self.path == "/api/status":
            return self.reply(status())
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
                    if req.get("key"):
                        store_key(cfg["keychain_service"], req["key"])
                    install_agents(cfg["report_time"])
                return self.reply({"ok": True, "python": python_path(), "dry_run": DRY_RUN, "config": str(LOCAL)})
            if self.path == "/api/open-settings":
                subprocess.run(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"])
                return self.reply({"ok": True})
            if self.path == "/api/restart":
                subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.timelens.sampler"])
                return self.reply({"ok": True})
            if self.path == "/api/report":
                subprocess.run([str(ROOT / ".venv/bin/python"), str(ROOT / "timelens.py"), "report", "--open"])
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
    print(f"timelens setup is open at {url} (Ctrl-C to quit)")
    if not os.environ.get("TIMELENS_NO_BROWSER"):
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
