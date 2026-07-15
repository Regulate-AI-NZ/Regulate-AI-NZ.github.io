"""Characterise public signatories by sector (academic, legal, arts, ...).

Reads "Form responses 1" from the signatories Google Sheet, classifies each
signatory using (in order of preference):

  1. manual  - a human-edited row in the Classification tab (never overwritten)
  2. rule    - deterministic rules on title/post-nominals, organisation
               keywords, and email domain (email never leaves this machine)
  3. llm     - Claude classifies the *organisation string only* (no names or
               emails are sent to the API)
  4. unknown - no signal available

Results are cached in a "Classification" tab in the same spreadsheet, keyed by
timestamp|name so re-runs only process new signatories. Aggregate stats are
printed; per-person rows stay private in the sheet.

Usage:
    python scripts/classify_signatories.py            # incremental run
    python scripts/classify_signatories.py --no-llm   # rules only, no API calls
"""

import argparse
import datetime
import json
import re
import sys

import gspread

CREDS_FILE = "/Users/lensenandr/.config/gsheets/regulate-ai-nz.json"
SHEET_KEY = "1UrtyrRHjwH_Hi5k4-RoNjmGgD_hV74bdtB6NAJQh2OE"
SOURCE_TAB = "Form responses 1"
CACHE_TAB = "Classification"
CACHE_HEADER = ["Key", "Name", "Org", "Sector", "Method", "Confidence",
                "ClassifiedAt", "Detail"]

SECTORS = [
    "academic-research",
    "legal",
    "arts-media-creative",
    "tech-ai",
    "health",
    "education",
    "govt-policy-union",
    "business",
    "other",
    "unknown",
]

# --- Tier 1: deterministic rules ------------------------------------------

TITLE_RULES = [
    (r"\b(professor|prof\.?|emeritus)\b", "academic-research", "high"),
    (r"\b(kc|qc)\b|\bllb\b|\bllm\b", "legal", "high"),
    (r"\bbvsc\b|\bmbchb\b|\bmd\b|\brn\b|\bmph\b", "health", "high"),
    (r"\bnzcs\b|\bampas\b", "arts-media-creative", "high"),
    (r"\bmed\b|\bmscw\b", "education", "medium"),
    (r"\bca\b", "business", "medium"),
    # Dr alone usually signals a PhD in this list; medium confidence.
    (r"^dr\.?\s", "academic-research", "medium"),
]

ORG_RULES = [
    (r"universit|wānanga|waka|taumata rau|whare wānanga|polytech|\bara\b|te pūkenga|school of|faculty|inria|research", "academic-research", "high"),
    (r"\blaw\b|legal|chambers|barrister|solicitor", "legal", "high"),
    (r"film|screen|cinema|movie|actor|director|writer|author|guild|playmarket|music|band|record|studio|production|photograph|media|publish|theatre|theater|gallery|artist|illustrat|design|creative|spada|dega|menza|storytelling|book", "arts-media-creative", "high"),
    (r"\bai\b|artificial|software|digital|\btech\b|comput|\bdata\b|cloud|cyber|algorithm|robot|quantum", "tech-ai", "high"),
    (r"health|medical|clinic|counsell|psycholog|therap|hospital|nursing", "health", "high"),
    (r"union|psa\b|kauae kaimahi|trade union|workers", "govt-policy-union", "high"),
    (r"ministry|government|council|public service|policy|parliament", "govt-policy-union", "medium"),
    (r"school\b|kura\b|college|kindergarten|education", "education", "medium"),
    (r"ltd|limited|consult|ventures|company|group|enterprises", "business", "low"),
]

EMAIL_DOMAIN_RULES = [
    (r"\.ac\.nz$|\.edu(\.[a-z]+)?$", "academic-research", "high"),
    (r"\.govt\.nz$|\.parliament\.nz$", "govt-policy-union", "high"),
    (r"\.school\.nz$", "education", "high"),
    (r"\.health\.nz$", "health", "high"),
]


