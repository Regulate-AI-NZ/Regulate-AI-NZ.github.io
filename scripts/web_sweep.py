"""Web-search sweep: classify unprocessed signatories via Brave Search + Gemini.

For each Classification row with Method == "none":
  1. Brave Search API fetches web results for the name in a New Zealand context
     (free tier: 2,000 queries/month, 1 req/sec).
  2. Free-tier Gemini judges the results under a strict no-guessing rule: if
     multiple plausible candidates exist or no clear NZ match, it's a miss.
     (Google Search *grounding* requires paid billing, but plain generation on
     the free tier does not — so search and judgment are split across APIs.)

Results are written back to the Classification tab:
  - confident match    -> Method "web",      Sector set, Confidence high/medium
  - no confident match -> Method "web-miss", Sector "unknown"
Both are preserved by classify_signatories.py across re-runs, so the sweep is
resumable and never re-searches a name. Gemini free-tier daily quota may stop
a long run early — just re-run the next day.

Keys: GEMINI_API_KEY or ~/.config/gemini/key; BRAVE_API_KEY or
~/.config/brave/key (lines starting with # are ignored).

Usage:
    python scripts/web_sweep.py --limit 25        # calibration batch
    python scripts/web_sweep.py --limit 0         # everything pending
    python scripts/web_sweep.py --dry-run         # count pending rows
"""

import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

import gspread

CREDS_FILE = "/Users/lensenandr/.config/gsheets/regulate-ai-nz.json"
SHEET_KEY = "1UrtyrRHjwH_Hi5k4-RoNjmGgD_hV74bdtB6NAJQh2OE"
CACHE_TAB = "Classification"
# Preference order; free tier load-sheds the primary model sometimes (503),
# so fall back to lite. Override with GEMINI_MODEL to pin one.
GEMINI_MODELS = ([os.environ["GEMINI_MODEL"]] if os.environ.get("GEMINI_MODEL")
                 else ["gemini-flash-latest", "gemini-flash-lite-latest"])

SECTORS = [
    "academic-research", "legal", "arts-media-creative", "tech-ai", "health",
    "education", "govt-policy-union", "business", "other",
]

PROMPT = """\
You are helping characterise signatories of a public New Zealand open letter
on AI regulation, in aggregate. Below are web search results for a signatory's
name searched with New Zealand context.

Name: {name}
{context}
Search results:
{results}

Rules — follow them strictly:
1. Only report a match if ONE clear NZ-connected person fits this name and
   the results identify their occupation from a credible source (staff page,
   professional register, IMDb, LinkedIn, news). If the results show multiple
   plausible different people, or nothing solid, report no match.
2. The person's name must match the signed name as written (ignore case and
   accents). A longer or hyphenated surname (e.g. "Jane Ward-Smith" for a
   signature "Jane Ward") is NOT a match unless the results explicitly show
   they are the same person using both forms.
3. If the only occupation evidence is a data-broker listing (ZoomInfo,
   RocketReach, SignalHire), the best you may report is confidence "medium".
4. Never guess. A wrong classification is worse than none.

If matched, pick the best sector from: {sectors}

Respond with ONLY a JSON object, no prose, no code fences:
{{"match": true/false, "sector": "<sector or null>",
  "confidence": "high"/"medium", "evidence": "<one short sentence>"}}
"""


def load_key(env_var, path):
    if os.environ.get(env_var):
        return os.environ[env_var]
    path = os.path.expanduser(path)
    if os.path.exists(path):
        with open(path) as f:
            keys = [l.strip() for l in f
                    if l.strip() and not l.strip().startswith("#")]
        if keys:
            return keys[0]
    sys.exit(f"{env_var} not set and {path} not found/empty")


def brave_search(api_key, name):
    query = f'"{name}" New Zealand'
    url = ("https://api.search.brave.com/res/v1/web/search?"
           + urllib.parse.urlencode({"q": query, "count": 8}))
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "X-Subscription-Token": api_key,
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    results = data.get("web", {}).get("results", [])
    lines = []
    for r in results:
        desc = re.sub(r"<[^>]+>", "", r.get("description", ""))
        lines.append(f"- {r.get('title', '')} | {r.get('url', '')}\n  {desc}")
    return "\n".join(lines)


_active_model = [None]  # sticky: once a model works, stop retrying the others


