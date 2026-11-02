# Chrome Local Data Mapper (`ChromeHistoryStudio`)

A standalone, zero-dependency desktop & web studio for viewing, adding, backdating, editing, and synchronizing Google Chrome SQLite `History` records (`urls` and `visits` tables).

---

## Quick Start (No Python Required — Windows `.exe`)

1. Download **`ChromeHistoryStudio.exe`** from the [**GitHub Releases**](https://github.com/sujan0629/chrome-local-data-mapper/releases) page.
2. Double-click **`ChromeHistoryStudio.exe`**.
3. It automatically:
   - Detects all local Google Chrome profiles on your computer (displaying account names & emails).
   - Connects to your `Default` Chrome `History` database.
   - Opens the web dashboard (`http://127.0.0.1:8765`) in **Microsoft Edge** so Chrome doesn't lock the database file.

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

## Features

- **Two Database Modes:**
  1. **Sandbox Mode:** Automatically creates and seeds a local `sandbox_History.db` matching Chrome's SQLite schema.
  2. **Custom / Real Chrome `History` Mode:** Auto-detects Chrome profiles (`Default`, `Profile 1`, etc.) across Windows, macOS, and Linux, creates timestamped `.bak` backups before writes, and includes a 1-click **⚡ Sync to Chrome (Close Chrome)** button.
- **Exact 17-Digit WebKit Timestamp Conversion:**
  - Converts between `<input type="datetime-local">` (down to seconds) and Chrome's WebKit timestamp (`microseconds since 1601-01-01 00:00:00 UTC`):
    - `webkit_timestamp = int((unix_seconds + 11644473600) * 1_000_000)`
    - `unix_seconds = (webkit_timestamp / 1_000_000) - 11644473600`
- **Add, Backdate, Edit & Delete:**
  - Add links with custom past/future dates, transition types (`Clicked Link`, `Typed URL`, `Bookmark`, `Reload`), visit durations, and **Auto-Fetch Title from URL**.
  - Inline edit/backdate any existing visit row.
  - Automatically recalculates `urls.visit_count = COUNT(*)` and `urls.last_visit_time = MAX(visit_time)` after every change.
- **Live SQL Execution Log:**
  - Displays exact `SELECT`, `INSERT`, `UPDATE`, and `DELETE` queries executed against SQLite in real time.

---

## Building the Standalone `.exe` Yourself

Double-click `Build-Portable-Exe.bat` or run:

```bash
pip install pyinstaller
pyinstaller --onefile --name "ChromeHistoryStudio" app.py
```

The standalone executable will be output to `dist/ChromeHistoryStudio.exe`.
