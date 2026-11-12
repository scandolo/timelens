#!/usr/bin/env python3
"""timelens: every minute, label what's on screen with a decision model; write daily and weekly reports.

  timelens setup                   open the setup page (goals, categories, model, permission)
  timelens report [DATE] [--open]  today's (or DATE's) report and its week
  timelens sample [--dry-run]      one sample now; --dry-run prints the request and sends nothing
  timelens process                 label every queued minute now (on-this-Mac mode)
  timelens demo [DIR]              render an invented week, no API and no screen access
  timelens mcp                     MCP server on stdin/stdout, so your agent can read the log (read-only)
  timelens selftest
  timelens run                     sample forever (launchd runs this for you)

Your setup lives in ~/Library/Application Support/timelens/config.toml ($TIMELENS_CONFIG overrides it).
"""
import base64, html, json, os, re, sqlite3, subprocess, sys, tempfile, threading, time, tomllib, urllib.error, urllib.request, uuid
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("TIMELENS_DATA", Path.home() / "Library/Application Support/timelens"))
CONFIG = Path(os.environ.get("TIMELENS_CONFIG") or next(  # yours, then a git checkout's local one, then the defaults
    p for p in (DATA / "config.toml", ROOT / "config.local.toml", ROOT / "config.toml") if p.exists()))
CFG = tomllib.loads(CONFIG.read_text())
DB = DATA / "log.sqlite"
QUEUE = DATA / "queue"   # batch mode only: screenshots waiting to be labelled by a local model
RECAP = DATA / "last-recap"  # the date the morning recap last appeared
APP = ROOT / "timelens.app/Contents/MacOS/timelens"  # the signed launcher; Homebrew installs it next to this file

CATEGORIES = {k: " ".join(v.split()) for k, v in CFG["categories"].items()}
PALETTE = ["#3b6fe0", "#6c8ff0", "#2fa58a", "#e0a020", "#9b6bd6", "#5bb4d6", "#8a8f98", "#e0566b", "#b9bdc4", "#c78a5b"]
COLORS = dict(zip(CATEGORIES, PALETTE)) | {"private": "#5b5f66", "unclear": "#d7d9dd", "pending": "#c9cdd3"}
FOCUS, INTENTIONAL = CFG["report"]["focus"], CFG["report"]["intentional"]
DISTRACTIONS = CFG["report"].get("distractions", [])


# ---------- sampling ----------

def db():
    DATA.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.execute("""create table if not exists samples(ts integer primary key, status text, app text, bundle text,
        title text, url text, activity text, activity_conf real, project text, project_conf real,
        answers text, tokens integer)""")
    return con


def front_window():
    """(app, bundle_id, title, window_id) of the frontmost normal window. Titles need Screen Recording permission."""
    import Quartz
    from AppKit import NSRunningApplication
    opts = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    for w in Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []:  # front-to-back order
        if w.get("kCGWindowLayer") == 0:
            app = NSRunningApplication.runningApplicationWithProcessIdentifier_(w["kCGWindowOwnerPID"])
            return (w.get("kCGWindowOwnerName"), app.bundleIdentifier() if app else None,
                    w.get("kCGWindowName"), w["kCGWindowNumber"])
    return None, None, None, None


def away(last_activity):
    import Quartz
    if (Quartz.CGSessionCopyCurrentDictionary() or {}).get("CGSSessionScreenIsLocked"):
        return True
    idle = Quartz.CGEventSourceSecondsSinceLastEventType(Quartz.kCGEventSourceStateHIDSystemState, 0xFFFFFFFF)
    # ponytail: meetings are hands-off, so allow an hour of idle; videos/reading past idle_minutes count as away
    on_call = any(w in (last_activity or "") for w in ("meeting", "call"))  # calls are hands-off
    return idle > (3600 if on_call else CFG["idle_minutes"] * 60)


CHROMIUM = 'tell application "{}" to get URL of active tab of front window'
BROWSERS = {  # bundle id -> AppleScript that returns the front tab's URL (Firefox has no AppleScript for this)
    "com.apple.Safari": 'tell application "Safari" to get URL of front document',
    "com.google.Chrome": CHROMIUM.format("Google Chrome"),
    "company.thebrowser.Browser": CHROMIUM.format("Arc"),
    "com.brave.Browser": CHROMIUM.format("Brave Browser"),
    "com.microsoft.edgemac": CHROMIUM.format("Microsoft Edge"),
    "com.vivaldi.Vivaldi": CHROMIUM.format("Vivaldi"),
}


def browser_url(bundle):
    if bundle not in BROWSERS:
        return None
    r = subprocess.run(["osascript", "-e", BROWSERS[bundle]], capture_output=True, text=True, timeout=5)
    return r.stdout.strip() or None


EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{1,4}){4,8}\b")
DIGITS = re.compile(r"(?<![\w.:#])\+?\(?\d[\d ()\-]{6,}\d(?![\w.:])")  # runs of digits with spaces, dashes, brackets


def _mask_digits(m):
    n = sum(ch.isdigit() for ch in m.group())  # dates (8 digits), times and versions stay readable
    return "[number]" if n >= 12 else "[phone]" if n >= 9 else m.group()


def redact(text):
    """Mask email addresses, phone numbers, card/account numbers and IBANs before anything is stored or sent."""
    if not text or not CFG.get("redact", True):
        return text
    return DIGITS.sub(_mask_digits, IBAN.sub("[iban]", EMAIL.sub("[email]", text)))


def redact_url(url):
    """Keep the site and path (what makes labels accurate); drop the query and fragment (tokens, searches, IDs)."""
    if not url or not CFG.get("redact", True):
        return url
    return redact(url.split("#")[0].split("?")[0])


def screenshot(window_id):
    """JPEG bytes of the front window, longest side 1280px. The temp file lives ~1s and is deleted."""
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "s.jpg")
        try:
            subprocess.run(["screencapture", "-x", "-o", "-t", "jpg", "-l", str(window_id), p], check=True, timeout=10)
            subprocess.run(["sips", "-Z", "1280", "-s", "formatOptions", "70", p], capture_output=True, check=True)
            out = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", p],
                                 capture_output=True, text=True, check=True).stdout
        except Exception as e:  # no permission / window gone: fall back to text only
            print(f"{datetime.now():%F %T} screenshot failed: {e!r}", file=sys.stderr)
            return None
        w, h = (int(l.split()[-1]) for l in out.splitlines() if "pixel" in l)
        return Path(p).read_bytes() if min(w, h) >= 280 else None  # model rejects images under 280px


