"""Audit the signatory Google Sheet for duplicates and junk entries.

Report-only: prints findings with sheet row numbers so they can be fixed in
"Form responses 1" (the source of truth — index.md is regenerated from it by
update_signatories.py). This preserves the cleanup logic used for the
July 2026 audit that removed ~20 duplicate/garbage rows.

Checks:
  - near-duplicate names (same person with/without title, affiliation,
    extra whitespace, or accents)
  - public signatories who are already Expert Signatories in index.md
  - junk: timestamps-as-names, title-only names, stray backticks,
    double spaces, N/A-style affiliations, name duplicated into org field

Usage: python scripts/audit_signatories.py
"""

import re
import unicodedata
from collections import defaultdict

import gspread

CREDS_FILE = "/Users/lensenandr/.config/gsheets/regulate-ai-nz.json"
SHEET_KEY = "1UrtyrRHjwH_Hi5k4-RoNjmGgD_hV74bdtB6NAJQh2OE"
MARKDOWN_FILE = "index.md"

TITLES = r"^(dr|prof|professor|associate professor|emeritus professor|mr|mrs|ms|miss|master|rev|hon)\.?\s+"
POSTNOMINALS = r"\s+(phd|mph|med|mpp|msc|mls|bvsc|kc|qc|ca|md|nzcs|ampas|mscw \(applied\)|ba \(hons\)|llb.*)$"


def base_name(name):
    s = unicodedata.normalize("NFKD", name.strip())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("`", "").replace("’", "'")
    s = re.sub(r"\s+", " ", s).strip().casefold()
    prev = None
    while prev != s:
        prev = s
        s = re.sub(TITLES, "", s)
    s = re.sub(POSTNOMINALS, "", s)
    return s.strip()


def main():
    gc = gspread.service_account(filename=CREDS_FILE)
    ss = gc.open_by_key(SHEET_KEY)
    rows = ss.worksheet("Form responses 1").get_all_values()[1:]

    entries = []  # (sheet_row, name, org)
    for i, r in enumerate(rows, start=2):
        name = (r[1] if len(r) > 1 else "").strip()
        org = (r[4] if len(r) > 4 else "").strip()
        if name:
            entries.append((i, name, org))

    print("=== Near-duplicate names (same normalized base) ===")
    groups = defaultdict(list)
    for row, name, org in entries:
        groups[base_name(name)].append((row, name, org))
    for base, g in groups.items():
        if len(g) > 1:
            # exact (name, org) repeats are already deduped downstream, but
            # flag them too so the source can be cleaned
            for row, name, org in g:
                print(f"  row {row}: {name!r}  org={org!r}")
            print()

    print("=== Public signatories already in the Expert list ===")
    experts = set()
    try:
        with open(MARKDOWN_FILE, encoding="utf-8") as f:
            content = f.read()
        expert_block = content.split("### Expert Signatories")[1].split("###")[0]
        for line in expert_block.strip().splitlines():
            line = line.strip()
            if line:
                experts.add(base_name(line.split(",")[0]))
    except (FileNotFoundError, IndexError):
        print("  (could not parse index.md — run from the repo root)")
    for row, name, org in entries:
        if base_name(name.split(",")[0]) in experts:
            print(f"  row {row}: {name!r}  org={org!r}")

    print("\n=== Junk / error entries ===")
    for row, name, org in entries:
        flags = []
        if re.fullmatch(r"[\d./: ]+", name):
            flags.append("timestamp-as-name")
        if name in ("Ms", "Mr", "Mrs", "Dr", "Miss", "Prof"):
            flags.append("title-only")
        if "`" in name:
            flags.append("stray backtick")
        if re.search(r"\s{2,}", name):
            flags.append("double space")
        if org and org.strip().casefold() in ("n/a", "na", "no", "none", "-"):
            flags.append("junk org")
        if org and org.strip().casefold() == name.strip().casefold():
            flags.append("name repeated as org")
        if len(base_name(name).split()) == 1 and not re.fullmatch(r"[\d./: ]+", name):
            flags.append("single-word name")
        if flags:
            print(f"  row {row}: {name!r} org={org!r}  -> {', '.join(flags)}")

    print("\nDone. Fix rows in 'Form responses 1'; index.md regenerates via "
          "scripts/update_signatories.py (published CSV caches ~2-5 min).")


if __name__ == "__main__":
    main()
