"""Web-search sweep: classify unprocessed signatories via Gemini + Google Search.

For each Classification row with Method == "none", asks Gemini (with the
google_search grounding tool) to identify the person in a New Zealand context
and assign a sector — with a strict no-guessing rule: if the name has multiple
plausible candidates or no clear NZ match, the result is a miss.

Results are written back to the Classification tab:
  - confident match  -> Method "web",      Sector set, Confidence high/medium
  - no confident match -> Method "web-miss", Sector "unknown"
Both are preserved by classify_signatories.py across re-runs, so the sweep is
resumable and never re-searches a name.

Requires GEMINI_API_KEY in the environment (create at aistudio.google.com/apikey;
attach the key's project to the billing account holding the Google AI Pro
monthly credits if using paid quota).

Usage:
    python scripts/web_sweep.py --limit 25        # process 25 names
    python scripts/web_sweep.py --limit 0         # process everything pending
    python scripts/web_sweep.py --dry-run         # just count pending rows
"""

import argparse
import datetime
import json
import os
import re
import sys
import time

import gspread

CREDS_FILE = "/Users/lensenandr/.config/gsheets/regulate-ai-nz.json"
SHEET_KEY = "1UrtyrRHjwH_Hi5k4-RoNjmGgD_hV74bdtB6NAJQh2OE"
CACHE_TAB = "Classification"
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")

SECTORS = [
    "academic-research", "legal", "arts-media-creative", "tech-ai", "health",
    "education", "govt-policy-union", "business", "other",
]

PROMPT = """\
You are helping characterise signatories of a public New Zealand open letter
on AI regulation, in aggregate. Search the web for this person:

Name: {name}

Rules — follow them strictly:
1. Search for the person in a New Zealand context (they signed an NZ letter).
2. Only report a match if ONE clear NZ-connected person fits this name and
   you can identify their occupation from a credible source (staff page,
   professional register, IMDb, LinkedIn, news). If the name is common and
   multiple plausible people exist, or you find nothing solid, report no_match.
3. Never guess. A wrong classification is worse than none.

If matched, pick the best sector from: {sectors}

Respond with ONLY a JSON object, no prose, no code fences:
{{"match": true/false, "sector": "<sector or null>",
  "confidence": "high"/"medium", "evidence": "<one short sentence>"}}
"""


def classify_name(client, name):
    from google.genai import types

    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=PROMPT.format(name=name, sectors=", ".join(SECTORS)),
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
        ),
    )
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
    parser.add_argument("--delay", type=float, default=2.0,
                        help="seconds between API calls (rate limiting)")
    args = parser.parse_args()

    gc = gspread.service_account(filename=CREDS_FILE)
    ws = gc.open_by_key(SHEET_KEY).worksheet(CACHE_TAB)
    rows = ws.get_all_values()

    pending = []
    for i, r in enumerate(rows[1:], start=2):
        name = r[1].strip()
        if r[4] != "none" or not name:
            continue
        # skip structurally hopeless entries (initials-only, single word,
        # numeric) — mark them web-miss without spending a search
        base = re.sub(r"[^a-zA-Z ]", "", name).strip()
        parts = base.split()
        hopeless = len(parts) < 2 or any(len(p) == 1 for p in parts[:2]) or not base
        pending.append((i, name, hopeless))

    n_hopeless = sum(1 for _, _, h in pending if h)
    print(f"{len(pending)} unprocessed rows ({n_hopeless} unsearchable, "
          f"{len(pending) - n_hopeless} searchable)")
    if args.dry_run:
        return

    if not os.environ.get("GEMINI_API_KEY"):
        sys.exit("GEMINI_API_KEY not set — create one at aistudio.google.com/apikey")
    from google import genai
    client = genai.Client()

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    done = hits = 0
    for row_i, name, hopeless in pending:
        if args.limit and done >= args.limit:
            break
        if hopeless:
            ws.update(values=[["unknown", "web-miss", "", now]],
                      range_name=f"D{row_i}:G{row_i}", raw=True)
            print(f"  {name!r}: unsearchable -> web-miss")
            continue

        done += 1
        try:
            sector, conf, evidence = classify_name(client, name)
        except Exception as e:
            print(f"  {name!r}: ERROR {e} — leaving as none")
            time.sleep(args.delay)
            continue

        if sector:
            hits += 1
            ws.update(values=[[sector, "web", conf, now]],
                      range_name=f"D{row_i}:G{row_i}", raw=True)
            print(f"  {name!r}: {sector} ({conf}) — {evidence}")
        else:
            ws.update(values=[["unknown", "web-miss", "", now]],
                      range_name=f"D{row_i}:G{row_i}", raw=True)
            print(f"  {name!r}: no confident match -> web-miss")
        time.sleep(args.delay)

    print(f"\nProcessed {done} searches, {hits} classified "
          f"({100 * hits / done:.0f}% hit rate)" if done else "\nNothing searched")
    print("Run scripts/classify_signatories.py --no-llm for updated totals.")


if __name__ == "__main__":
    main()
