#!/usr/bin/env python3
"""timelens: every minute, label what's on screen with a decision model; write daily and weekly reports.

  timelens setup                   open the setup page (goals, categories, model, permission)
  timelens report [DATE] [--open]  today's (or DATE's) report and its week
  timelens sample [--dry-run]      one sample now; --dry-run prints the request and sends nothing
  timelens process                 label every queued minute now (on-this-Mac mode)
  timelens demo [DIR]              render an invented week, no API and no screen access
  timelens selftest
  timelens run                     sample forever (launchd runs this for you)

Your setup lives in ~/Library/Application Support/timelens/config.toml ($TIMELENS_CONFIG overrides it).
"""
import base64, html, json, os, re, sqlite3, subprocess, sys, tempfile, time, tomllib, urllib.error, urllib.request, uuid
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
    for d in cfg["projects"]["dirs"]:
        for sub in sorted(Path(d).expanduser().glob("*/")):
            if not sub.name.startswith("."):
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
        QUEUE.mkdir(mode=0o700, parents=True, exist_ok=True)
        (QUEUE / f"{ts}.json").write_text(json.dumps({"app": app, "bundle": bundle, "title": title, "url": url}))
        if jpeg:
            (QUEUE / f"{ts}.jpg").write_bytes(jpeg)
        return con.execute("insert or replace into samples(ts, status, app, bundle, title, url) values(?, 'queued', ?, ?, ?, ?)",
                           (ts, app, bundle, title, url))
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
        return con.execute("insert or replace into samples(ts, status, app, bundle, title, url) values(?, 'error', ?, ?, ?, ?)",
                           (ts, app, bundle, title, url))
    if verbose:
        print(json.dumps(r, indent=2))
    store(con, ts, app, bundle, title, url, r)


def store(con, ts, app, bundle, title, url, r):
    a = r["answers"]
    con.execute("insert or replace into samples values(?, 'ok', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, app, bundle, title, url, a["activity"]["choice"], a["activity"]["confidence"],
                 a["project"]["choice"], a["project"]["confidence"], json.dumps(a), r.get("usage", {}).get("input_tokens")))


def process_queue(con, limit=None):
    """Label queued minutes (batch mode), oldest first. Each screenshot is deleted as soon as its minute is labelled."""
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
            if CFG.get("processing") == "batch" and good_time_to_process():
                process_queue(con, CFG.get("batch_per_minute", 4))
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


def notify(m, week):
    text = f"{dur(m['active'])} on the laptop · {m['intentional']:.0%} intentional"
    subprocess.run(["osascript", "-e", f'display notification "{text}" with title "timelens: your {"week" if week else "day"}"'])


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
        if "--notify" in args:
            notify(m, d.weekday() == 6)
        if "--open" in args:
            subprocess.run(["open", path])
    elif cmd == "process":  # label everything still queued (batch mode), now
        print(f"labelled {process_queue(db())} queued minutes")
    elif cmd == "selftest":
        selftest()
    elif cmd == "demo":
        demo(args[0] if args else tempfile.mkdtemp(prefix="timelens-demo-"))
    else:
        print(__doc__)