def projects(cfg=None):
    cfg, p = cfg or CFG, {}
    ignore = set(cfg["projects"].get("ignore", []))
    for d in cfg["projects"]["dirs"]:
        for sub in sorted(Path(d).expanduser().glob("*/")):
            if not sub.name.startswith(".") and sub.name not in ignore:
                p[sub.name] = f"The project or repository called {sub.name}."
    p.update(cfg["projects"].get("known", {}))
    p["new_project"] = "A specific project, client or company that is not in this list."
    p["none"] = "Not about any specific project (personal browsing, general admin), or it cannot be identified."
    return p


def build_request(app, title, url, jpeg, cfg=None):
    cfg = cfg or CFG
    examples = cfg.get("examples", {})
    categories = {k: " ".join(v.split()) + (f" Example: {examples[k]}." if k in examples else "")
                  for k, v in cfg["categories"].items()}
    ref = f"img-{uuid.uuid4()}"
    state = [f"App: {app}", f"Window title: {title or 'unknown'}", f"URL: {url or 'none'}"] + ([ref] if jpeg else [])
    seen = "the app, window title, URL" + (" and the screenshot of the active window" if jpeg else "")
    about = " ".join(cfg.get("about", "").split())
    body = {
        "model": cfg["model"],
        "state": state,
        "questions": {
            "activity": {"type": "choice", "criteria": categories, "instructions":
                f"{about}\n\nBased on {seen}, what is the person doing right now? Judge by what they are doing and "
                "who it is for, not by which app it is."},
            "project": {"type": "choice", "criteria": projects(cfg), "instructions":
                f"{about}\n\nBased on {seen}, which project is this for? Look for project, repository, client or company names."},
        },
    }
    if cfg.get("provider"):
        body["provider"] = {"order": [cfg["provider"]], "allow_fallbacks": False}
    if jpeg:
        body["assets"] = {ref: {"type": "image", "format": "jpg", "data_base64": base64.b64encode(jpeg).decode()}}
    return body


def call(body, cfg=None, key=None):
    cfg = cfg or CFG
    if key is None and cfg.get("keychain_service"):  # local servers need no key
        key = subprocess.run(["security", "find-generic-password", "-s", cfg["keychain_service"], "-w"],
                             capture_output=True, text=True, check=True).stdout.strip()
    req = urllib.request.Request(cfg["api_url"], json.dumps(body).encode(), {
        "Authorization": f"Bearer {key or ''}", "Content-Type": "application/json", "TypeSafe-Danger-Zone-Images": "true"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except (urllib.error.URLError, TimeoutError):  # pinned provider has no fallback: retry a blip once
        time.sleep(3)
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)


def sample(con, dry_run=False, verbose=False):
    ts = int(time.time())
    last = con.execute("select activity from samples where ts > ? order by ts desc limit 1", (ts - 180,)).fetchone()
    if away(last and last[0]):
        return con.execute("insert or replace into samples(ts, status) values(?, 'away')", (ts,))
    app, bundle, title, wid = front_window()
    url = browser_url(bundle)
    if bundle in CFG["privacy"]["apps"] or any(w in (url or "") for w in CFG["privacy"]["url_words"]):
        return con.execute("insert or replace into samples(ts, status, app) values(?, 'private', ?)", (ts, app))
    title, url = redact(title), redact_url(url)
    jpeg = screenshot(wid) if wid and CFG.get("screenshots", True) else None
    if CFG.get("processing") == "batch" and not dry_run:  # local model: queue now, label later while you're away
        return enqueue(con, ts, app, bundle, title, url, jpeg)
    body = build_request(app, title, url, jpeg)
    if verbose or dry_run:
        shown = json.loads(json.dumps(body))
        for a in shown.get("assets", {}).values():
            a["data_base64"] = f"<{len(a['data_base64'])} base64 chars>"
        print(json.dumps(shown, indent=2))
    if dry_run:
        return
    try:
        r = call(body)
    except Exception as e:
        print(f"{datetime.now():%F %T} api error: {e}", file=sys.stderr)
        if offline_or_busy(e):  # no Wi-Fi, timeout, rate limit: keep the minute and label it once the API answers again
            return enqueue(con, ts, app, bundle, title, url, jpeg)
        return con.execute("insert or replace into samples(ts, status, app, bundle, title, url) values(?, 'error', ?, ?, ?, ?)",
                           (ts, app, bundle, title, url))
    if verbose:
        print(json.dumps(r, indent=2))
    store(con, ts, app, bundle, title, url, r)


def offline_or_busy(e):
    if isinstance(e, urllib.error.HTTPError):
        return e.code in (408, 429, 500, 502, 503, 504, 529)
    return isinstance(e, (urllib.error.URLError, TimeoutError, ConnectionError))


def enqueue(con, ts, app, bundle, title, url, jpeg):
    """Park a minute in the private queue folder (readable only by you) until it can be labelled."""
    QUEUE.mkdir(mode=0o700, parents=True, exist_ok=True)
    (QUEUE / f"{ts}.json").write_text(json.dumps({"app": app, "bundle": bundle, "title": title, "url": url}))
    if jpeg:
        (QUEUE / f"{ts}.jpg").write_bytes(jpeg)
    return con.execute("insert or replace into samples(ts, status, app, bundle, title, url) values(?, 'queued', ?, ?, ?, ?)",
                       (ts, app, bundle, title, url))


def store(con, ts, app, bundle, title, url, r):
    a = r["answers"]
    con.execute("insert or replace into samples values(?, 'ok', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, app, bundle, title, url, a["activity"]["choice"], a["activity"]["confidence"],
                 a["project"]["choice"], a["project"]["confidence"], json.dumps(a), r.get("usage", {}).get("input_tokens")))


