import pandas as pd
import datetime
import os

# 1. CONFIGURATION
SHEET_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vQcBXLCS1M9sMWKTGXh3Gi6AFU_tG36BypFXvzjQUhzQl7M1oQcNiIbWJXJPLg_I5rH9CQpmmCOTzfT/pub?gid=1011415780&single=true&output=csv" 
MARKDOWN_FILE = "index.md"

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

    # Format signatures: Ensure they are stripped of whitespace then have 2 spaces for Markdown line break
    formatted_signatures = [s.strip() + "  " for s in raw_signatures if s.strip()]
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
    new_section += f"Add your signature here: [Sign]({{{{ \"/sign/\" | relative_url }}}})\n\n"
    new_section += "\n".join(formatted_signatures)
    
    # 4. SAVE CHANGES
    new_content = pre_content + new_section
    
    with open(MARKDOWN_FILE, 'w', encoding='utf-8') as f:
        f.write(new_content)

    print(f"Success! Updated {MARKDOWN_FILE} with {count} signatories.")

if __name__ == "__main__":
    main()
