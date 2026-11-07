#!/usr/bin/env python3
"""Compare decision models on real screenshots.

  eval.py collect MINUTES [INTERVAL_S]   save front-window screenshots + metadata to eval/samples/
  eval.py label                          blind-label them at http://localhost:8765 (keys 1-7, u = unsure)
  eval.py compare                        run every model on every sample, print accuracy/calibration/cost
"""
import json, os, statistics, sys, time, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import timelens as t

ROOT = Path(__file__).resolve().parent
EVAL = ROOT / "eval"
SAMPLES = EVAL / "samples"
LABELS = EVAL / "labels.json"
RESULTS = EVAL / "results.jsonl"
ENV = dict(l.split("=", 1) for l in (ROOT / ".env").read_text().split() if "=" in l)
CATS = list(t.CATEGORIES)

TS, OR = "https://api.typesafe.ai/v1/systemone", "https://openrouter.ai/api/alpha/decisions"
MODELS = {  # name: (url, model id, key name, $ per M input tokens if the API doesn't report cost)
    "jev-1.14 (typesafe)": (TS, "jev-1.14.0", "TYPESAFE_API_KEY", 0.042),
    "clef-flash": (OR, "cloudflare/clef-flash", "OPENROUTER_API_KEY", 0.038),
    "liquid d1": (OR, "liquid/d1", "OPENROUTER_API_KEY", 0.020),
    "pplx-decider": (OR, "perplexity/pplx-decider-v1.2", "OPENROUTER_API_KEY", 0.020),
    "gpt-6-luna": (OR, "openai/gpt-6-luna-decisions", "OPENROUTER_API_KEY", 0.100),
}


def collect(minutes, interval=60):
    SAMPLES.mkdir(parents=True, exist_ok=True)
    end = time.time() + minutes * 60
    while time.time() < end:
        t0 = time.time()
        try:
            app, bundle, title, wid = t.front_window()
            url = t.browser_url(bundle)
            private = bundle in t.CFG["privacy"]["apps"] or any(w in (url or "") for w in t.CFG["privacy"]["url_words"])
            if not t.away(None) and not private and wid:
                jpeg = t.screenshot(wid)
                if jpeg:
                    name = str(int(t0))
                    (SAMPLES / f"{name}.jpg").write_bytes(jpeg)
                    (SAMPLES / f"{name}.json").write_text(json.dumps({"app": app, "title": title, "url": url}))
                    print(name, app, title, flush=True)
        except Exception as e:
            print("error", repr(e), file=sys.stderr, flush=True)
        time.sleep(max(1, interval - (time.time() - t0)))


def samples():
    return sorted(p.stem for p in SAMPLES.glob("*.json"))


PAGE = """<!doctype html><meta charset=utf-8><title>Label</title><style>
body{font:14px -apple-system,sans-serif;margin:0;padding:16px;background:#111;color:#eee}
img{max-width:100%;max-height:72vh;display:block;margin:10px 0;border:1px solid #333}
.k span{display:inline-block;margin:0 10px 6px 0;padding:4px 8px;border:1px solid #444;border-radius:6px}
.k span.on{background:#3b6fe0;border-color:#3b6fe0}small{color:#999}</style>
<div id=top></div><img id=img><div class=k id=keys></div>
<small>1-7 label · u unsure · ← → move. Saved automatically. Judge the activity, not the app.</small>
<script>
const S=__SAMPLES__, C=__CATS__; let L=__LABELS__, i=S.findIndex(s=>!L[s]); if(i<0)i=0;
function show(){const s=S[i];fetch('/meta/'+s).then(r=>r.json()).then(m=>{
 top.innerHTML=`<b>${i+1}/${S.length}</b> · ${Object.keys(L).length} labelled · ${m.app} — ${m.title||''} <small>${m.url||''}</small>`});
 img.src='/img/'+s; keys.innerHTML=[...C,'unsure'].map((c,j)=>`<span class="${L[s]==c?'on':''}">${c=='unsure'?'u':j+1} ${c}</span>`).join('')}
function set(c){L[S[i]]=c;fetch('/label',{method:'POST',body:JSON.stringify({s:S[i],c})});if(i<S.length-1)i++;show()}
onkeydown=e=>{const n=+e.key;if(n>=1&&n<=C.length)set(C[n-1]);else if(e.key=='u')set('unsure');
 else if(e.key=='ArrowRight'&&i<S.length-1){i++;show()}else if(e.key=='ArrowLeft'&&i>0){i--;show()}};show()
</script>"""