def rule_classify(name, org, email):
    n = " " + name.strip().casefold() + " "
    for pattern, sector, conf in TITLE_RULES:
        if re.search(pattern, n.strip()):
            return sector, conf
    # Org keywords apply to the org field, and to any descriptor the signer
    # appended after a comma in the name field ("Jane Doe, MSc ... VUW").
    org_texts = [org.strip().casefold()] if org else []
    if "," in name:
        org_texts.append(name.split(",", 1)[1].casefold())
    for text in org_texts:
        for pattern, sector, conf in ORG_RULES:
            if re.search(pattern, text):
                return sector, conf
    if email and "@" in email:
        domain = email.rsplit("@", 1)[1].strip().casefold()
        for pattern, sector, conf in EMAIL_DOMAIN_RULES:
            if re.search(pattern, domain):
                return sector, conf
    return None, None


# --- Tier 2: LLM on organisation strings only ------------------------------

LLM_SCHEMA = {
    "type": "object",
    "properties": {
        "classifications": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "org": {"type": "string"},
                    "sector": {"type": "string", "enum": SECTORS},
                },
                "required": ["org", "sector"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["classifications"],
    "additionalProperties": False,
}

LLM_SYSTEM = (
    "You classify New Zealand organisation names into sectors. "
    "Given a list of organisation names from an open-letter signature form, "
    "assign each one the best-fitting sector from: "
    + ", ".join(SECTORS)
    + ". Use 'unknown' only when the name carries no sector signal at all. "
    "These are mostly NZ organisations; use your knowledge of NZ institutions."
)


def _classify_chunk_api(chunk):
    """Anthropic API path — used when ANTHROPIC_API_KEY is set."""
    import anthropic

    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=8000,
        thinking={"type": "adaptive"},
        output_config={
            "effort": "low",
            "format": {"type": "json_schema", "schema": LLM_SCHEMA},
        },
        system=LLM_SYSTEM,
        messages=[
            {
                "role": "user",
                "content": "Classify these organisation names:\n"
                + "\n".join(f"- {o}" for o in chunk),
            }
        ],
    )
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)["classifications"]