def gemini_judge(client, name, results_text, context=""):
    contents = PROMPT.format(name=name, context=context,
                             results=results_text or "(no results)",
                             sectors=", ".join(SECTORS))
    # Preferred (last-working) model first, but always keep the others as
    # fallbacks; if every model is load-shedding (503), wait and retry once.
    models = ([_active_model[0]] if _active_model[0] else []) + \
             [m for m in GEMINI_MODELS if m != _active_model[0]]
    response = None
    for round_ in range(2):
        for model in models:
            try:
                response = client.models.generate_content(model=model, contents=contents)
                _active_model[0] = model
                break
            except Exception as e:
                msg = str(e)
                if "429" in msg:
                    time.sleep(45)  # RPM burst — wait out the minute window
                    continue
                if "503" in msg:
                    continue  # overloaded — try next model
                raise
        if response is not None:
            break
        if round_ == 0:
            time.sleep(20)  # every model shedding load — brief pause, retry all
    if response is None:
        raise RuntimeError("all Gemini models unavailable (503)")
    text = (response.text or "").strip()
    text = re.sub(r"^```(json)?|```$", "", text, flags=re.M).strip()
    m = re.search(r"\{.*\}", text, flags=re.S)
    if not m:
        raise ValueError(f"unparseable response: {text[:120]!r}")
    data = json.loads(m.group(0))
    if data.get("match") and data.get("sector") in SECTORS:
        conf = data.get("confidence", "medium")
        return data["sector"], ("medium" if conf not in ("high", "medium") else conf), data.get("evidence", "")
    return None, None, data.get("evidence", "")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=25,
                        help="max names to process this run (0 = no limit)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--delay", type=float, default=1.5,
                        help="seconds between names (Brave free tier is 1 req/sec, "
                             "which is the binding rate; paid Gemini has ample RPM)")
    parser.add_argument("--verify-rules", action="store_true",
                        help="re-check rule-classified rows (e.g. 'Dr' may be a "
                             "medical doctor, not academic) and fill in Detail; "
                             "keeps the rule classification when inconclusive")
    args = parser.parse_args()

    gc = gspread.service_account(filename=CREDS_FILE)
    ws = gc.open_by_key(SHEET_KEY).worksheet(CACHE_TAB)
    rows = ws.get_all_values()

    pending = []
    for i, r in enumerate(rows[1:], start=2):
        r = r + [""] * (8 - len(r))
        name, org, sector, method, detail = (r[1].strip(), r[2].strip(),
                                             r[3], r[4], r[7])
        if not name:
            continue
        if args.verify_rules:
            if method != "rule" or detail:
                continue  # only unchecked rule rows
            pending.append((i, name, org, sector, False))
        else:
            if method != "none":
                continue
            base = re.sub(r"[^a-zA-Z ]", "", name).strip()
            parts = base.split()
            hopeless = len(parts) < 2 or any(len(p) == 1 for p in parts[:2]) or not base
            pending.append((i, name, org, None, hopeless))

    n_hopeless = sum(1 for *_, h in pending if h)
    print(f"{len(pending)} unprocessed rows ({n_hopeless} unsearchable, "
          f"{len(pending) - n_hopeless} searchable)")
    if args.dry_run:
        return

    brave_key = load_key("BRAVE_API_KEY", "~/.config/brave/key")
    os.environ["GEMINI_API_KEY"] = load_key("GEMINI_API_KEY", "~/.config/gemini/key")
    from google import genai
    client = genai.Client()

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    done = hits = errors = 0
    for row_i, name, org, rule_sector, hopeless in pending:
        if args.limit and done >= args.limit:
            break
        if hopeless:
            ws.update(values=[["unknown", "web-miss", "", now,
                               "unsearchable (initials/partial name)"]],
                      range_name=f"D{row_i}:H{row_i}", raw=True)
            print(f"  {name!r}: unsearchable -> web-miss", flush=True)
            continue

        done += 1
        try:
            # strip titles/descriptors for the search query itself
            search_name = re.sub(r"^(dr|prof(essor)?|assoc\w* prof\w*|emeritus prof\w*)\.?\s+",
                                 "", name.split(",")[0].strip(), flags=re.I)
            query_extra = " " + org if org else ""
            results_text = brave_search(brave_key, search_name + query_extra)
            time.sleep(1.1)  # Brave free tier: 1 req/sec
            context = ""
            if args.verify_rules:
                context = (f'\nThey signed with affiliation "{org}".' if org else "") + (
                    f'\nOur provisional rule-based guess is "{rule_sector}" — '
                    'note a "Dr" title may mean a medical doctor, vet, or other '
                    "clinician rather than an academic. Confirm or correct.\n")
            sector, conf, evidence = gemini_judge(client, name, results_text, context)
            errors = 0
        except Exception as e:
            msg = str(e)
            print(f"  {name!r}: ERROR {msg[:160]} — leaving as none", flush=True)
            errors += 1
            if "RESOURCE_EXHAUSTED" in msg and "PerDay" in msg or errors >= 5:
                print("Stopping: repeated errors or daily quota reached. "
                      "Re-run later — progress is saved.")
                break
            time.sleep(args.delay)
            continue

        detail = (evidence or "")[:300]
        if sector:
            hits += 1
            ws.update(values=[[sector, "web", conf, now, detail]],
                      range_name=f"D{row_i}:H{row_i}", raw=True)
            flag = (" (was rule: %s)" % rule_sector
                    if args.verify_rules and sector != rule_sector else "")
            print(f"  {name!r}: {sector} ({conf}){flag} — {evidence}", flush=True)
        elif args.verify_rules:
            # inconclusive — keep the rule classification, just note the check
            ws.update(values=[["web check inconclusive; rule classification retained"]],
                      range_name=f"H{row_i}", raw=True)
            print(f"  {name!r}: inconclusive, kept rule={rule_sector}", flush=True)
        else:
            ws.update(values=[["unknown", "web-miss", "", now,
                               detail or "no confident match"]],
                      range_name=f"D{row_i}:H{row_i}", raw=True)
            print(f"  {name!r}: no confident match -> web-miss", flush=True)
        time.sleep(args.delay)

    if done:
        print(f"\nProcessed {done} searches, {hits} classified "
              f"({100 * hits / done:.0f}% hit rate)")
    print("Run scripts/classify_signatories.py --no-llm for updated totals.")


if __name__ == "__main__":
    main()
