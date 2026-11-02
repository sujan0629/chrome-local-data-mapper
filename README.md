# Chrome Local Data Mapper (`WorkStudio`)

A standalone, zero-dependency desktop & web studio for viewing, adding, backdating, editing, and synchronizing Google Chrome SQLite `History` records (`urls`, `visits`, and `keyword_search_terms` tables).

---

## Quick Start (No Python Required — Windows `WorkStudio.exe`)

1. Download **`WorkStudio.exe`** from the [**GitHub Releases**](https://github.com/sujan0629/chrome-local-data-mapper/releases) page.
2. Double-click **`WorkStudio.exe`**.
3. It automatically:
   - Detects all local Google Chrome profiles on your computer (displaying account names & emails).
   - Connects to your `Default` Chrome `History` database.
   - Opens the web dashboard (`http://127.0.0.1:8765`) in **Microsoft Edge** so Chrome doesn't lock the database file.

---

## Features

- **Direct URLs OR Plain Search Queries (e.g. `"how to make egg curry"`):**
  - Type any plain search phrase like `how to make egg curry` (or select **Google Search Query** / **YouTube Search Query**) and `WorkStudio` automatically generates:
    - Exact Chrome search URL (`https://www.google.com/search?q=how+to+make+egg+curry&oq=how+to+make+egg+curry&sourceid=chrome&ie=UTF-8`)
    - Page Title (`how to make egg curry - Google Search`)
    - Omnibox Search transition (`838860805`)
    - Matching row in Chrome's `keyword_search_terms` table (`keyword_id=2`, `term`, `normalized_term`) so it appears in Chrome's Omnibox autocomplete and `chrome://history`.
- **Two Database Modes:**
  1. **Sandbox Mode:** Automatically creates and seeds a local `sandbox_History.db` matching Chrome's SQLite schema.
  2. **Custom / Real Chrome `History` Mode:** Auto-detects Chrome profiles (`Default`, `Profile 1`, etc.) across Windows, macOS, and Linux, creates timestamped `.bak` backups before writes, and includes a 1-click **⚡ Sync to Chrome (Close Chrome)** button.
- **Exact 17-Digit WebKit Timestamp Conversion:**
  - Converts between `<input type="datetime-local">` (down to seconds) and Chrome's WebKit timestamp (`microseconds since 1601-01-01 00:00:00 UTC`):
    - `webkit_timestamp = int((unix_seconds + 11644473600) * 1_000_000)`
    - `unix_seconds = (webkit_timestamp / 1_000_000) - 11644473600`

---

## Running from Source (Windows / macOS / Linux)

Requires Python 3.10+ (uses **only the Python standard library** — zero `pip` packages needed to run).

- **Windows (1-Click):** Double-click `Start-ChromeHistoryStudio.bat`
- **macOS / Linux:**
  ```bash
  chmod +x start.sh
  ./start.sh
  ```
- **Manual CLI:**
  ```bash
  python app.py --auto-connect --open-browser
  ```

---

## Building `WorkStudio.exe` Yourself

Double-click `Build-Portable-Exe.bat` or run:

```bash
pip install pyinstaller
pyinstaller --onefile --name "WorkStudio" app.py
```

The standalone executable will be output to `dist/WorkStudio.exe`.