def _classify_chunk_cli(chunk):
    """Claude Code headless path — uses the Pro subscription, no API key."""
    import subprocess

    prompt = (
        LLM_SYSTEM
        + "\n\nClassify these organisation names:\n"
        + "\n".join(f"- {o}" for o in chunk)
        + '\n\nRespond with ONLY a JSON object, no prose and no code fences: '
        '{"classifications": [{"org": "...", "sector": "..."}, ...]}'
    )
    proc = subprocess.run(
        ["claude", "-p", prompt], capture_output=True, text=True, timeout=600
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude CLI failed: {proc.stderr.strip()[:200]}")
    text = proc.stdout.strip()
    # Tolerate a stray code fence despite the instruction.
    text = re.sub(r"^```(json)?|```$", "", text, flags=re.M).strip()
    return json.loads(text)["classifications"]


def llm_classify_orgs(orgs):
    """Classify unique org strings via Claude. Returns {org: sector}.

    Uses the Anthropic API when ANTHROPIC_API_KEY is set; otherwise falls
    back to headless Claude Code (`claude -p`), covered by a Claude
    subscription."""
    import os
    import shutil

    if os.environ.get("ANTHROPIC_API_KEY"):
        classify_chunk, backend = _classify_chunk_api, "API"
    elif shutil.which("claude"):
        classify_chunk, backend = _classify_chunk_cli, "Claude Code CLI"
    else:
        raise RuntimeError(
            "no ANTHROPIC_API_KEY set and no `claude` CLI on PATH"
        )
    print(f"  (backend: {backend})")

    result = {}
    chunk_size = 50
    for i in range(0, len(orgs), chunk_size):
        chunk = orgs[i : i + chunk_size]
        for item in classify_chunk(chunk):
            if item["org"] in chunk and item["sector"] in SECTORS:
                result[item["org"]] = item["sector"]
        print(f"  LLM classified {min(i + chunk_size, len(orgs))}/{len(orgs)} orgs")
    return result


# --- Cache handling ---------------------------------------------------------


def load_cache(ss):
    """Load settled classifications (manual/llm rows). Rule rows are cheap to
    recompute and 'none' rows should be retried, so neither blocks a re-run."""
    try:
        ws = ss.worksheet(CACHE_TAB)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=CACHE_TAB, rows=2000, cols=len(CACHE_HEADER))
        ws.update(values=[CACHE_HEADER], range_name="A1")
        return ws, {}
    rows = ws.get_all_values()
    cache = {}
    details = {}  # Detail is sticky for ALL rows, even recomputed ones
    for r in rows[1:]:
        if r and r[0]:
            entry = dict(zip(CACHE_HEADER, r + [""] * (len(CACHE_HEADER) - len(r))))
            if entry["Detail"]:
                details[r[0]] = entry["Detail"]
            # web-miss = web-searched, no confident match found; kept so future
            # sweeps know not to re-search. Sector stays "unknown".
            if entry["Method"] in ("manual", "self", "org-name", "email-domain",
                                   "web-found", "web-no-match",
                                   # legacy names, pre-July-2026 rows
                                   "llm", "domain", "web", "web-miss"):
                cache[r[0]] = entry
    return ws, cache, details


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-llm", action="store_true", help="skip the Claude tier")
    args = parser.parse_args()

    gc = gspread.service_account(filename=CREDS_FILE)
    ss = gc.open_by_key(SHEET_KEY)
    source = ss.worksheet(SOURCE_TAB)
    rows = source.get_all_values()[1:]

    cache_ws, cache, details = load_cache(ss)
    print(f"{len(rows)} signatories, {len(cache)} settled in cache (manual/llm)")

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    out_rows = []  # full rewritten Classification tab
    pending_llm = []  # (index into out_rows, org) needing the LLM tier

    for r in rows:
        ts = r[0] if len(r) > 0 else ""
        name = (r[1] if len(r) > 1 else "").strip()
        email = (r[2] if len(r) > 2 else "").strip()
        org = (r[4] if len(r) > 4 else "").strip()
        if not name:
            continue
        key = f"{ts}|{name}"

        if key in cache:  # manual override or previous settled result
            c = cache[key]
            out_rows.append([key, name, org, c["Sector"], c["Method"],
                             c["Confidence"], c["ClassifiedAt"], c["Detail"]])
            continue

        detail = details.get(key, "")
        sector, conf = rule_classify(name, org, email)
        if sector:
            out_rows.append([key, name, org, sector, "auto-rule", conf, now, detail])
        elif org and not args.no_llm:
            out_rows.append([key, name, org, "unknown", "none", "", now, detail])
            pending_llm.append((len(out_rows) - 1, org))
        else:
            out_rows.append([key, name, org, "unknown", "none", "", now, detail])

    if pending_llm:
        unique_orgs = sorted({org for _, org in pending_llm})
        print(f"Sending {len(unique_orgs)} unique org names to Claude "
              f"(org strings only — no names or emails)...")
        try:
            org_map = llm_classify_orgs(unique_orgs)
        except Exception as e:
            print(f"LLM tier failed ({e}); leaving those rows unknown for now.")
            org_map = {}
        for idx, org in pending_llm:
            sector = org_map.get(org)
            if sector:
                out_rows[idx][3:7] = [sector, "org-name", "medium", now]

    # Rewrite the tab: it's derived data, and manual rows were carried over.
    cache_ws.clear()
    cache_ws.update(values=[CACHE_HEADER] + out_rows, range_name="A1",
                    raw=True)
    print(f"Wrote {len(out_rows)} classifications to '{CACHE_TAB}' tab")

    # --- Aggregate stats (the only public-facing output) --------------------
    counts = {}
    for row in out_rows:
        counts[row[3]] = counts.get(row[3], 0) + 1

    total = sum(counts.values()) or 1
    print(f"\nSector breakdown ({total} signatories):")
    for sector, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {sector:22s} {count:5d}  ({100 * count / total:.1f}%)")


if __name__ == "__main__":
    main()