def label():
    labels = json.loads(LABELS.read_text()) if LABELS.exists() else {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def send(self, body, ctype):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/":
                html = PAGE.replace("__SAMPLES__", json.dumps(samples())).replace("__CATS__", json.dumps(CATS))
                return self.send(html.replace("__LABELS__", json.dumps(labels)).encode(), "text/html")
            kind, name = self.path.strip("/").split("/")
            if name not in samples():
                return self.send_error(404)
            if kind == "img":
                return self.send((SAMPLES / f"{name}.jpg").read_bytes(), "image/jpeg")
            self.send((SAMPLES / f"{name}.json").read_bytes(), "application/json")

        def do_POST(self):
            d = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            labels[d["s"]] = d["c"]
            LABELS.write_text(json.dumps(labels, indent=1))
            self.send(b"ok", "text/plain")

    print("labelling at http://localhost:8765")
    HTTPServer(("127.0.0.1", 8765), H).serve_forever()


def ask(sample, model):
    url, mid, key, price = MODELS[model]
    m = json.loads((SAMPLES / f"{sample}.json").read_text())
    body = t.build_request(m["app"], m["title"], m["url"], (SAMPLES / f"{sample}.jpg").read_bytes())
    body["model"] = mid
    req = urllib.request.Request(url, json.dumps(body).encode(), {
        "Authorization": f"Bearer {ENV[key]}", "Content-Type": "application/json", "TypeSafe-Danger-Zone-Images": "true"})
    for attempt in range(3):
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                res = json.load(r)
            u = res.get("usage", {})
            return {"sample": sample, "model": model, "latency": time.time() - t0, "answers": res["answers"],
                    "tokens": u.get("input_tokens"), "cost": u.get("cost", (u.get("input_tokens") or 0) * price / 1e6)}
        except urllib.error.HTTPError as e:
            err = f"{e.code} {e.read()[:300]!r}"
            if e.code not in (429, 500, 502, 503, 529):
                break
        except Exception as e:
            err = repr(e)
        time.sleep(2 ** attempt * 2)
    return {"sample": sample, "model": model, "error": err}


def compare():
    done = {(r["sample"], r["model"]) for r in map(json.loads, RESULTS.read_text().splitlines())} if RESULTS.exists() else set()
    todo = [(s, m) for s in samples() for m in MODELS if (s, m) not in done]
    print(f"{len(todo)} calls to make")
    with ThreadPoolExecutor(8) as pool, RESULTS.open("a") as f:
        for r in pool.map(lambda sm: ask(*sm), todo):
            f.write(json.dumps(r) + "\n")
            f.flush()
            if "error" in r:
                print(r["model"], r["sample"], r["error"][:120])
    summarize()


def summarize():
    labels = {s: c for s, c in json.loads(LABELS.read_text()).items() if c != "unsure"} if LABELS.exists() else {}
    rows = [json.loads(l) for l in RESULTS.read_text().splitlines()]
    out = [f"{len(labels)} labelled samples (unsure excluded)\n",
           "| model | accuracy | acc. when conf ≥ 0.7 (share) | Brier ↓ | project = majority | median s | tokens/call | $/day (480 calls) | errors |",
           "|---|---|---|---|---|---|---|---|---|"]
    ok = [r for r in rows if "error" not in r]
    majority = {}
    for s in {r["sample"] for r in ok}:
        votes = [r["answers"]["project"]["choice"] for r in ok if r["sample"] == s]
        majority[s] = max(set(votes), key=votes.count)
    for m in MODELS:
        mine = [r for r in rows if r["model"] == m]
        good = [r for r in mine if "error" not in r]
        lab = [r for r in good if r["sample"] in labels]
        if not good:
            out.append(f"| {m} | – | – | – | – | – | – | – | {len(mine)} |")
            continue
        hit = [r["answers"]["activity"]["choice"] == labels[r["sample"]] for r in lab]
        conf = [r for r, h in zip(lab, hit) if r["answers"]["activity"]["confidence"] >= 0.7]
        conf_hit = [r["answers"]["activity"]["choice"] == labels[r["sample"]] for r in conf]
        brier = [sum((r["answers"]["activity"]["probabilities"].get(c, 0) - (c == labels[r["sample"]])) ** 2 for c in CATS) for r in lab]
        proj = [r["answers"]["project"]["choice"] == majority[r["sample"]] for r in good]
        pct = lambda xs: f"{sum(xs) / len(xs):.0%}" if xs else "–"
        out.append(f"| {m} | {pct(hit)} | {pct(conf_hit)} ({len(conf)}/{len(lab)}) | "
                   f"{statistics.mean(brier) if brier else float('nan'):.3f} | {pct(proj)} | "
                   f"{statistics.median(r['latency'] for r in good):.2f} | {statistics.mean(r['tokens'] or 0 for r in good):.0f} | "
                   f"${statistics.mean(r['cost'] or 0 for r in good) * 480:.3f} | {len(mine) - len(good)} |")
    misses = {}
    for r in ok:
        lab = labels.get(r["sample"])
        if lab and r["answers"]["activity"]["choice"] != lab:
            misses.setdefault(r["model"], []).append(f"{lab}→{r['answers']['activity']['choice']}")
    out.append("\nMistakes (truth→answer):")
    for m, xs in misses.items():
        out.append(f"- {m}: " + ", ".join(f"{k} ×{xs.count(k)}" for k in sorted(set(xs), key=xs.count, reverse=True)))
    text = "\n".join(out)
    (EVAL / "report.md").write_text(text)
    print(text)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "collect":
        collect(float(sys.argv[2]), float(sys.argv[3]) if len(sys.argv) > 3 else 60)
    elif cmd == "label":
        label()
    elif cmd == "compare":
        compare()
    elif cmd == "summary":
        summarize()
    else:
        print(__doc__)
