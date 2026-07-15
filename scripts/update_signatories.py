import pandas as pd
import datetime
import os

# 1. CONFIGURATION
SHEET_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vQcBXLCS1M9sMWKTGXh3Gi6AFU_tG36BypFXvzjQUhzQl7M1oQcNiIbWJXJPLg_I5rH9CQpmmCOTzfT/pub?gid=1011415780&single=true&output=csv" 
MARKDOWN_FILE = "index.md"

# Sector figures for the compact banner. Sourced from the private Classification
# sheet (scripts/classify_signatories.py); update alongside a re-classification.
# Widths are percentages of the largest sector. Keep the label the "as of" month
# in sync with the classification date.
BANNER_SECTORS = [
    ("Arts, media &amp; creative", 278, 100),
    ("Academia &amp; research", 150, 54),
    ("Govt, policy &amp; unions", 81, 29),
    ("Technology &amp; AI", 79, 28),
    ("Business", 71, 26),
    ("Health", 53, 19),
    ("Education", 48, 17),
    ("Law", 28, 10),
]
BANNER_ASOF = "July 2026"


def floor_to_ten(value):
    """Return value rounded down to the nearest 10."""
    return (int(value) // 10) * 10


def make_banner(total):
    """Return the compact signatory banner as a single blank-line-free HTML
    block (kramdown passes contiguous top-level HTML through untouched)."""
    css = (
        ".sig-banner{--b-ink:#101418;--b-ink2:#4d545c;--b-muted:#8a9096;"
        "--b-accent:#1c5cab;--b-bar:#2a78d6;--b-track:#e4eefb;--b-surface:#f4f7fb;"
        "--b-hair:#d7ddea;font-family:system-ui,-apple-system,'Segoe UI',sans-serif;"
        "background:var(--b-surface);border:1px solid var(--b-hair);border-radius:10px;"
        "padding:22px 24px;margin:0 0 28px;display:flex;gap:28px;flex-wrap:wrap;align-items:center}"
        ".sig-banner *{box-sizing:border-box}.sig-banner .lead{flex:0 0 auto}"
        ".sig-banner .num{font-size:46px;font-weight:800;letter-spacing:-.03em;line-height:1;color:var(--b-accent)}"
        ".sig-banner .num small{display:block;font-size:13px;font-weight:600;letter-spacing:0;color:var(--b-ink2);margin-top:6px;max-width:200px}"
        ".sig-banner .bars{flex:1 1 320px;min-width:280px}"
        ".sig-banner .bars h4{margin:0 0 10px;font-size:11px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--b-muted)}"
        ".sig-banner .brow{display:grid;grid-template-columns:132px 1fr auto;align-items:center;gap:10px;margin:0 0 5px;font-size:12.5px}"
        ".sig-banner .brow .bl{color:var(--b-ink2);text-align:right;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}"
        ".sig-banner .brow .bt{display:block;background:var(--b-track);border-radius:3px;height:12px;overflow:hidden}"
        ".sig-banner .brow .bf{display:block;background:var(--b-bar);height:100%;border-radius:0 3px 3px 0}"
        ".sig-banner .brow .bv{font-weight:700;color:var(--b-ink);font-variant-numeric:tabular-nums;min-width:30px;text-align:right}"
        ".sig-banner .note{flex-basis:100%;margin:2px 0 0;font-size:11px;color:var(--b-muted)}"
        "@media (prefers-color-scheme:dark){.sig-banner{--b-ink:#f2f4f2;--b-ink2:#c3c2b7;"
        "--b-accent:#6aa5ec;--b-bar:#3987e5;--b-track:#243244;--b-surface:#171b21;--b-hair:#2c333d}}"
        "@media (max-width:520px){.sig-banner .brow{grid-template-columns:96px 1fr auto}}"
    )
    rows = "".join(
        f'<div class="brow"><span class="bl">{label}</span>'
        f'<span class="bt"><span class="bf" style="width:{width}%"></span></span>'
        f'<span class="bv">{floor_to_ten(n)}+</span></div>'
        for label, n, width in BANNER_SECTORS
    )
    return (
        '<div class="sig-banner"><style>' + css + '</style>'
        '<div class="lead"><div class="num">' + f"{total:,}"
        + '<small>public signatories and counting, from every corner of working life</small></div></div>'
        '<div class="bars"><h4>Sectors represented (' + BANNER_ASOF + ')</h4>'
        + rows + '</div></div>'
    )


def main():
    print("Fetching data from Google Sheets...")
    try:
        # Load the CSV directly from the URL
        df = pd.read_csv(SHEET_URL)
    except Exception as e:
        print(f"Error fetching data: {e}")
        exit(1)

    # 2. CLEAN DATA
    # Identify the column (assuming it's the first one or named 'Formatted...')
    col_name = "Formatted for markdown copy paste"
    
    # Filter out empty rows or garbage header rows like "Yes, keep me informed."
    if col_name in df.columns:
        # Remove the specific checkbox row if it exists
        df = df[df[col_name] != "Yes, keep me informed."]
        df = df.dropna(subset=[col_name])
        
        # Extract the list
        raw_signatures = df[col_name].tolist()
    else:
        print(f"Column '{col_name}' not found. Check your CSV format.")
        exit(1)

    # Format signatures and remove duplicates while preserving first-seen order.
    # Deduplication is case-insensitive and whitespace-normalized.
    seen = set()
    unique_signatures = []
    for signature in raw_signatures:
        cleaned = signature.strip()
        if not cleaned:
            continue

        dedupe_key = cleaned.casefold()
        if dedupe_key in seen:
            continue

        seen.add(dedupe_key)
        unique_signatures.append(cleaned)

    # Add 2 spaces for Markdown line breaks
    formatted_signatures = [s + "  " for s in unique_signatures]
    count = len(formatted_signatures)
    
    # 3. UPDATE MARKDOWN FILE
    if not os.path.exists(MARKDOWN_FILE):
        print(f"{MARKDOWN_FILE} not found!")
        exit(1)

    with open(MARKDOWN_FILE, 'r', encoding='utf-8') as f:
        content = f.read()

    # Define the marker where the Public Signatories section begins
    # We split the file here and only rewrite the part AFTER this marker
    marker = "### Public Signatories"
    
    if marker not in content:
        print(f"Could not find section '{marker}' in {MARKDOWN_FILE}")
        exit(1)

    # Split the file: Keep everything before the marker (Expert/Political signatories)
    pre_content, _ = content.split(marker, 1)

    # Generate the new section
    today = datetime.datetime.now().strftime("%d/%m/%y")

    new_section = f"{marker} ({count} and counting)\n\n"
    new_section += f"_As of {today}_\n\n"
    new_section += make_banner(count) + "\n\n"
    new_section += f"Add your signature here: [Sign]({{{{ \"/sign/\" | relative_url }}}})" + "\n\n"
    new_section += "If you want to be added as an Expert Signatory (e.g. you are involved in AI research, oversight, or governance), please email Andrew or one of the other authors to arrange this.  \n\n"
    new_section += "\n".join(formatted_signatures)
    
    # 4. SAVE CHANGES
    new_content = pre_content + new_section
    
    with open(MARKDOWN_FILE, 'w', encoding='utf-8') as f:
        f.write(new_content)

    print(f"Success! Updated {MARKDOWN_FILE} with {count} signatories.")

if __name__ == "__main__":
    main()