def process_queue(con, limit=None):
    """Label queued minutes (batch mode, or minutes missed while offline), oldest first.
    Each screenshot is deleted as soon as its minute is labelled."""
    done = 0
    for meta in sorted(QUEUE.glob("*.json"))[:limit]:
        m, jpg = json.loads(meta.read_text()), meta.with_suffix(".jpg")
        try:
            r = call(build_request(m["app"], m["title"], m["url"], jpg.read_bytes() if jpg.exists() else None))
        except Exception as e:
            print(f"{datetime.now():%F %T} queue: {e!r}", file=sys.stderr)
            break
        store(con, int(meta.stem), m["app"], m["bundle"], m["title"], m["url"], r)
        con.commit()
        meta.unlink()
        jpg.unlink(missing_ok=True)
        done += 1
    return done


def good_time_to_process():
    """You've been away for a while, or it's inside the window you chose (e.g. "12:30-14:00")."""
    import Quartz
    idle = Quartz.CGEventSourceSecondsSinceLastEventType(Quartz.kCGEventSourceStateHIDSystemState, 0xFFFFFFFF)
    if idle >= CFG.get("batch_idle_minutes", 10) * 60:
        return True
    window = CFG.get("batch_window", "")
    if "-" not in window:
        return False
    start, end = window.split("-")
    now = datetime.now().strftime("%H:%M")
    return start <= now < end if start < end else (now >= start or now < end)  # windows may cross midnight


def run():
    con = db()
    while True:
        t0 = time.time()
        try:
            sample(con)
            con.commit()
            if CFG.get("processing") == "batch":
                if good_time_to_process():
                    process_queue(con, CFG.get("batch_per_minute", 4))
            elif QUEUE.exists() and any(QUEUE.glob("*.json")) and \
                    con.execute("select status from samples order by ts desc limit 1").fetchone()[0] == "ok":
                process_queue(con, 4)  # back online: catch up on minutes missed while offline, a few at a time
            recap(con)
            nudge(con)
            ask_new_project(con)
        except Exception as e:  # never die; launchd would restart us anyway
            print(f"{datetime.now():%F %T} {e!r}", file=sys.stderr)
        time.sleep(max(1, CFG["interval_seconds"] - (time.time() - t0)))


# ---------- report ----------

def day_minutes(con, d):
    """1440 slots for day d: None = away/no data, else (label, project)."""
    start = datetime.combine(d, datetime.min.time()).timestamp()
    mins, tokens, n, private = [None] * 1440, 0, 0, 0
    for ts, status, act, conf, proj, tok in con.execute(
            "select ts, status, activity, activity_conf, project, tokens from samples where ts >= ? and ts < ?",
            (start, start + 86400)):
        i = int((ts - start) // 60)
        if status == "away":
            continue
        n += 1
        tokens += tok or 0
        private += status == "private"
        label = ("private" if status == "private" else "pending" if status == "queued"
                 else act if status == "ok" and conf >= CFG["min_confidence"] else "unclear")
        mins[i] = (label, proj or "none")
    return mins, tokens, n, private


def focus_block(mins):
    """Longest run of 'work' on one project; interruptions of <=2 minutes don't break it, being away does."""
    best, start, proj, last, gap = (0, None, None, None), None, None, None, 0
    for i, m in enumerate(mins + [None]):
        is_work = m is not None and m[0] in FOCUS
        if start is not None and is_work and m[1] == proj:
            last, gap = i, 0
            continue
        if start is not None and m is not None and not is_work and gap < 2:
            gap += 1
            continue
        if start is not None and last - start + 1 > best[0]:
            best = (last - start + 1, start, last, proj)
        start, proj, last, gap = (i, m[1], i, 0) if is_work else (None, None, None, 0)
    return best


def metrics(mins):
    labels = [m[0] if m else None for m in mins]
    active = sum(l is not None for l in labels)
    return {
        "active": active,
        "cats": Counter(l for l in labels if l),
        "projects": Counter(m[1] for m in mins if m and m[0] not in ("private", "other", *DISTRACTIONS)),
        "block": focus_block(mins),
        "switches": sum(b in DISTRACTIONS and a not in DISTRACTIONS for a, b in zip([None] + labels, labels)),
        "intentional": sum(l in INTENTIONAL for l in labels) / active if active else 0,
    }


def insight(mins, m):
    work = [i for i, x in enumerate(mins) if x and x[0] in FOCUS]
    if len(work) >= 60:
        am = sum(i < 13 * 60 for i in work) / len(work)
        if am >= 0.65:
            return f"Your mornings are where the work gets done: <b>{am:.0%} of your work time came before 13:00</b>."
        if am <= 0.35:
            return f"Your afternoons carry the load: <b>{1 - am:.0%} of your work time came after 13:00</b>."
    length, s, _, p = m["block"]
    if length >= 90:
        return f"Best stretch: <b>{length} uninterrupted minutes</b> on {html.escape(p)}, starting {hhmm(s)}."
    if m["switches"] >= 10:
        return f"You drifted into distractions <b>{m['switches']} times</b>; your longest work stretch was {length} minutes."
    return ""


def hhmm(i):
    return f"{i // 60:02d}:{i % 60:02d}"


def dur(n):
    return f"{n // 60}h {n % 60:02d}"


def strip(mins, lo, hi):
    segs, i = [], lo
    while i < hi:
        j, cur = i, (mins[i] or (None,))[0]
        while j < hi and (mins[j] or (None,))[0] == cur:
            j += 1
        style = f"background:{COLORS[cur]}" if cur else "background:var(--hatch)"
        segs.append(f'<div style="flex:{j - i};{style}" title="{hhmm(i)}–{hhmm(j)} {cur or "away"}"></div>')
        i = j
    return f'<div class="strip">{"".join(segs)}</div>'


def bars(counter, color=None):
    top = max(counter.values(), default=1)
    rows = []
    for k, v in counter.most_common():
        c = color or COLORS.get(k, "#888")
        name = "No project" if k == "none" else k.replace("_", " ").capitalize() if color is None else k
        rows.append(f'<div class="row"><span><i class="dot" style="background:{c}"></i>{html.escape(name)}</span>'
                    f'<div class="bar"><div style="width:{v / top:.0%};background:{c}"></div></div>'
                    f'<span class="num">{dur(v)}</span></div>')
    return "".join(rows)


def delta(now, past, fmt, good_up=True):
    if past is None:
        return ""
    d = now - past
    if abs(d) < 0.5:
        return '<div class="d">same as {ref}</div>'
    cls = "up" if (d >= 0) == good_up else "down"
    return f'<div class="d {cls}">{fmt(d)} vs. {{ref}}</div>'


def page(title, total, body, tabs):
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>
<meta name="viewport" content="width=device-width,initial-scale=1"><style>
:root{{--bg:#fafafa;--fg:#17181a;--muted:#6b6f76;--card:#fff;--border:#e6e7e9;--track:#eef0f2;--good:#1f9d62;--bad:#d4443f;
--hatch:repeating-linear-gradient(45deg,#e6e7e9 0 3px,transparent 3px 6px)}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0e0f11;--fg:#ecedef;--muted:#8d929a;--card:#16181b;--border:#26292d;--track:#23262a;
--good:#3fc485;--bad:#f06a63;--hatch:repeating-linear-gradient(45deg,#2a2d31 0 3px,transparent 3px 6px)}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 -apple-system,BlinkMacSystemFont,"SF Pro Text",sans-serif}}
.wrap{{max-width:760px;margin:0 auto;padding:32px 16px;display:flex;flex-direction:column;gap:20px}}
.top{{display:flex;justify-content:space-between;align-items:flex-end;gap:12px;flex-wrap:wrap}}
.eyebrow{{font-size:12px;color:var(--muted);letter-spacing:.04em;text-transform:uppercase}}
h1{{margin:4px 0 0;font-size:26px;font-weight:600}}h1 span{{color:var(--muted);font-weight:400}}
.tabs{{display:flex;gap:2px;background:var(--track);padding:3px;border-radius:8px;font-size:13px}}
.tabs a{{padding:5px 12px;border-radius:6px;color:var(--muted);text-decoration:none}}.tabs a.on{{background:var(--card);color:var(--fg);font-weight:500}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px}}
.label{{font-size:12px;color:var(--muted);margin-bottom:10px}}
.strip{{display:flex;height:26px;border-radius:4px;overflow:hidden}}.strip div{{height:100%}}
.hours{{display:flex;justify-content:space-between;font:11px ui-monospace,monospace;color:var(--muted);margin-top:6px}}
.wk{{display:grid;grid-template-columns:36px 1fr 52px;gap:10px;align-items:center;margin:6px 0;font-size:12px}}
.grid{{display:grid;grid-template-columns:1.3fr 1fr;gap:16px}}@media (max-width:600px){{.grid{{grid-template-columns:1fr}}}}
.row{{display:grid;grid-template-columns:150px 1fr 52px;align-items:center;gap:10px;font-size:13px;padding:4px 0}}
.row span:first-child{{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.bar{{height:8px;border-radius:4px;background:var(--track);overflow:hidden}}.bar div{{height:100%;border-radius:4px}}
.num{{text-align:right;font:12px ui-monospace,monospace}}
.dot{{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:8px}}
.stats{{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}}
.v{{font:600 22px ui-monospace,monospace}}.k{{font-size:12px;color:var(--muted);margin-top:2px}}.d{{font-size:12px;margin-top:4px}}
.up{{color:var(--good)}}.down{{color:var(--bad)}}
.note{{line-height:1.55;border-left:2px solid #3b6fe0;padding-left:12px}}
.foot{{font:11px ui-monospace,monospace;color:var(--muted)}}
</style></head><body><div class="wrap">
<div class="top"><div><div class="eyebrow">{title}</div><h1>{total} <span>on the laptop</span></h1></div>{tabs}</div>
{body}</div></body></html>"""


def stats_card(m, ref_m, ref):
    length, s, e, p = m["block"]
    cells = [
        (f"{length}m", "Longest focus block", f'<div class="d">{hhmm(s)}–{hhmm(e + 1)} · {html.escape(p)}</div>' if length else ""),
        (str(m["switches"]), "Switches into distractions",
         delta(m["switches"], ref_m and ref_m["switches"], lambda d: f"{d:+.0f}", good_up=False)),
        (f"{m['intentional']:.0%}", "Time on intentional work",
         delta(m["intentional"] * 100, ref_m and ref_m["intentional"] * 100, lambda d: f"{d:+.0f} pts")),
    ]
    inner = "".join(f'<div><div class="v">{v}</div><div class="k">{k}</div>{x.replace("{ref}", ref)}</div>' for v, k, x in cells)
    return f'<div class="card stats">{inner}</div>'


def avg_metrics(ms):
    ms = [m for m in ms if m["active"]]
    if not ms:
        return None
    return {k: sum(m[k] for m in ms) / len(ms) for k in ("switches", "intentional")}


def report(d, con):
    DATA.joinpath("reports").mkdir(parents=True, exist_ok=True)
    monday = d - timedelta(days=d.weekday())
    day_file, week_file = f"{d}.html", f"week-{monday}.html"

    # day
    mins, tokens, n, private = day_minutes(con, d)
    m = metrics(mins)
    past = avg_metrics([metrics(day_minutes(con, d - timedelta(weeks=k))[0]) for k in range(1, 5)])
    on = [i for i, x in enumerate(mins) if x]
    lo, hi = (on[0], on[-1] + 1) if on else (9 * 60, 18 * 60)
    ticks = "".join(f"<span>{hhmm(lo + (hi - lo) * k // 4)}</span>" for k in range(5))
    note = insight(mins, m)
    body = (f'<div class="card"><div class="label">Your day, minute by minute · hatched = away</div>{strip(mins, lo, hi)}'
            f'<div class="hours">{ticks}</div></div>'
            f'<div class="grid"><div class="card"><div class="label">What you did</div>{bars(m["cats"])}</div>'
            f'<div class="card"><div class="label">Which project</div>{bars(m["projects"], "#7d828a")}</div></div>'
            + stats_card(m, past, f"your {d:%A} average")
            + (f'<div class="note">{note}</div>' if note else "")
            + f'<div class="foot">{n} samples · {private} private · 0 screenshots kept · '
              f'${tokens * CFG["price_per_mtok"] / 1e6:.3f} API spend</div>')
    tabs = f'<nav class="tabs"><a class="on" href="{day_file}">Day</a><a href="{week_file}">Week</a></nav>'
    DATA.joinpath("reports", day_file).write_text(page(f"{d:%A %-d %b}", dur(m["active"]), body, tabs))

    # week
    days = [monday + timedelta(days=k) for k in range(7)]
    wmins = [day_minutes(con, x)[0] for x in days]
    allm = [x for dm in wmins for x in dm]
    wm = metrics(allm)
    wm["block"] = max((metrics(dm)["block"] for dm in wmins), key=lambda b: b[0])
    prev = avg_metrics([metrics([x for k in range(7) for x in day_minutes(con, monday - timedelta(days=7 - k))[0]])])
    ons = [i for dm in wmins for i, x in enumerate(dm) if x]
    lo, hi = (min(ons), max(ons) + 1) if ons else (9 * 60, 18 * 60)
    rows = "".join(f'<div class="wk"><span>{x:%a}</span>{strip(dm, lo, hi)}<span class="num">{dur(sum(1 for v in dm if v))}</span></div>'
                   for x, dm in zip(days, wmins))
    ticks = "".join(f"<span>{hhmm(lo + (hi - lo) * k // 4)}</span>" for k in range(5))
    body = (f'<div class="card"><div class="label">Your week · hatched = away</div>{rows}'
            f'<div class="hours" style="margin-left:46px;margin-right:62px">{ticks}</div></div>'
            f'<div class="grid"><div class="card"><div class="label">What you did</div>{bars(wm["cats"])}</div>'
            f'<div class="card"><div class="label">Which project</div>{bars(wm["projects"], "#7d828a")}</div></div>'
            + stats_card(wm, prev, "last week"))
    tabs = f'<nav class="tabs"><a href="{day_file}">Day</a><a class="on" href="{week_file}">Week</a></nav>'
    DATA.joinpath("reports", week_file).write_text(page(f"Week of {monday:%-d %b}", dur(wm["active"]), body, tabs))
    return DATA / "reports" / (week_file if d.weekday() == 6 else day_file), (wm if d.weekday() == 6 else m)


def recap(con):
    """Once a day, at the first minute you're at the Mac: yesterday's headline (last week's on Mondays) and its report."""
    today = date.today()
    if (RECAP.read_text().strip() if RECAP.exists() else "") >= str(today) or \
            con.execute("select status from samples order by ts desc limit 1").fetchone()[0] == "away":
        return
    RECAP.write_text(str(today))
    yesterday = today - timedelta(days=1)
    path, m = report(yesterday, con)
    if m["active"]:
        notify(m, yesterday.weekday() == 6, path)


def notify(m, week, path):
    """The headline as a notification that opens the report when clicked. When that can't be shown (no timelens.app,
    or notifications not allowed), a small dialog with an "Open report" button instead, so the recap is never lost."""
    title, text = f"timelens: {'last week' if week else 'yesterday'}", f"{dur(m['active'])} on the laptop · {m['intentional']:.0%} intentional"
    banner(title, text, path, fallback=lambda: subprocess.run(
        ["osascript", "-e", "on run argv", "-e", "activate", "-e",
         'set r to display dialog (item 1 of argv) with title (item 2 of argv) buttons {"Not now", "Open report"} '
         'cancel button "Not now" giving up after 900', "-e",
         'if button returned of r is "Open report" then do shell script "open " & quoted form of (item 3 of argv)',
         "-e", "end run", text, title, str(path)], stdout=subprocess.DEVNULL))  # errors go to the tracker's log


def banner(title, text, open_path=None, fallback=None):
    """A macOS notification. Through timelens.app it shows the timelens icon and a click opens open_path. If the app
    can't post it (missing, an older app, or notifications not allowed), `fallback` runs, or osascript's notification
    (Script Editor's icon, and a click can't open anything). Runs in a thread: the app may wait for the permission prompt."""
    def post():
        cmd = [str(APP), "notify", "--title", title, "--body", text] + (["--open", str(open_path)] if open_path else [])
        if APP.exists() and subprocess.run(cmd, stdout=subprocess.DEVNULL).returncode == 0:  # errors go to the tracker's log
            return
        (fallback or (lambda: subprocess.run(["osascript", "-e", "on run argv", "-e",
                                              "display notification (item 2 of argv) with title (item 1 of argv)",
                                              "-e", "end run", title, text], stdout=subprocess.DEVNULL)))()
    threading.Thread(target=post, daemon=True).start()


FRAYING = 1.4  # switching this many times faster than your usual counts as focus fraying
BREAK = 10   # minutes away (or asleep: no samples at all) that count as a break
nudged = 0   # when the break reminder last appeared, so each stretch at the laptop gets one


def nudge(con):
    """After break_after_minutes at the laptop without a break, one gentle notification for that stretch."""
    global nudged
    if not CFG.get("nudges", True):
        return
    rows = con.execute("select ts, status, activity from samples where ts > (select max(ts) from samples) - 43200 "
                       "order by ts").fetchall()  # the last 12 hours
    here = [r for r in rows if r[1] != "away"]
    if not here or rows[-1][1] == "away":
        return
    start = here[0][0]
    for a, b in zip(here, here[1:]):
        if b[0] - a[0] > (BREAK + .5) * 60:  # 10 minutes away = 11 between two samples here, 9 = 10: split the difference
            start = b[0]
    end = here[-1][0]
    if start <= nudged or end - start < min(60, CFG.get("break_after_minutes", 120)) * 60:
        return
    stretch = [r for r in here if r[0] >= start]
    labelled = [r[2] for r in stretch if r[1] == "ok"]
    focus = f", {sum(a in FOCUS for a in labelled) / len(labelled):.0%} focused" if labelled else ""
    rate, usual = switch_rate(con, end - 1200, end), usual_switch_rate(con)
    if end - start >= CFG.get("break_after_minutes", 120) * 60:
        title = f"timelens: {dur(CFG.get('break_after_minutes', 120)).replace(' 00', '')} without a break"
        body = f"{dur((end - start) // 60)} at the laptop{focus}. A good moment to get up and drink some water."
    elif usual and rate >= max(FRAYING * usual, 12):  # focus is fraying: an early break beats the 2-hour mark
        title = "timelens: time for a break?"
        body = (f"{dur((end - start) // 60)} without a break, and you're switching apps {rate:.0f} times an hour "
                f"(you usually do {usual:.0f}). Good moment for a short break.")
    else:
        return
    nudged = end
    banner(title, body)


def switch_rate(con, a, b):
    """App switches per hour at the Mac between a and b (one sample a minute: a change of app counts as a switch)."""
    rows = con.execute("select ts, app from samples where ts >= ? and ts <= ? and status != 'away' order by ts", (a, b)).fetchall()
    pairs = [(x, y) for x, y in zip(rows, rows[1:]) if y[0] - x[0] <= 90]
    return sum(x[1] != y[1] for x, y in pairs) * 60 / len(pairs) if len(pairs) >= 15 else 0


_usual = (0, 0)  # (computed at, switches per hour): your own normal, refreshed hourly


def usual_switch_rate(con):
    """Your usual switch rate over the last 14 days; 0 until there are about 3 working days of data."""
    global _usual
    if time.time() - _usual[0] > 3600:
        now = time.time()
        n = con.execute("select count(*) from samples where ts > ? and status != 'away'", (now - 14 * 86400,)).fetchone()[0]
        _usual = (now, switch_rate(con, now - 14 * 86400, now) if n >= 900 else 0)
    return _usual[1]


ASKED = DATA / "last-project-ask"  # when we last asked to name a project that isn't on the list


def ask_new_project(con):
    """The model can only pick from the list, so after 60 'new_project' minutes in 48 hours (counted since the last
    question) ask the person what the project is. The dialog runs in a thread: the loop keeps sampling meanwhile."""
    now = int(time.time())
    since = max(now - 48 * 3600, int(ASKED.read_text()) if ASKED.exists() else 0)
    rows = con.execute("select app, title, url from samples where project = 'new_project' and ts > ?", (since,)).fetchall()
    if len(rows) < 60 or con.execute("select status from samples order by ts desc limit 1").fetchone()[0] == "away":
        return
    ASKED.write_text(str(now))  # answered or not, the next question needs 60 new minutes
    threading.Thread(target=lambda: add_project(ask_project_name(project_hints(rows))), daemon=True).start()


def project_hints(rows, n=5):
    """The most common window titles (or site + path when there's no title) among those minutes. Many have none."""
    c = Counter()
    for app, title, url in rows:
        t = re.sub(r" - \d+ new items?", "", (title or "").strip())  # Slack's unread count isn't part of the name
        if not t or t == app:
            t = re.sub(r"^\w+://(www\.)?", "", url or "").rstrip("/")
        if t:
            c[t[:70]] += 1
    return c.most_common(n)


def ask_project_name(hints):
    """A local dialog with a text field. Returns what was typed after "Add", else ""."""
    text = ("Looks like you're working on a project that isn't on your list. What is it?\n\n"
            + "".join(f"• {t} ({n} min)\n" for t, n in hints) + ("\n" if hints else "")
            + "Type name: description, e.g.\nfc-outbound: Client: Founders Capital outbound and demand-gen engine")
    r = subprocess.run(["osascript", "-e", "on run argv", "-e", "activate", "-e",
                        'set r to display dialog (item 1 of argv) with title "timelens: new project" default answer "" '
                        'buttons {"Not a project", "Add"} default button "Add" giving up after 600', "-e",
                        'if button returned of r is "Add" then return text returned of r', "-e", "end run", text],
                       capture_output=True, text=True)
    return r.stdout.strip()


def add_project(answer):
    """'name: description' -> a [projects.known] entry in the config file and in the running tracker's CFG."""
    name, _, desc = answer.partition(":")
    desc = " ".join(desc.split()) or name.strip()
    name = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not name:
        return
    lines = CONFIG.read_text().splitlines()
    entry = f"{json.dumps(name)} = {json.dumps(desc, ensure_ascii=False)}"  # a JSON string is a valid TOML string
    start = next((i for i, l in enumerate(lines) if re.fullmatch(r"\s*\[\s*projects\.known\s*\]\s*(#.*)?", l)), None)
    if start is None:
        lines += ["", "[projects.known]", entry]
    else:  # hand-edited files may have more tables after it: insert after its last entry
        end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1
        lines.insert(end, entry)
    text = "\n".join(lines) + "\n"
    assert tomllib.loads(text)["projects"]["known"][name] == desc  # round-trip check before anything is replaced
    tmp = CONFIG.with_name(CONFIG.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(CONFIG)
    CFG["projects"].setdefault("known", {})[name] = desc  # the next minute's request includes it, no restart
    print(f"{datetime.now():%F %T} added project {name}", file=sys.stderr)


# ---------- presets, demo, self-check ----------

def presets():
    return tomllib.loads((ROOT / "presets.toml").read_text())["preset"]


def use_categories(cats):
    """Point the report code at a list of {name, kind} categories (demo, preview, self-test)."""
    global CATEGORIES, COLORS, FOCUS, INTENTIONAL, DISTRACTIONS
    CATEGORIES = {c["name"]: c.get("description", "") for c in cats}
    COLORS = dict(zip(CATEGORIES, PALETTE)) | {"private": "#5b5f66", "unclear": "#d7d9dd", "pending": "#c9cdd3"}
    FOCUS = [c["name"] for c in cats if c["kind"] == "focus"]
    INTENTIONAL = [c["name"] for c in cats if c["kind"] in ("focus", "neutral")]
    DISTRACTIONS = [c["name"] for c in cats if c["kind"] == "distraction"]


def use_preset(preset_id):
    use_categories(next(p for p in presets() if p["id"] == preset_id)["category"])


def demo(out, cats=None, projects=None):
    """Five weeks of invented days for any set of categories and projects, rendered to HTML. No API, no screen.
    Without arguments: the 'deep_work' preset and an invented engineer's projects (used for the README images)."""
    import random
    global DATA, DB
    cats = cats or next(p for p in presets() if p["id"] == "deep_work")["category"]
    projects = projects or ["pebble-api", "tidepool", "field-notes"]
    use_categories(cats)
    focus = FOCUS or [c["name"] for c in cats][:1]
    neutral = [c["name"] for c in cats if c["kind"] == "neutral"] or focus
    drift = DISTRACTIONS or neutral
    DATA, DB = Path(out), Path(out) / "log.sqlite"
    DATA.mkdir(parents=True, exist_ok=True)
    DB.unlink(missing_ok=True)
    con, rnd = db(), random.Random(7)
    last_monday = date.today() - timedelta(days=date.today().weekday() + 7)  # last full week, plus 4 weeks of history
    deep = [(focus[i % len(focus)], projects[i % len(projects)], m) for i, m in enumerate((95, 60, 70, 50))]
    morning = [(neutral[i % len(neutral)], "none", m) for i, m in enumerate((15, 25, 20))]
    afternoon = [(neutral[0], projects[0], 30), (neutral[-1], "none", 20), (focus[0], projects[-1], 45),
                 (focus[-1], projects[1 % len(projects)], 40), (neutral[len(neutral) // 2], "none", 25)]
    for d in range(35):
        day = last_monday - timedelta(days=28) + timedelta(days=d)
        if day.weekday() == 6:
            continue
        t = datetime.combine(day, datetime.min.time()).timestamp() + (8 * 60 + rnd.randint(40, 80)) * 60
        plan = morning + rnd.sample(deep, 4) + ["lunch"] + rnd.sample(afternoon, 5)
        if day.weekday() == 5:  # Saturday: a little side work, a lot of drifting
            plan = [(drift[0], "none", 25), (focus[-1], projects[-1], 80), (drift[-1], "none", 30)]
        for b in plan:
            if b == "lunch":
                t += rnd.randint(45, 70) * 60
                continue
            cat, proj, mins = b
            for _ in range(max(5, int(mins * rnd.uniform(0.7, 1.3)))):
                c, p = cat, proj
                if cat not in drift and rnd.random() < (0.03 if t % 86400 < 46800 else 0.07):
                    c, p = rnd.choice([(rnd.choice(drift), "none"), (neutral[0], "none")])  # afternoons get choppier
                con.execute("insert or replace into samples values(?, 'ok', 'demo', 'demo', '', NULL, ?, ?, ?, 0.8, '{}', 2350)",
                            (int(t), c, rnd.uniform(0.5, 0.99), p))
                t += 60
    con.commit()
    path, _ = report(last_monday + timedelta(days=3), con)  # Thursday + its week
    print(DATA / "reports")
    return path


def selftest():
    use_preset("deep_work")
    W, D, M = ("deep_work", "pebble-api"), ("distractions", "none"), ("messages", "none")
    mins = [None] * 1440
    mins[540:600] = [W] * 60          # 09:00-10:00 deep work
    mins[600:602] = [M] * 2           # 2-minute message check: tolerated
    mins[602:640] = [W] * 38          # back to work -> one 100-minute block
    mins[640:643] = [D] * 3           # 3 minutes of distraction: breaks the block
    mins[643:650] = [W] * 7
    mins[700:705] = [D] * 5           # second distraction after an away gap
    m = metrics(mins)
    assert m["block"] == (100, 540, 639, "pebble-api"), m["block"]
    assert m["switches"] == 2, m["switches"]
    assert m["active"] == 60 + 2 + 38 + 3 + 7 + 5
    assert focus_block([W, ("deep_work", "tidepool")] + [None]) == (1, 0, 0, "pebble-api")
    assert all(p["category"][-1]["kind"] == "none" for p in presets())  # every preset ends with the catch-all
    assert redact("Inbox - jo@acme.com - Gmail") == "Inbox - [email] - Gmail"
    assert redact("Call (555) 123-4567") == "Call [phone]" and redact("Pay 4111 1111 1111 1111") == "Pay [number]"
    assert redact("Sprint 2026-11-07 · 14:30 · v1.14.0 · PR #4411") == "Sprint 2026-11-07 · 14:30 · v1.14.0 · PR #4411"
    assert redact_url("https://x.com/search?q=secret#top") == "https://x.com/search"
    print("selftest ok")


# ---------- MCP server ----------
# `timelens mcp`: lets your own agent (Claude Code, Codex, Claude Desktop…) read the log. Stdio, newline-delimited
# JSON-RPC, standard library only. Read-only: the log is opened with mode=ro and nothing is ever written.

def span(a, b):
    return {"start": hhmm(a), "end": hhmm(b + 1), "minutes": b - a + 1}


def day_json(con, d):
    mins = day_minutes(con, d)[0]
    m, on = metrics(mins), [i for i, x in enumerate(mins) if x]
    stretches = []  # runs of time at the Mac; 10+ minutes away is a break
    for i in on:
        if stretches and i - stretches[-1][1] <= 10:
            stretches[-1][1] = i
        else:
            stretches.append([i, i])
    start = datetime.combine(d, datetime.min.time()).timestamp()
    rows = con.execute("select ts, status, app from samples where ts >= ? and ts < ? order by ts", (start, start + 86400)).fetchall()
    hops = [ts for (_, s0, a0), (ts, s1, a1) in zip(rows, rows[1:]) if "away" not in (s0, s1) and a0 != a1]
    length, s, e, p = m["block"]
    return {"date": str(d), "minutes_at_laptop": m["active"],
            "first_active": hhmm(on[0]) if on else None, "last_active": hhmm(on[-1]) if on else None,
            "categories": dict(m["cats"].most_common()), "projects": dict(m["projects"].most_common()),
            "longest_focus_block": {**span(s, e), "project": p} if length else None, "distraction_switches": m["switches"],
            "intentional_share": round(m["intentional"], 3), "stretches_between_breaks": [span(a, b) for a, b in stretches],
            "app_switches": len(hops), "app_switches_per_active_hour": round(len(hops) * 60 / m["active"], 1) if m["active"] else 0,
            "app_switches_by_hour": dict(sorted(Counter(f"{datetime.fromtimestamp(t):%H}" for t in hops).items()))}


def range_json(con, start, end):
    a, b = date.fromisoformat(start), date.fromisoformat(end)
    days = [a + timedelta(days=k) for k in range((b - a).days + 1)]
    if not 0 < len(days) <= 92:
        raise ValueError("end must be on or after start, and the range at most 92 days")
    m = metrics([x for d in days for x in day_minutes(con, d)[0]])
    return {"days": [day_json(con, d) for d in days], "totals": {
        "minutes_at_laptop": m["active"], "categories": dict(m["cats"].most_common()), "projects": dict(m["projects"].most_common()),
        "distraction_switches": m["switches"], "intentional_share": round(m["intentional"], 3)}}


def minutes_json(con, start, end, app=None, activity=None, project=None, limit=500):
    lo = datetime.fromisoformat(start).timestamp()
    hi = datetime.fromisoformat(end).timestamp() + (86400 if len(end) == 10 else 0)  # a bare date includes that whole day
    where, args = "ts >= ? and ts < ? and status != 'away'", [lo, hi]
    for col, v in (("app", app and f"%{app}%"), ("activity", activity), ("project", project)):
        if v:
            where, args = where + f" and {col} {'like' if col == 'app' else '='} ?", args + [v]
    total = con.execute(f"select count(*) from samples where {where}", args).fetchone()[0]
    rows = con.execute(f"select ts, status, app, title, url, activity, activity_conf, project, project_conf from samples "
                       f"where {where} order by ts limit ?", args + [max(1, min(int(limit), 5000))]).fetchall()
    return {"columns": ["time", "status", "app", "title", "url", "activity", "activity_conf", "project", "project_conf"],
            "rows": [[f"{datetime.fromtimestamp(r[0]):%F %H:%M}", *r[1:6], r[6] and round(r[6], 2), r[7], r[8] and round(r[8], 2)]
                     for r in rows], "total": total, "truncated": total > len(rows)}


def projects_json():
    kinds = {"focus": FOCUS, "intentional": INTENTIONAL, "distraction": DISTRACTIONS}
    return {"projects": projects(), "min_confidence": CFG["min_confidence"], "categories": {
        c: {"description": v, "kinds": [k for k, cs in kinds.items() if c in cs]} for c, v in CATEGORIES.items()}}


DATE = {"type": "string", "description": "YYYY-MM-DD, local time"}
WHEN = {"type": "string", "description": "YYYY-MM-DD (whole day) or YYYY-MM-DDTHH:MM, local time"}
TOOLS = {  # name: (function, description for the agent, properties, required)
    "day": (lambda con, a: day_json(con, date.fromisoformat(a["date"])),
            "The user's day as in their daily report: minutes at the laptop, first/last active minute, minutes per category "
            "and per project, longest focus block, switches into distractions, intentional share, stretches between breaks "
            "(a break is 10+ minutes away) and app switches. One minute = one sample; away minutes are not counted.",
            {"date": DATE}, ["date"]),
    "range": (lambda con, a: range_json(con, a["start"], a["end"]),
              "The `day` numbers for every day from start to end (inclusive, at most 92 days), plus totals. "
              "Use it for weeks, trends and comparing days.", {"start": DATE, "end": DATE}, ["start", "end"]),
    "minutes": (lambda con, a: minutes_json(con, **a),
                "Raw minute rows, away minutes left out: time, status (ok, error, private, queued), app, window title, URL, "
                "and the model's activity and project with confidence. Use it to see what was on screen behind a total. "
                "Filters: app (substring), activity and project (exact names from `projects`).",
                {"start": WHEN, "end": WHEN, "app": {"type": "string"}, "activity": {"type": "string"},
                 "project": {"type": "string"}, "limit": {"type": "integer", "default": 500, "maximum": 5000}}, ["start", "end"]),
    "projects": (lambda con, a: projects_json(),
                 "The user's categories (description, and kind: focus, intentional, distraction) and projects (description). "
                 "Reports also show 'unclear' (answer below min_confidence), 'private' (password manager or bank, nothing "
                 "captured) and 'pending' (not labelled yet). Call it first to know what the labels mean.", {}, []),
}


def rpc(method, p):
    if method == "initialize":
        v = p.get("protocolVersion")
        return {"protocolVersion": v if v in ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05") else "2025-11-25",
                "capabilities": {"tools": {}}, "serverInfo": {"name": "timelens", "version": "0.3.0"},
                "instructions": "The user's own timelens log: one row per minute at their Mac, labelled with their "
                                "categories and projects. Read-only. Times are local."}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [{"name": n, "description": d, "annotations": {"readOnlyHint": True}, "inputSchema": {
            "type": "object", "properties": s, "required": r, "additionalProperties": False}} for n, (_, d, s, r) in TOOLS.items()]}
    if method == "tools/call" and p.get("name") in TOOLS:
        from contextlib import closing
        try:
            if not DB.exists():
                raise FileNotFoundError(f"no timelens log at {DB} yet: run `timelens setup` first")
            with closing(sqlite3.connect(DB.as_uri() + "?mode=ro", uri=True)) as con:
                return {"content": [{"type": "text", "text": json.dumps(
                    TOOLS[p["name"]][0](con, p.get("arguments") or {}), separators=(",", ":"))}]}
        except Exception as e:  # a bad date, no log yet: tell the agent and keep serving
            return {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True}
    raise LookupError(-32602, f"Unknown tool: {p.get('name')}") if method == "tools/call" else \
        LookupError(-32601, f"Method not found: {method}")


def mcp():
    out, sys.stdout = sys.stdout, sys.stderr  # stdout carries the protocol only; a stray print goes to stderr
    for line in sys.stdin:
        msg, reply = {}, None
        try:
            msg = json.loads(line)
            if not isinstance(msg, dict) or "id" in msg:  # notifications (initialized, cancelled) get no answer
                reply = {"result": rpc(msg.get("method"), msg.get("params") or {})}
        except LookupError as e:
            reply = {"error": {"code": e.args[0], "message": e.args[1]}}
        except (ValueError, AttributeError):  # not JSON, or not one JSON object
            msg, reply = {}, {"error": {"code": -32700, "message": "Parse error: send one JSON-RPC object per line"}}
        if reply:
            out.write(json.dumps({"jsonrpc": "2.0", "id": msg.get("id"), **reply}) + "\n")
            out.flush()


if __name__ == "__main__":
    cmd, args = (sys.argv[1] if len(sys.argv) > 1 else "run"), sys.argv[2:]
    if cmd == "run":
        run()
    elif cmd == "sample":
        con = db()
        sample(con, dry_run="--dry-run" in args, verbose=True)
        con.commit()
    elif cmd == "report":
        d = next((date.fromisoformat(a) for a in args if not a.startswith("-")), date.today())
        path, m = report(d, db())
        print(path)
        if "--open" in args:
            subprocess.run(["open", path])
    elif cmd == "process":  # label everything still queued (batch mode), now
        print(f"labelled {process_queue(db())} queued minutes")
    elif cmd == "selftest":
        selftest()
    elif cmd == "demo":
        demo(args[0] if args else tempfile.mkdtemp(prefix="timelens-demo-"))
    elif cmd == "mcp":
        mcp()
    else:
        print(__doc__)
