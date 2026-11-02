#!/usr/bin/env python3
"""
Chrome History SQLite Studio
============================
A standalone, zero-dependency Python web application for viewing, adding,
backdating, and editing Google Chrome SQLite `History` records (`urls` and `visits`).

Uses only the Python standard library:
  http.server, sqlite3, urllib, json, datetime, shutil, os, html, re, sys, time
"""

import argparse
import datetime
import html
import json
import os
import platform
import re
import shutil
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# Constants & WebKit Timestamp Conversion
# ---------------------------------------------------------------------------

WEBKIT_EPOCH_OFFSET = 11644473600  # Seconds between 1601-01-01 00:00:00 UTC and 1970-01-01 00:00:00 UTC
MICROSECONDS_PER_SECOND = 1_000_000

TRANSITION_TYPES = {
    805306368: "Clicked Link",
    805306369: "Typed URL",
    805306370: "Bookmark",
    805306376: "Reload",
    838860805: "Omnibox Search",
}

TYPED_URL_TRANSITION = 805306369
OMNIBOX_SEARCH_TRANSITION = 838860805
DEFAULT_TRANSITION = 805306368


def resolve_url_or_search_query(
    raw_input: str,
    raw_title: str = "",
    entry_type: str = "auto",
    transition: int = DEFAULT_TRANSITION,
) -> dict:
    """
    Convert a plain search query (e.g. 'how to make egg curry') or URL into
    Chrome's exact URL, Page Title, Transition code, and `keyword_search_terms` metadata.
    """
    text = (raw_input or "").strip()
    if not text:
        raise ValueError("URL or search query cannot be empty.")

    title = (raw_title or "").strip()
    entry_type = (entry_type or "auto").strip().lower()
    trans = int(transition or DEFAULT_TRANSITION)

    # Determine if `text` is a plain search query vs a URL
    looks_like_url = (
        text.startswith(("http://", "https://", "chrome://", "file://", "about:"))
        or (" " not in text and "." in text and not text.startswith("."))
    )

    if entry_type == "google_search" or (entry_type == "auto" and not looks_like_url):
        q_encoded = urllib.parse.quote_plus(text)
        final_url = f"https://www.google.com/search?q={q_encoded}&oq={q_encoded}&sourceid=chrome&ie=UTF-8"
        final_title = title if title else f"{text} - Google Search"
        final_trans = OMNIBOX_SEARCH_TRANSITION if trans == DEFAULT_TRANSITION else trans
        return {
            "url": final_url,
            "title": final_title,
            "transition": final_trans,
            "search_term": text,
            "normalized_term": " ".join(text.lower().split()),
            "keyword_id": 2,  # Chrome default Google keyword_id
            "is_search": True,
        }

    if entry_type == "youtube_search":
        q_encoded = urllib.parse.quote_plus(text)
        final_url = f"https://www.youtube.com/results?search_query={q_encoded}"
        final_title = title if title else f"{text} - YouTube"
        return {
            "url": final_url,
            "title": final_title,
            "transition": trans,
            "search_term": text,
            "normalized_term": " ".join(text.lower().split()),
            "keyword_id": 31,  # YouTube keyword_id
            "is_search": True,
        }

    # Direct URL: normalize scheme if missing (e.g. 'wikipedia.org/wiki/Dodgeball')
    final_url = text
    if not final_url.startswith(("http://", "https://", "chrome://", "file://", "about:")):
        final_url = "https://" + final_url

    # Check if the direct URL itself is a Google or YouTube search URL
    parsed = urllib.parse.urlparse(final_url)
    qs = urllib.parse.parse_qs(parsed.query)
    if "google." in (parsed.netloc or "").lower() and parsed.path.startswith("/search") and "q" in qs:
        term = qs["q"][0].strip()
        if term:
            return {
                "url": final_url,
                "title": title if title else f"{term} - Google Search",
                "transition": trans,
                "search_term": term,
                "normalized_term": " ".join(term.lower().split()),
                "keyword_id": 2,
                "is_search": True,
            }
    if "youtube.com" in (parsed.netloc or "").lower() and "search_query" in qs:
        term = qs["search_query"][0].strip()
        if term:
            return {
                "url": final_url,
                "title": title if title else f"{term} - YouTube",
                "transition": trans,
                "search_term": term,
                "normalized_term": " ".join(term.lower().split()),
                "keyword_id": 31,
                "is_search": True,
            }

    return {
        "url": final_url,
        "title": title,
        "transition": trans,
        "search_term": "",
        "normalized_term": "",
        "keyword_id": 0,
        "is_search": False,
    }

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SANDBOX_DB = os.path.join(BASE_DIR, "sandbox_History.db")
BACKUP_DIR = os.path.join(BASE_DIR, "backups")


def unix_to_webkit(unix_seconds: float) -> int:
    """
    Convert Unix epoch seconds to Chrome's 17-digit WebKit timestamp
    (microseconds since 1601-01-01 00:00:00 UTC).
    Formula: webkit_timestamp = int((unix_seconds + 11644473600) * 1_000_000)
    """
    return int(round((float(unix_seconds) + WEBKIT_EPOCH_OFFSET) * MICROSECONDS_PER_SECOND))


def webkit_to_unix(webkit_timestamp: int) -> float:
    """
    Convert Chrome's 17-digit WebKit timestamp to Unix epoch seconds.
    Formula: unix_seconds = (webkit_timestamp / 1_000_000) - 11644473600
    """
    if not webkit_timestamp or int(webkit_timestamp) <= 0:
        return 0.0
    return (int(webkit_timestamp) / MICROSECONDS_PER_SECOND) - WEBKIT_EPOCH_OFFSET


def parse_datetime_local(dt_str: str, use_utc: bool = False) -> datetime.datetime:
    """
    Parse an HTML <input type="datetime-local"> string or ISO timestamp string
    (e.g. '2022-08-15T14:30:00', '2022-08-15 14:30:00', '2022-08-15T14:30')
    into a datetime object.
    """
    cleaned = dt_str.strip().replace(" ", "T")
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1]
        use_utc = True
    # Normalize HH:MM to HH:MM:00
    if len(cleaned) == 16 and cleaned[10] == "T":
        cleaned += ":00"
    dt = datetime.datetime.fromisoformat(cleaned)
    if use_utc:
        return dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def datetime_local_to_webkit(dt_str: str, use_utc: bool = False) -> int:
    """Convert a datetime-local string directly to a 17-digit WebKit timestamp."""
    dt = parse_datetime_local(dt_str, use_utc=use_utc)
    unix_seconds = dt.timestamp()
    return unix_to_webkit(unix_seconds)


def webkit_to_datetime_local(webkit_timestamp: int, use_utc: bool = False) -> str:
    """Convert a 17-digit WebKit timestamp to 'YYYY-MM-DDTHH:MM:SS' for datetime-local inputs."""
    if not webkit_timestamp or int(webkit_timestamp) <= 0:
        return ""
    unix_seconds = webkit_to_unix(int(webkit_timestamp))
    try:
        if use_utc:
            dt = datetime.datetime.fromtimestamp(unix_seconds, tz=datetime.timezone.utc)
        else:
            dt = datetime.datetime.fromtimestamp(unix_seconds)
        return dt.strftime("%Y-%m-%dT%H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return ""


def webkit_to_display_str(webkit_timestamp: int, use_utc: bool = False) -> str:
    """Convert a 17-digit WebKit timestamp to human-readable 'YYYY-MM-DD HH:MM:SS'."""
    iso = webkit_to_datetime_local(webkit_timestamp, use_utc=use_utc)
    return iso.replace("T", " ") if iso else "Never"


def transition_label(transition_val: int) -> str:
    """Return human-readable label for a Chrome transition value."""
    if transition_val in TRANSITION_TYPES:
        return TRANSITION_TYPES[transition_val]
    core = int(transition_val) & 0xFF
    core_map = {
        0: "Clicked Link",
        1: "Typed URL",
        2: "Bookmark",
        3: "Auto Subframe",
        4: "Manual Subframe",
        5: "Generated",
        6: "Start Page",
        7: "Form Submit",
        8: "Reload",
        9: "Keyword",
        10: "Keyword Generated",
    }
    return core_map.get(core, f"Custom ({transition_val})")


def is_typed_transition(transition_val: int) -> bool:
    """Check if the transition represents a Typed URL."""
    return int(transition_val) == TYPED_URL_TRANSITION or (int(transition_val) & 0xFF) == 1


# ---------------------------------------------------------------------------
# OS Default Chrome History Path Detection
# ---------------------------------------------------------------------------

def detect_os_chrome_paths() -> dict:
    """
    Return default OS Chrome History paths for Windows, macOS, and Linux,
    plus any existing Chrome profile History files found on this system.
    """
    home = os.path.expanduser("~")
    local_app_data = os.environ.get("LOCALAPPDATA", os.path.join(home, "AppData", "Local"))

    os_templates = {
        "windows": os.path.join(local_app_data, "Google", "Chrome", "User Data", "Default", "History"),
        "macos": os.path.expanduser("~/Library/Application Support/Google/Chrome/Default/History"),
        "linux": os.path.expanduser("~/.config/google-chrome/Default/History"),
        "linux_chromium": os.path.expanduser("~/.config/chromium/Default/History"),
    }

    current_os = platform.system().lower()
    detected_profiles = []

    # Candidate User Data roots on the current machine
    candidate_roots = [
        os.path.join(local_app_data, "Google", "Chrome", "User Data"),
        os.path.expanduser("~/Library/Application Support/Google/Chrome"),
        os.path.expanduser("~/.config/google-chrome"),
        os.path.expanduser("~/.config/chromium"),
    ]

    seen_paths = set()
    for root in candidate_roots:
        if os.path.isdir(root):
            # Try reading Chrome's Local State for human-friendly profile names (gaia_name, name, user_name)
            info_cache = {}
            local_state_path = os.path.join(root, "Local State")
            if os.path.isfile(local_state_path):
                try:
                    with open(local_state_path, "r", encoding="utf-8", errors="replace") as f:
                        ls_data = json.load(f)
                        info_cache = ls_data.get("profile", {}).get("info_cache", {}) or {}
                except Exception:
                    pass

            try:
                for entry in sorted(os.listdir(root)):
                    if entry == "Default" or entry.startswith("Profile "):
                        hist_path = os.path.join(root, entry, "History")
                        if os.path.isfile(hist_path) and hist_path not in seen_paths:
                            seen_paths.add(hist_path)
                            p_meta = info_cache.get(entry, {}) if isinstance(info_cache, dict) else {}
                            gaia_name = (p_meta.get("gaia_name") or "").strip()
                            prof_name = (p_meta.get("name") or "").strip()
                            user_email = (p_meta.get("user_name") or "").strip()

                            # Fallback to Preferences inside the profile directory if needed
                            if not gaia_name and not prof_name:
                                prefs_path = os.path.join(root, entry, "Preferences")
                                if os.path.isfile(prefs_path):
                                    try:
                                        with open(prefs_path, "r", encoding="utf-8", errors="replace") as pf:
                                            prefs = json.load(pf)
                                            prof_name = (prefs.get("profile", {}).get("name") or "").strip()
                                            acct = prefs.get("account_info") or []
                                            if acct and isinstance(acct, list):
                                                gaia_name = (acct[0].get("full_name") or "").strip()
                                                user_email = user_email or (acct[0].get("email") or "").strip()
                                    except Exception:
                                        pass

                            label_parts = []
                            if gaia_name:
                                label_parts.append(gaia_name)
                            if prof_name and prof_name.lower() != gaia_name.lower():
                                label_parts.append(prof_name)
                            if user_email:
                                label_parts.append(user_email)

                            display_label = f"{entry} ({' | '.join(label_parts)})" if label_parts else entry
                            detected_profiles.append({
                                "profile": display_label,
                                "folder": entry,
                                "gaia_name": gaia_name,
                                "profile_name": prof_name,
                                "email": user_email,
                                "path": hist_path,
                                "exists": True,
                                "size_bytes": os.path.getsize(hist_path),
                            })
            except OSError:
                pass

    # Also include standard template paths so user can see/select them
    for key, path in os_templates.items():
        if path not in seen_paths:
            detected_profiles.append({
                "profile": f"Default ({key})",
                "path": path,
                "exists": os.path.isfile(path),
                "size_bytes": os.path.getsize(path) if os.path.isfile(path) else 0,
            })

    return {
        "current_os": current_os,
        "os_templates": os_templates,
        "detected_paths": detected_profiles,
    }


# ---------------------------------------------------------------------------
# Database Locked Custom Exception
# ---------------------------------------------------------------------------

class DatabaseLockedError(Exception):
    """Raised when SQLite reports that the History database is locked by Chrome."""
    def __init__(self, db_path: str, original_error: Exception):
        self.db_path = db_path
        self.original_error = original_error
        super().__init__(
            f"sqlite3.OperationalError: database is locked ({db_path}). "
            "Please close Google Chrome or switch to a working copy."
        )


# ---------------------------------------------------------------------------
# History Database Manager
# ---------------------------------------------------------------------------

class HistoryDBManager:
    """
    Manages connections, schema introspection, backups, SQL query logging,
    and CRUD operations on Chrome SQLite `History` databases.
    """

    def __init__(self, sandbox_path: str = DEFAULT_SANDBOX_DB):
        self.lock = threading.RLock()
        self.sandbox_path = os.path.abspath(sandbox_path)
        self.mode = "sandbox"  # 'sandbox' or 'custom'
        self.custom_path = ""
        self.target_chrome_path = ""  # Original Chrome History path when using live mirror while Chrome is open
        self.pending_chrome_ops = []  # Queued operations to replay onto target_chrome_path once Chrome closes
        self.sql_log = []  # List of recent SQL query dicts
        self.max_log_entries = 250
        self.last_backup_path = None
        self.ensure_sandbox_initialized()
        self._start_autosync_thread()

    def _start_autosync_thread(self):
        """Background daemon that automatically syncs queued edits to live Chrome History once Chrome closes."""
        def _worker():
            while True:
                time.sleep(1.5)
                try:
                    self.try_sync_to_target_chrome()
                except Exception:
                    pass

        t = threading.Thread(target=_worker, daemon=True)
        t.start()

    def try_sync_to_target_chrome(self) -> bool:
        """If using a working mirror because Chrome was open, check if Chrome closed and sync changes."""
        with self.lock:
            if (
                self.mode != "custom"
                or not self.target_chrome_path
                or self.custom_path == self.target_chrome_path
            ):
                return False
            if not os.path.isfile(self.target_chrome_path):
                return False

            # Probe if target_chrome_path is unlocked now
            try:
                probe = sqlite3.connect(self.target_chrome_path, timeout=0.4)
                probe.execute("BEGIN IMMEDIATE")
                probe.rollback()
                probe.close()
            except sqlite3.OperationalError:
                return False

            # Target Chrome History is now unlocked! Replay any pending operations onto it
            if self.pending_chrome_ops:
                ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:19]
                bak = f"{self.target_chrome_path}.{ts}.bak"
                try:
                    shutil.copy2(self.target_chrome_path, bak)
                    self.last_backup_path = bak
                except OSError:
                    pass

                conn = sqlite3.connect(self.target_chrome_path, timeout=2.0)
                conn.row_factory = sqlite3.Row
                try:
                    for op in self.pending_chrome_ops:
                        kind = op["op"]
                        if kind == "add_visit":
                            self._add_visit_in_tx(
                                conn,
                                url=op["url"],
                                title=op["title"],
                                webkit_time=op["webkit_time"],
                                transition=op["transition"],
                                duration_seconds=op["duration_seconds"],
                            )
                        elif kind == "delete_url_by_str":
                            cur = conn.execute("SELECT id FROM urls WHERE url = ?", (op["url"],))
                            for r in cur.fetchall():
                                conn.execute("DELETE FROM visits WHERE url = ?", (r["id"],))
                                conn.execute("DELETE FROM urls WHERE id = ?", (r["id"],))
                    conn.commit()
                    self.pending_chrome_ops.clear()
                finally:
                    conn.close()

            # Switch directly to the live unlocked Chrome History file
            self.custom_path = self.target_chrome_path
            return True

    def force_sync_chrome(self) -> dict:
        """Close running Chrome processes on Windows/macOS/Linux so the lock releases, then sync directly."""
        import subprocess
        with self.lock:
            if platform.system().lower() == "windows":
                subprocess.run(["taskkill", "/F", "/IM", "chrome.exe"], capture_output=True)
            else:
                subprocess.run(["pkill", "-f", "chrome"], capture_output=True)
            time.sleep(1.2)
            synced = self.try_sync_to_target_chrome()
            return {
                "synced": synced,
                "active_db_path": self.active_db_path,
                "target_chrome_path": self.target_chrome_path or self.active_db_path,
                "pending_sync_count": len(self.pending_chrome_ops),
                "message": (
                    f"Closed Chrome & synced all changes directly to {self.active_db_path}! "
                    "You can now open Chrome and check chrome://history."
                    if synced or self.active_db_path == self.target_chrome_path
                    else "Attempted sync, but database is still locked."
                ),
            }

    @property
    def active_db_path(self) -> str:
        if self.mode == "custom" and self.custom_path:
            return self.custom_path
        return self.sandbox_path

    # -- SQL Logging Helpers --

    def _format_sql_with_params(self, sql: str, params: tuple | list = ()) -> str:
        """Render SQL query with bound parameters inline for clear display in the SQL log."""
        normalized_sql = " ".join(sql.strip().split())
        if not params:
            return normalized_sql
        parts = normalized_sql.split("?")
        if len(parts) != len(params) + 1:
            return f"{normalized_sql} -- params: {repr(list(params))}"
        out = []
        for idx, param in enumerate(params):
            out.append(parts[idx])
            if param is None:
                out.append("NULL")
            elif isinstance(param, (int, float)):
                out.append(str(param))
            else:
                escaped = str(param).replace("'", "''")
                out.append(f"'{escaped}'")
        out.append(parts[-1])
        return "".join(out)

    def _log_sql(self, sql: str, params: tuple | list = (), duration_ms: float = 0.0, rowcount: int = 0):
        rendered = self._format_sql_with_params(sql, params)
        verb = sql.strip().split()[0].upper() if sql.strip() else "SQL"
        entry = {
            "id": len(self.sql_log) + 1,
            "timestamp": datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3],
            "verb": verb,
            "sql": rendered,
            "raw_sql": " ".join(sql.strip().split()),
            "params": list(params) if params else [],
            "duration_ms": round(duration_ms, 2),
            "rowcount": rowcount,
            "db_mode": self.mode,
            "db_file": os.path.basename(self.active_db_path),
        }
        self.sql_log.insert(0, entry)
        if len(self.sql_log) > self.max_log_entries:
            self.sql_log = self.sql_log[: self.max_log_entries]

    def execute_sql(self, conn: sqlite3.Connection, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        """Execute a SQL statement while recording its exact form and timing in the SQL log."""
        start = time.perf_counter()
        cur = conn.cursor()
        try:
            cur.execute(sql, params)
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "locked" in msg or "busy" in msg:
                raise DatabaseLockedError(self.active_db_path, exc) from exc
            raise
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        self._log_sql(sql, params, duration_ms=elapsed_ms, rowcount=cur.rowcount)
        return cur

    # -- Connection & Backup Management --

    def _connect(self, db_path: str | None = None) -> sqlite3.Connection:
        target = db_path or self.active_db_path
        try:
            conn = sqlite3.connect(target, timeout=1.5)
            conn.row_factory = sqlite3.Row
            return conn
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "locked" in msg or "busy" in msg:
                raise DatabaseLockedError(target, exc) from exc
            raise

    def create_backup_if_custom(self) -> str | None:
        """
        If operating in custom/real Chrome History mode, automatically create a
        timestamped .bak backup before any write operation.
        """
        if self.mode != "custom":
            return None
        target = self.active_db_path
        if not os.path.isfile(target):
            return None

        os.makedirs(BACKUP_DIR, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:19]
        base_name = os.path.basename(target)
        # Create backup alongside the file if writable, and also in BACKUP_DIR
        local_bak = f"{target}.{ts}.bak"
        try:
            shutil.copy2(target, local_bak)
            self.last_backup_path = local_bak
            return local_bak
        except OSError:
            fallback_bak = os.path.join(BACKUP_DIR, f"{base_name}.{ts}.bak")
            shutil.copy2(target, fallback_bak)
            self.last_backup_path = fallback_bak
            return fallback_bak

    # -- Schema Creation & Dynamic Introspection --

    def ensure_schema(self, conn: sqlite3.Connection):
        """Ensure Chrome's core `urls` and `visits` tables exist on the database."""
        self.execute_sql(
            conn,
            """
            CREATE TABLE IF NOT EXISTS urls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url LONGVARCHAR,
                title LONGVARCHAR,
                visit_count INTEGER DEFAULT 0 NOT NULL,
                typed_count INTEGER DEFAULT 0 NOT NULL,
                last_visit_time INTEGER NOT NULL,
                hidden INTEGER DEFAULT 0 NOT NULL
            )
            """,
        )
        self.execute_sql(
            conn,
            """
            CREATE TABLE IF NOT EXISTS visits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url INTEGER NOT NULL,
                visit_time INTEGER NOT NULL,
                from_visit INTEGER DEFAULT 0,
                transition INTEGER DEFAULT 805306368 NOT NULL,
                segment_id INTEGER DEFAULT 0,
                visit_duration INTEGER DEFAULT 0 NOT NULL
            )
            """,
        )
        self.execute_sql(conn, "CREATE INDEX IF NOT EXISTS urls_url_index ON urls (url)")
        self.execute_sql(conn, "CREATE INDEX IF NOT EXISTS visits_url_index ON visits (url)")
        self.execute_sql(conn, "CREATE INDEX IF NOT EXISTS visits_time_index ON visits (visit_time)")

    def get_table_columns(self, conn: sqlite3.Connection, table_name: str) -> list[dict]:
        """Inspect table schema via PRAGMA table_info(table_name)."""
        if table_name not in ("urls", "visits"):
            raise ValueError(f"Unsupported table name: {table_name}")
        cur = self.execute_sql(conn, f"PRAGMA table_info({table_name})")
        cols = []
        for row in cur.fetchall():
            cols.append({
                "cid": row["cid"],
                "name": row["name"],
                "type": row["type"] or "",
                "notnull": bool(row["notnull"]),
                "dflt_value": row["dflt_value"],
                "pk": bool(row["pk"]),
            })
        return cols

    def _safe_default_for_type(self, col_type: str):
        """Return safe default value ('' for text-like columns, 0 for numeric/boolean/other)."""
        upper_t = (col_type or "").upper()
        if any(tok in upper_t for tok in ("CHAR", "CLOB", "TEXT", "VARCHAR")):
            return ""
        return 0

    def dynamic_insert(self, conn: sqlite3.Connection, table_name: str, values: dict) -> int:
        """
        Inspect PRAGMA table_info(table_name) before inserting into `urls` or `visits`
        so any additional modern Chromium columns with NOT NULL constraints receive
        safe default values (0 or '').
        Returns the inserted row's lastrowid.
        """
        columns_info = self.get_table_columns(conn, table_name)
        existing_col_names = {c["name"] for c in columns_info}

        insert_data = {}
        # 1. Include provided values that exist in the table
        for k, v in values.items():
            if k in existing_col_names:
                insert_data[k] = v

        # 2. Populate all remaining Chromium columns with safe non-NULL defaults
        for col in columns_info:
            col_name = col["name"]
            if col_name in insert_data or col["pk"]:
                continue
            if col_name in ("is_known_to_sync", "consider_for_ntp_most_visited"):
                insert_data[col_name] = 1
            elif col["notnull"] or col["dflt_value"] is None:
                insert_data[col_name] = self._safe_default_for_type(col["type"])

        col_names = list(insert_data.keys())
        placeholders = ", ".join(["?"] * len(col_names))
        cols_sql = ", ".join(col_names)
        params = [insert_data[c] for c in col_names]

        sql = f"INSERT INTO {table_name} ({cols_sql}) VALUES ({placeholders})"
        cur = self.execute_sql(conn, sql, params)
        return cur.lastrowid

    # -- Sandbox Initialization & Seeding --

    def ensure_sandbox_initialized(self, force_reseed: bool = False):
        """Create and seed `sandbox_History.db` if it does not exist or has no records."""
        with self.lock:
            is_new = not os.path.isfile(self.sandbox_path)
            conn = self._connect(self.sandbox_path)
            try:
                self.ensure_schema(conn)
                if force_reseed:
                    self.execute_sql(conn, "DELETE FROM visits")
                    self.execute_sql(conn, "DELETE FROM urls")

                cur = self.execute_sql(conn, "SELECT COUNT(*) AS cnt FROM urls")
                count = cur.fetchone()["cnt"]

                if is_new or force_reseed or count == 0:
                    now = time.time()
                    seed_entries = [
                        {
                            "url": "https://developer.chrome.com/docs/extensions/",
                            "title": "Chrome Extensions Documentation - Chrome for Developers",
                            "visit_time_unix": now - 86400 * 5,
                            "transition": 805306369,  # Typed URL
                            "duration_sec": 145,
                        },
                        {
                            "url": "https://developer.chrome.com/docs/extensions/",
                            "title": "Chrome Extensions Documentation - Chrome for Developers",
                            "visit_time_unix": now - 3600 * 6,
                            "transition": 805306370,  # Bookmark
                            "duration_sec": 62,
                        },
                        {
                            "url": "https://www.sqlite.org/pragma.html",
                            "title": "Pragma statements supported by SQLite",
                            "visit_time_unix": now - 86400 * 2,
                            "transition": 805306368,  # Clicked Link
                            "duration_sec": 210,
                        },
                        {
                            "url": "https://docs.python.org/3/library/sqlite3.html",
                            "title": "sqlite3 — DB-API 2.0 interface for SQLite databases — Python 3 documentation",
                            "visit_time_unix": now - 86400 * 1 - 7200,
                            "transition": 805306368,  # Clicked Link
                            "duration_sec": 95,
                        },
                        {
                            "url": "https://github.com/chromium/chromium/tree/main/components/history/core/browser",
                            "title": "chromium/components/history/core/browser at main · chromium/chromium",
                            "visit_time_unix": now - 1800,
                            "transition": 805306369,  # Typed URL
                            "duration_sec": 320,
                        },
                    ]
                    for item in seed_entries:
                        self._add_visit_in_tx(
                            conn,
                            url=item["url"],
                            title=item["title"],
                            webkit_time=unix_to_webkit(item["visit_time_unix"]),
                            transition=item["transition"],
                            duration_seconds=item["duration_sec"],
                        )
                conn.commit()
            finally:
                conn.close()

    # -- Core URL & Visit Synchronization Logic --

    def _sync_url_stats(self, conn: sqlite3.Connection, url_id: int) -> dict | None:
        """
        Recalculate and sync `urls.visit_count = COUNT(*)` and
        `urls.last_visit_time = MAX(visit_time)` from `visits` for `url_id`.
        If visit_count reaches 0, delete the parent `urls` row.
        """
        cur = self.execute_sql(
            conn,
            "SELECT COUNT(*) AS cnt, COALESCE(MAX(visit_time), 0) AS max_time FROM visits WHERE url = ?",
            (url_id,),
        )
        stats = cur.fetchone()
        visit_count = int(stats["cnt"])
        last_visit_time = int(stats["max_time"])

        if visit_count == 0:
            self.execute_sql(conn, "DELETE FROM urls WHERE id = ?", (url_id,))
            return None

        self.execute_sql(
            conn,
            "UPDATE urls SET visit_count = ?, last_visit_time = ? WHERE id = ?",
            (visit_count, last_visit_time, url_id),
        )
        cur_url = self.execute_sql(
            conn,
            "SELECT id, url, title, visit_count, typed_count, last_visit_time, hidden FROM urls WHERE id = ?",
            (url_id,),
        )
        row = cur_url.fetchone()
        return dict(row) if row else None

    def _add_visit_in_tx(
        self,
        conn: sqlite3.Connection,
        url: str,
        title: str,
        webkit_time: int,
        transition: int = DEFAULT_TRANSITION,
        duration_seconds: float = 0.0,
        from_visit: int = 0,
        entry_type: str = "auto",
    ) -> dict:
        """
        Internal helper to add a visit (or Google/YouTube search query) within an active transaction:
        1. Resolve plain search queries (e.g. 'how to make egg curry') into Chrome search URLs + titles.
        2. Check if `url` already exists in `urls`.
        3. If it exists, update `title` (if provided) and increment `typed_count` if transition is Typed URL.
        4. If it does not exist, insert a new row into `urls`.
        5. Insert the new visit record into `visits` referencing `urls.id`.
        6. If it is a search query, record the search term in `keyword_search_terms`.
        7. Recalculate and sync `urls.visit_count = COUNT(*)` and `urls.last_visit_time = MAX(visit_time)`.
        """
        resolved = resolve_url_or_search_query(url, title, entry_type=entry_type, transition=transition)
        url = resolved["url"]
        title = resolved["title"]
        transition = int(resolved["transition"])
        webkit_time = int(webkit_time)
        visit_duration_us = int(round(max(0.0, float(duration_seconds or 0.0)) * MICROSECONDS_PER_SECOND))
        typed_increment = 1 if is_typed_transition(transition) else 0

        cur = self.execute_sql(
            conn,
            "SELECT id, url, title, visit_count, typed_count, last_visit_time FROM urls WHERE url = ?",
            (url,),
        )
        existing_url = cur.fetchone()

        if existing_url:
            url_id = int(existing_url["id"])
            new_title = title if title else (existing_url["title"] or "")
            new_typed_count = int(existing_url["typed_count"] or 0) + typed_increment
            self.execute_sql(
                conn,
                "UPDATE urls SET title = ?, typed_count = ? WHERE id = ?",
                (new_title, new_typed_count, url_id),
            )
        else:
            url_id = self.dynamic_insert(
                conn,
                "urls",
                {
                    "url": url,
                    "title": title or url,
                    "visit_count": 1,
                    "typed_count": typed_increment,
                    "last_visit_time": webkit_time,
                    "hidden": 0,
                },
            )

        visit_id = self.dynamic_insert(
            conn,
            "visits",
            {
                "url": url_id,
                "visit_time": webkit_time,
                "from_visit": int(from_visit or 0),
                "transition": transition,
                "segment_id": 0,
                "visit_duration": visit_duration_us,
            },
        )

        # If this is a search query (Google / YouTube), populate Chrome's `keyword_search_terms` table
        if resolved["is_search"] and resolved["search_term"]:
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS keyword_search_terms (
                        keyword_id INTEGER NOT NULL,
                        url_id INTEGER NOT NULL,
                        term LONGVARCHAR NOT NULL,
                        normalized_term LONGVARCHAR NOT NULL
                    )
                    """
                )
                existing_kw = conn.execute(
                    "SELECT 1 FROM keyword_search_terms WHERE url_id = ? AND normalized_term = ?",
                    (url_id, resolved["normalized_term"]),
                ).fetchone()
                if not existing_kw:
                    self.execute_sql(
                        conn,
                        "INSERT INTO keyword_search_terms (keyword_id, url_id, term, normalized_term) VALUES (?, ?, ?, ?)",
                        (resolved["keyword_id"], url_id, resolved["search_term"], resolved["normalized_term"]),
                    )
            except Exception:
                pass

        # If real Chrome `context_annotations` table exists, insert a matching annotation row
        try:
            has_ctx = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='context_annotations'"
            ).fetchone()
            if has_ctx:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO context_annotations (
                        visit_id, context_annotation_flags, duration_since_last_visit,
                        page_end_reason, total_foreground_duration, browser_type,
                        window_id, tab_id, task_id, root_task_id, parent_task_id, response_code
                    ) VALUES (?, 0, -1000000, 6, ?, 1, 1, 1, ?, ?, -1, 200)
                    """,
                    (visit_id, visit_duration_us, webkit_time, webkit_time),
                )
        except Exception:
            pass

        updated_url = self._sync_url_stats(conn, url_id)
        return {
            "visit_id": visit_id,
            "url_id": url_id,
            "resolved_url": url,
            "resolved_title": title or url,
            "search_term": resolved["search_term"],
            "webkit_timestamp": webkit_time,
            "url_record": updated_url,
        }

    # -- Public CRUD Methods --

    def add_visit(
        self,
        url: str,
        title: str = "",
        datetime_local: str | None = None,
        webkit_timestamp: int | None = None,
        transition: int = DEFAULT_TRANSITION,
        duration_seconds: float = 0.0,
        use_utc: bool = False,
        entry_type: str = "auto",
    ) -> dict:
        """Add a link or search query with a custom date/time or explicit WebKit timestamp."""
        with self.lock:
            self.try_sync_to_target_chrome()
            resolved_preview = resolve_url_or_search_query(url, title, entry_type=entry_type, transition=transition)
            if not resolved_preview["title"] and not resolved_preview["is_search"]:
                try:
                    title = fetch_title_from_url(resolved_preview["url"], timeout=2.5)
                except Exception:
                    title = ""
            if webkit_timestamp is not None:
                webkit_time = int(webkit_timestamp)
            elif datetime_local:
                webkit_time = datetime_local_to_webkit(datetime_local, use_utc=use_utc)
            else:
                webkit_time = unix_to_webkit(time.time())

            backup_path = self.create_backup_if_custom()
            conn = self._connect()
            try:
                self.ensure_schema(conn)
                result = self._add_visit_in_tx(
                    conn,
                    url=url,
                    title=title,
                    webkit_time=webkit_time,
                    transition=transition,
                    duration_seconds=duration_seconds,
                    entry_type=entry_type,
                )
                conn.commit()
                if self.target_chrome_path and self.target_chrome_path != self.custom_path:
                    self.pending_chrome_ops.append({
                        "op": "add_visit",
                        "url": result["resolved_url"],
                        "title": result["resolved_title"],
                        "webkit_time": webkit_time,
                        "transition": transition,
                        "duration_seconds": duration_seconds,
                    })
                result["backup_created"] = backup_path
                return result
            except sqlite3.OperationalError as exc:
                conn.rollback()
                if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                    raise DatabaseLockedError(self.active_db_path, exc) from exc
                raise
            finally:
                conn.close()

    def edit_visit(
        self,
        visit_id: int,
        url: str | None = None,
        title: str | None = None,
        datetime_local: str | None = None,
        webkit_timestamp: int | None = None,
        transition: int | None = None,
        duration_seconds: float | None = None,
        use_utc: bool = False,
    ) -> dict:
        """
        Edit or backdate an existing visit row (date/time, URL, title, transition, duration)
        and automatically recalculate `urls.last_visit_time` and `urls.visit_count`.
        """
        with self.lock:
            backup_path = self.create_backup_if_custom()
            conn = self._connect()
            try:
                self.ensure_schema(conn)
                cur = self.execute_sql(
                    conn,
                    """
                    SELECT v.id AS visit_id, v.url AS url_id, v.visit_time, v.transition, v.visit_duration,
                           u.url AS current_url, u.title AS current_title, u.typed_count
                    FROM visits v
                    JOIN urls u ON v.url = u.id
                    WHERE v.id = ?
                    """,
                    (int(visit_id),),
                )
                row = cur.fetchone()
                if not row:
                    raise ValueError(f"Visit ID {visit_id} not found.")

                old_url_id = int(row["url_id"])
                old_url_str = row["current_url"]
                new_url_str = (url.strip() if url is not None else old_url_str)
                if not new_url_str:
                    raise ValueError("URL cannot be empty.")

                new_title_str = title.strip() if title is not None else (row["current_title"] or "")

                if webkit_timestamp is not None:
                    new_webkit_time = int(webkit_timestamp)
                elif datetime_local:
                    new_webkit_time = datetime_local_to_webkit(datetime_local, use_utc=use_utc)
                else:
                    new_webkit_time = int(row["visit_time"])

                new_transition = int(transition) if transition is not None else int(row["transition"])
                if duration_seconds is not None:
                    new_duration_us = int(round(max(0.0, float(duration_seconds)) * MICROSECONDS_PER_SECOND))
                else:
                    new_duration_us = int(row["visit_duration"] or 0)

                target_url_id = old_url_id

                if new_url_str != old_url_str:
                    # Check if the new URL already exists in `urls`
                    cur_target = self.execute_sql(
                        conn, "SELECT id, title, typed_count FROM urls WHERE url = ?", (new_url_str,)
                    )
                    existing_target = cur_target.fetchone()

                    # Count how many visits old_url_id has
                    cur_cnt = self.execute_sql(
                        conn, "SELECT COUNT(*) AS cnt FROM visits WHERE url = ?", (old_url_id,)
                    )
                    old_visit_cnt = int(cur_cnt.fetchone()["cnt"])

                    if existing_target and int(existing_target["id"]) != old_url_id:
                        target_url_id = int(existing_target["id"])
                        if title is not None:
                            self.execute_sql(
                                conn,
                                "UPDATE urls SET title = ? WHERE id = ?",
                                (new_title_str, target_url_id),
                            )
                    elif old_visit_cnt <= 1:
                        # Only this visit uses old_url_id -> update the URL record in place
                        self.execute_sql(
                            conn,
                            "UPDATE urls SET url = ?, title = ? WHERE id = ?",
                            (new_url_str, new_title_str, old_url_id),
                        )
                        target_url_id = old_url_id
                    else:
                        # Multiple visits share old_url_id -> create a new URL row for this moved visit
                        target_url_id = self.dynamic_insert(
                            conn,
                            "urls",
                            {
                                "url": new_url_str,
                                "title": new_title_str,
                                "visit_count": 1,
                                "typed_count": 1 if is_typed_transition(new_transition) else 0,
                                "last_visit_time": new_webkit_time,
                                "hidden": 0,
                            },
                        )
                else:
                    # Same URL -> update title on the existing `urls` record
                    self.execute_sql(
                        conn,
                        "UPDATE urls SET title = ? WHERE id = ?",
                        (new_title_str, old_url_id),
                    )

                # Update the visit row
                self.execute_sql(
                    conn,
                    """
                    UPDATE visits
                    SET url = ?, visit_time = ?, transition = ?, visit_duration = ?
                    WHERE id = ?
                    """,
                    (target_url_id, new_webkit_time, new_transition, new_duration_us, int(visit_id)),
                )

                # Sync target URL stats (and old URL stats if moved)
                updated_target_url = self._sync_url_stats(conn, target_url_id)
                updated_old_url = None
                if target_url_id != old_url_id:
                    updated_old_url = self._sync_url_stats(conn, old_url_id)

                conn.commit()
                return {
                    "visit_id": int(visit_id),
                    "url_id": target_url_id,
                    "old_url_id": old_url_id,
                    "webkit_timestamp": new_webkit_time,
                    "url_record": updated_target_url,
                    "old_url_record": updated_old_url,
                    "backup_created": backup_path,
                }
            except sqlite3.OperationalError as exc:
                conn.rollback()
                if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                    raise DatabaseLockedError(self.active_db_path, exc) from exc
                raise
            finally:
                conn.close()

    def delete_visit(self, visit_id: int) -> dict:
        """
        Delete a single visit from `visits`.
        Automatically removes the parent `urls` row if `visit_count` reaches 0,
        or updates `visit_count` and `last_visit_time` if other visits remain.
        """
        with self.lock:
            backup_path = self.create_backup_if_custom()
            conn = self._connect()
            try:
                self.ensure_schema(conn)
                cur = self.execute_sql(
                    conn, "SELECT id, url FROM visits WHERE id = ?", (int(visit_id),)
                )
                row = cur.fetchone()
                if not row:
                    raise ValueError(f"Visit ID {visit_id} not found.")

                url_id = int(row["url"])
                self.execute_sql(conn, "DELETE FROM visits WHERE id = ?", (int(visit_id),))
                remaining_url = self._sync_url_stats(conn, url_id)
                conn.commit()
                return {
                    "deleted_visit_id": int(visit_id),
                    "url_id": url_id,
                    "url_deleted": remaining_url is None,
                    "url_record": remaining_url,
                    "backup_created": backup_path,
                }
            except sqlite3.OperationalError as exc:
                conn.rollback()
                if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                    raise DatabaseLockedError(self.active_db_path, exc) from exc
                raise
            finally:
                conn.close()

    def delete_url(self, url_id: int) -> dict:
        """Delete an entire URL and all of its associated visits."""
        with self.lock:
            backup_path = self.create_backup_if_custom()
            conn = self._connect()
            try:
                self.ensure_schema(conn)
                cur = self.execute_sql(conn, "SELECT id, url FROM urls WHERE id = ?", (int(url_id),))
                row = cur.fetchone()
                if not row:
                    raise ValueError(f"URL ID {url_id} not found.")

                del_visits = self.execute_sql(
                    conn, "DELETE FROM visits WHERE url = ?", (int(url_id),)
                )
                visits_deleted = del_visits.rowcount
                self.execute_sql(conn, "DELETE FROM urls WHERE id = ?", (int(url_id),))
                conn.commit()
                return {
                    "deleted_url_id": int(url_id),
                    "deleted_url": row["url"],
                    "visits_deleted": visits_deleted,
                    "backup_created": backup_path,
                }
            except sqlite3.OperationalError as exc:
                conn.rollback()
                if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                    raise DatabaseLockedError(self.active_db_path, exc) from exc
                raise
            finally:
                conn.close()

    # -- Querying & Filtering --

    def query_history(
        self,
        search: str = "",
        start_date: str = "",
        end_date: str = "",
        transition: str = "",
        limit: int = 300,
        use_utc: bool = False,
    ) -> dict:
        """
        Query visits joined with urls, supporting live substring search and date range filters,
        plus summary statistics and aggregated URLs table.
        """
        with self.lock:
            conn = self._connect()
            try:
                self.ensure_schema(conn)
                where_clauses = []
                params = []

                if search and search.strip():
                    q = f"%{search.strip()}%"
                    where_clauses.append("(u.url LIKE ? OR u.title LIKE ?)")
                    params.extend([q, q])

                if start_date and start_date.strip():
                    s_str = start_date.strip()
                    if len(s_str) == 10:
                        s_str += "T00:00:00"
                    start_webkit = datetime_local_to_webkit(s_str, use_utc=use_utc)
                    where_clauses.append("v.visit_time >= ?")
                    params.append(start_webkit)

                if end_date and end_date.strip():
                    e_str = end_date.strip()
                    if len(e_str) == 10:
                        e_str += "T23:59:59"
                    end_webkit = datetime_local_to_webkit(e_str, use_utc=use_utc)
                    where_clauses.append("v.visit_time <= ?")
                    params.append(end_webkit)

                if transition and str(transition).strip():
                    where_clauses.append("v.transition = ?")
                    params.append(int(transition))

                where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

                sql = f"""
                    SELECT
                        v.id AS visit_id,
                        v.url AS url_id,
                        v.visit_time,
                        v.from_visit,
                        v.transition,
                        v.segment_id,
                        v.visit_duration,
                        u.url AS url,
                        u.title AS title,
                        u.visit_count AS visit_count,
                        u.typed_count AS typed_count,
                        u.last_visit_time AS last_visit_time,
                        u.hidden AS hidden
                    FROM visits v
                    JOIN urls u ON v.url = u.id
                    {where_sql}
                    ORDER BY v.visit_time DESC, v.id DESC
                    LIMIT ?
                """
                params_with_limit = list(params) + [int(limit)]
                cur = self.execute_sql(conn, sql, params_with_limit)

                visits = []
                for r in cur.fetchall():
                    vt = int(r["visit_time"] or 0)
                    lvt = int(r["last_visit_time"] or 0)
                    dur_us = int(r["visit_duration"] or 0)
                    trans = int(r["transition"] or DEFAULT_TRANSITION)
                    visits.append({
                        "visit_id": r["visit_id"],
                        "url_id": r["url_id"],
                        "url": r["url"],
                        "title": r["title"] or "",
                        "visit_time": vt,
                        "visit_time_unix": round(webkit_to_unix(vt), 3) if vt else 0,
                        "visit_datetime_local": webkit_to_datetime_local(vt, use_utc=use_utc),
                        "visit_display": webkit_to_display_str(vt, use_utc=use_utc),
                        "from_visit": r["from_visit"] or 0,
                        "transition": trans,
                        "transition_label": transition_label(trans),
                        "segment_id": r["segment_id"] or 0,
                        "visit_duration": dur_us,
                        "visit_duration_sec": round(dur_us / MICROSECONDS_PER_SECOND, 2),
                        "visit_count": r["visit_count"],
                        "typed_count": r["typed_count"],
                        "last_visit_time": lvt,
                        "last_visit_datetime_local": webkit_to_datetime_local(lvt, use_utc=use_utc),
                        "last_visit_display": webkit_to_display_str(lvt, use_utc=use_utc),
                        "hidden": r["hidden"],
                    })

                # Also query URLs summary table
                url_where = ""
                url_params = []
                if search and search.strip():
                    q = f"%{search.strip()}%"
                    url_where = "WHERE url LIKE ? OR title LIKE ?"
                    url_params = [q, q]

                cur_urls = self.execute_sql(
                    conn,
                    f"""
                    SELECT id, url, title, visit_count, typed_count, last_visit_time, hidden
                    FROM urls
                    {url_where}
                    ORDER BY last_visit_time DESC, id DESC
                    LIMIT ?
                    """,
                    url_params + [int(limit)],
                )
                urls_list = []
                for u in cur_urls.fetchall():
                    lvt = int(u["last_visit_time"] or 0)
                    urls_list.append({
                        "id": u["id"],
                        "url": u["url"],
                        "title": u["title"] or "",
                        "visit_count": u["visit_count"],
                        "typed_count": u["typed_count"],
                        "last_visit_time": lvt,
                        "last_visit_datetime_local": webkit_to_datetime_local(lvt, use_utc=use_utc),
                        "last_visit_display": webkit_to_display_str(lvt, use_utc=use_utc),
                        "hidden": u["hidden"],
                    })

                # Database totals
                cur_stats = self.execute_sql(
                    conn,
                    """
                    SELECT
                        (SELECT COUNT(*) FROM urls) AS total_urls,
                        (SELECT COUNT(*) FROM visits) AS total_visits,
                        (SELECT COALESCE(SUM(typed_count), 0) FROM urls) AS total_typed
                    """,
                )
                st = cur_stats.fetchone()

                return {
                    "mode": self.mode,
                    "active_db_path": self.active_db_path,
                    "target_chrome_path": self.target_chrome_path or self.active_db_path,
                    "pending_sync_count": len(self.pending_chrome_ops),
                    "last_backup_path": self.last_backup_path,
                    "stats": {
                        "total_urls": st["total_urls"],
                        "total_visits": st["total_visits"],
                        "total_typed": st["total_typed"],
                        "filtered_visits": len(visits),
                    },
                    "visits": visits,
                    "urls": urls_list,
                    "sql_log": self.sql_log[:60],
                }
            finally:
                conn.close()

    # -- Mode Switching & Working Copy Cloning --

    def switch_mode(
        self,
        mode: str,
        custom_path: str = "",
        create_if_missing: bool = False,
        auto_working_copy: bool = True,
    ) -> dict:
        """
        Switch between 'sandbox' and 'custom' database modes.
        Validates the SQLite file and automatically creates a synced working copy if locked by Chrome.
        """
        with self.lock:
            mode = (mode or "sandbox").strip().lower()
            if mode == "sandbox":
                self.mode = "sandbox"
                self.target_chrome_path = ""
                self.pending_chrome_ops.clear()
                self.ensure_sandbox_initialized()
                return {
                    "mode": self.mode,
                    "active_db_path": self.active_db_path,
                    "message": f"Switched to Sandbox Mode ({self.sandbox_path})",
                }

            if mode != "custom":
                raise ValueError("Mode must be 'sandbox' or 'custom'.")

            expanded_path = os.path.abspath(os.path.expanduser(os.path.expandvars(custom_path.strip())))
            if not expanded_path:
                raise ValueError("Please provide a valid path to a Chrome History file.")

            if not os.path.isfile(expanded_path):
                if create_if_missing:
                    os.makedirs(os.path.dirname(expanded_path), exist_ok=True)
                else:
                    raise FileNotFoundError(f"History file not found at: {expanded_path}")

            # Verify we can open and read/write (or detect lock immediately)
            try:
                conn = self._connect(expanded_path)
                try:
                    self.ensure_schema(conn)
                    self.get_table_columns(conn, "urls")
                    self.get_table_columns(conn, "visits")
                    conn.commit()
                finally:
                    conn.close()
            except DatabaseLockedError:
                if auto_working_copy:
                    return self.clone_to_working_copy(expanded_path)
                raise

            self.mode = "custom"
            self.custom_path = expanded_path
            self.target_chrome_path = expanded_path
            return {
                "mode": self.mode,
                "active_db_path": self.active_db_path,
                "target_chrome_path": self.target_chrome_path,
                "message": f"Connected to Custom Chrome History: {self.active_db_path}",
            }

    def clone_to_working_copy(self, source_path: str) -> dict:
        """
        Copy a locked or real Chrome `History` file to a local working copy,
        switch to Custom Mode on that working copy, and link it to `source_path`
        so edits auto-sync to Chrome History once Chrome closes.
        """
        with self.lock:
            expanded = os.path.abspath(os.path.expanduser(os.path.expandvars(source_path.strip())))
            if not os.path.isfile(expanded):
                raise FileNotFoundError(f"Source file not found: {expanded}")

            parent_folder = os.path.basename(os.path.dirname(expanded)) or "Chrome"
            safe_folder = re.sub(r"[^A-Za-z0-9_-]+", "_", parent_folder)
            working_copy_path = os.path.join(BASE_DIR, f"working_copy_{safe_folder}_History.db")
            if os.path.abspath(expanded).lower() != os.path.abspath(working_copy_path).lower():
                shutil.copy2(expanded, working_copy_path)
            res = self.switch_mode("custom", working_copy_path, auto_working_copy=False)
            self.target_chrome_path = expanded
            res["target_chrome_path"] = expanded
            res["message"] = (
                f"Connected to {parent_folder} Chrome History (Chrome is currently open, so loaded "
                f"live synced copy and will auto-sync edits to {expanded} when Chrome closes)."
            )
            return res


# ---------------------------------------------------------------------------
# Auto-Fetch Webpage Title Utility
# ---------------------------------------------------------------------------

def fetch_title_from_url(target_url: str, timeout: float = 5.0) -> str:
    """
    Fetch the HTML <title> from a URL using urllib.request.
    Falls back cleanly if the site blocks requests or is unreachable.
    """
    url = (target_url or "").strip()
    if not url:
        raise ValueError("URL is required.")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw_bytes = resp.read(131072)  # Read up to 128 KB
            charset = resp.headers.get_content_charset() or "utf-8"
            text = raw_bytes.decode(charset, errors="replace")
            match = re.search(r"<title[^>]*>(.*?)</title>", text, flags=re.IGNORECASE | re.DOTALL)
            if match:
                title = html.unescape(match.group(1)).strip()
                title = " ".join(title.split())
                if title:
                    return title
    except Exception as exc:
        # Derive a clean fallback title from hostname/path if offline or blocked
        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc or url
        raise RuntimeError(f"Could not fetch live page title ({exc}). Host: {host}") from exc

    parsed = urllib.parse.urlparse(url)
    return parsed.netloc or url


# ---------------------------------------------------------------------------
# Embedded Modern Frontend UI (HTML + CSS + JS)
# ---------------------------------------------------------------------------

FRONTEND_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Chrome History SQLite Studio</title>
  <style>
    :root {
      --bg-primary: #0b0f19;
      --bg-secondary: #111827;
      --bg-card: #1a2234;
      --bg-input: #0f172a;
      --border: #2a364f;
      --border-focus: #3b82f6;
      --text-main: #f1f5f9;
      --text-muted: #94a3b8;
      --accent: #3b82f6;
      --accent-hover: #2563eb;
      --success: #10b981;
      --warning: #f59e0b;
      --danger: #ef4444;
      --danger-hover: #dc2626;
      --purple: #8b5cf6;
      --font-sans: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      --font-mono: 'JetBrains Mono', 'Fira Code', Consolas, monospace;
    }

    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }

    body {
      background: var(--bg-primary);
      color: var(--text-main);
      font-family: var(--font-sans);
      line-height: 1.5;
      min-height: 100vh;
      padding-bottom: 48px;
    }

    /* Header */
    .top-bar {
      background: rgba(17, 24, 39, 0.92);
      backdrop-filter: blur(12px);
      border-bottom: 1px solid var(--border);
      padding: 14px 28px;
      position: sticky;
      top: 0;
      z-index: 100;
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 16px;
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
    }

    .brand-icon {
      width: 38px;
      height: 38px;
      border-radius: 10px;
      background: linear-gradient(135deg, #3b82f6, #8b5cf6);
      display: flex;
      align-items: center;
      justify-content: center;
      font-weight: 800;
      font-size: 18px;
      color: #fff;
      box-shadow: 0 4px 12px rgba(59, 130, 246, 0.35);
    }

    .brand h1 {
      font-size: 1.15rem;
      font-weight: 700;
      letter-spacing: -0.02em;
    }

    .brand p {
      font-size: 0.78rem;
      color: var(--text-muted);
    }

    .status-pills {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }

    .pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 5px 12px;
      border-radius: 999px;
      font-size: 0.78rem;
      font-weight: 600;
      border: 1px solid var(--border);
      background: var(--bg-card);
    }

    .pill.sandbox {
      border-color: rgba(16, 185, 129, 0.4);
      color: #34d399;
      background: rgba(16, 185, 129, 0.1);
    }

    .pill.custom {
      border-color: rgba(245, 158, 11, 0.4);
      color: #fbbf24;
      background: rgba(245, 158, 11, 0.1);
    }

    .container {
      max-width: 1480px;
      margin: 22px auto;
      padding: 0 24px;
      display: flex;
      flex-direction: column;
      gap: 20px;
    }

    /* Alert Banner for Locked DB or Notifications */
    .alert-banner {
      display: none;
      padding: 14px 18px;
      border-radius: 10px;
      border: 1px solid var(--border);
      font-size: 0.88rem;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      flex-wrap: wrap;
    }

    .alert-banner.show {
      display: flex;
    }

    .alert-banner.error {
      background: rgba(239, 68, 68, 0.12);
      border-color: rgba(239, 68, 68, 0.45);
      color: #fca5a5;
    }

    .alert-banner.success {
      background: rgba(16, 185, 129, 0.12);
      border-color: rgba(16, 185, 129, 0.45);
      color: #6ee7b7;
    }

    .alert-banner.warning {
      background: rgba(245, 158, 11, 0.12);
      border-color: rgba(245, 158, 11, 0.45);
      color: #fde68a;
    }

    /* Grid Cards */
    .grid-top {
      display: grid;
      grid-template-columns: 1fr 1.35fr;
      gap: 20px;
    }

    @media (max-width: 1100px) {
      .grid-top {
        grid-template-columns: 1fr;
      }
    }

    .card {
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 20px;
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.22);
    }

    .card-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 16px;
      padding-bottom: 12px;
      border-bottom: 1px solid var(--border);
      gap: 10px;
      flex-wrap: wrap;
    }

    .card-title {
      font-size: 1rem;
      font-weight: 700;
      display: flex;
      align-items: center;
      gap: 8px;
    }

    .badge {
      font-size: 0.72rem;
      padding: 2px 8px;
      border-radius: 6px;
      background: rgba(59, 130, 246, 0.16);
      color: #93c5fd;
      font-family: var(--font-mono);
    }

    /* Form Controls */
    .form-grid {
      display: grid;
      grid-template-columns: repeat(12, 1fr);
      gap: 12px;
    }

    .col-12 { grid-column: span 12; }
    .col-8 { grid-column: span 8; }
    .col-6 { grid-column: span 6; }
    .col-4 { grid-column: span 4; }
    .col-3 { grid-column: span 3; }

    @media (max-width: 720px) {
      .col-8, .col-6, .col-4, .col-3 { grid-column: span 12; }
    }

    label {
      display: block;
      font-size: 0.78rem;
      font-weight: 600;
      color: var(--text-muted);
      margin-bottom: 5px;
    }

    input[type="text"],
    input[type="url"],
    input[type="number"],
    input[type="datetime-local"],
    input[type="date"],
    select {
      width: 100%;
      background: var(--bg-input);
      color: var(--text-main);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 9px 12px;
      font-size: 0.86rem;
      font-family: var(--font-sans);
      transition: border-color 0.15s;
    }

    input:focus, select:focus {
      outline: none;
      border-color: var(--border-focus);
      box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.2);
    }

    .input-with-btn {
      display: flex;
      gap: 8px;
    }

    .input-with-btn input {
      flex: 1;
    }

    button {
      cursor: pointer;
      border: none;
      border-radius: 8px;
      padding: 9px 14px;
      font-size: 0.84rem;
      font-weight: 600;
      font-family: var(--font-sans);
      transition: all 0.15s ease;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      white-space: nowrap;
    }

    .btn-primary {
      background: var(--accent);
      color: #fff;
    }
    .btn-primary:hover {
      background: var(--accent-hover);
    }

    .btn-secondary {
      background: #263248;
      color: var(--text-main);
      border: 1px solid var(--border);
    }
    .btn-secondary:hover {
      background: #334155;
    }

    .btn-success {
      background: var(--success);
      color: #fff;
    }
    .btn-success:hover {
      background: #059669;
    }

    .btn-danger {
      background: rgba(239, 68, 68, 0.15);
      color: #fca5a5;
      border: 1px solid rgba(239, 68, 68, 0.35);
    }
    .btn-danger:hover {
      background: var(--danger);
      color: #fff;
    }

    .btn-sm {
      padding: 5px 10px;
      font-size: 0.76rem;
      border-radius: 6px;
    }

    .preset-group {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      margin-top: 6px;
    }

    /* Mode Selector Tabs */
    .mode-tabs {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
      margin-bottom: 14px;
    }

    .mode-tab {
      padding: 11px 14px;
      border-radius: 9px;
      border: 1px solid var(--border);
      background: var(--bg-input);
      cursor: pointer;
      text-align: left;
      transition: all 0.15s;
    }

    .mode-tab.active {
      border-color: var(--accent);
      background: rgba(59, 130, 246, 0.12);
    }

    .mode-tab-title {
      font-size: 0.86rem;
      font-weight: 700;
      color: var(--text-main);
    }

    .mode-tab-desc {
      font-size: 0.74rem;
      color: var(--text-muted);
      margin-top: 2px;
    }

    /* Stats Strip */
    .stats-row {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 12px;
      margin-top: 14px;
    }

    .stat-box {
      background: var(--bg-input);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 10px 12px;
    }

    .stat-label {
      font-size: 0.72rem;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }

    .stat-value {
      font-size: 1.25rem;
      font-weight: 700;
      font-family: var(--font-mono);
      margin-top: 2px;
    }

    /* Converter preview box */
    .webkit-preview {
      margin-top: 10px;
      padding: 9px 12px;
      background: var(--bg-input);
      border: 1px dashed var(--border);
      border-radius: 8px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 8px;
      font-size: 0.79rem;
    }

    .mono {
      font-family: var(--font-mono);
    }

    /* Table Styles */
    .table-wrapper {
      overflow-x: auto;
      border: 1px solid var(--border);
      border-radius: 10px;
      background: var(--bg-secondary);
    }

    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.83rem;
    }

    th {
      background: #161f33;
      color: var(--text-muted);
      font-weight: 600;
      text-align: left;
      padding: 11px 12px;
      border-bottom: 1px solid var(--border);
      white-space: nowrap;
      font-size: 0.76rem;
      text-transform: uppercase;
      letter-spacing: 0.03em;
    }

    td {
      padding: 10px 12px;
      border-bottom: 1px solid rgba(42, 54, 79, 0.55);
      vertical-align: middle;
    }

    tr:hover td {
      background: rgba(59, 130, 246, 0.04);
    }

    .url-cell {
      max-width: 340px;
    }

    .url-link {
      color: #60a5fa;
      text-decoration: none;
      word-break: break-all;
      font-family: var(--font-mono);
      font-size: 0.78rem;
    }
    .url-link:hover {
      text-decoration: underline;
    }

    .title-text {
      font-weight: 600;
      color: var(--text-main);
      margin-bottom: 2px;
      word-break: break-word;
    }

    .transition-tag {
      display: inline-block;
      padding: 2px 8px;
      border-radius: 5px;
      font-size: 0.73rem;
      font-weight: 600;
      background: rgba(139, 92, 246, 0.16);
      color: #c4b5fd;
      border: 1px solid rgba(139, 92, 246, 0.3);
    }

    .actions-cell {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
    }

    /* Inline Edit Inputs inside Table */
    .inline-input {
      padding: 5px 8px !important;
      font-size: 0.79rem !important;
      margin-bottom: 4px;
    }

    /* SQL Log Panel */
    .sql-panel {
      background: #090d16;
      border: 1px solid var(--border);
      border-radius: 10px;
      max-height: 320px;
      overflow-y: auto;
      padding: 12px;
      font-family: var(--font-mono);
      font-size: 0.78rem;
    }

    .sql-entry {
      padding: 8px 10px;
      border-bottom: 1px solid rgba(42, 54, 79, 0.45);
      display: flex;
      align-items: flex-start;
      gap: 10px;
    }

    .sql-entry:last-child {
      border-bottom: none;
    }

    .sql-verb {
      padding: 2px 6px;
      border-radius: 4px;
      font-size: 0.7rem;
      font-weight: 700;
      min-width: 58px;
      text-align: center;
    }

    .sql-verb.SELECT { background: rgba(59, 130, 246, 0.2); color: #93c5fd; }
    .sql-verb.INSERT { background: rgba(16, 185, 129, 0.2); color: #6ee7b7; }
    .sql-verb.UPDATE { background: rgba(245, 158, 11, 0.2); color: #fcd34d; }
    .sql-verb.DELETE { background: rgba(239, 68, 68, 0.2); color: #fca5a5; }
    .sql-verb.PRAGMA, .sql-verb.CREATE { background: rgba(139, 92, 246, 0.2); color: #c4b5fd; }

    .sql-code {
      flex: 1;
      color: #e2e8f0;
      word-break: break-all;
    }

    .sql-meta {
      color: var(--text-muted);
      font-size: 0.72rem;
      white-space: nowrap;
    }

    .subview-tabs {
      display: flex;
      gap: 8px;
    }
  </style>
</head>
<body>

  <!-- Top Navigation & Status Bar -->
  <header class="top-bar">
    <div class="brand">
      <div class="brand-icon">CH</div>
      <div>
        <h1>Chrome History SQLite Studio</h1>
        <p>View, Add, Backdate &amp; Synchronize <code>urls</code> and <code>visits</code> Records</p>
      </div>
    </div>
    <div class="status-pills">
      <span id="modePill" class="pill sandbox">Mode: Sandbox DB</span>
      <span id="dbPathPill" class="pill mono">sandbox_History.db</span>
      <span id="backupPill" class="pill" style="display:none;">Last Backup: None</span>
    </div>
  </header>

  <main class="container">

    <!-- Dynamic Notification / Lock Warning Banner -->
    <div id="alertBanner" class="alert-banner">
      <div id="alertMessage"></div>
      <div id="alertActions" style="display:flex; gap:8px;"></div>
    </div>

    <!-- Top Grid: Left = Database Mode & WebKit Converter, Right = Add Link & Backdate Form -->
    <div class="grid-top">

      <!-- Card 1: Database Mode & Connection -->
      <section class="card">
        <div class="card-header">
          <div class="card-title">
            <span>1. Database Mode &amp; Target File</span>
          </div>
          <button class="btn-secondary btn-sm" onclick="reseedSandbox()" title="Reset Sandbox DB with sample Chrome history">Reset Sandbox Seed</button>
        </div>

        <div class="mode-tabs">
          <div id="tabSandbox" class="mode-tab active" onclick="selectModeTab('sandbox')">
            <div class="mode-tab-title">Sandbox Mode (Default)</div>
            <div class="mode-tab-desc">Safe local <code>sandbox_History.db</code> matching Chrome schema</div>
          </div>
          <div id="tabCustom" class="mode-tab" onclick="selectModeTab('custom')">
            <div class="mode-tab-title">Custom / Real Chrome History</div>
            <div class="mode-tab-desc">Auto-creates <code>.bak</code> backup before every write</div>
          </div>
        </div>

        <div id="customModeControls" style="display:none; margin-bottom:14px;">
          <div class="form-grid">
            <div class="col-12">
              <label for="osPathPreset">Auto-Detected &amp; Default OS Chrome <code>History</code> Paths</label>
              <select id="osPathPreset" onchange="applyPresetPath()">
                <option value="">-- Select a detected Chrome profile or OS default path --</option>
              </select>
            </div>
            <div class="col-12">
              <label for="customDbPathInput">Chrome SQLite <code>History</code> File Path</label>
              <div class="input-with-btn">
                <input type="text" id="customDbPathInput" placeholder="e.g. C:\Users\You\AppData\Local\Google\Chrome\User Data\Default\History" />
                <button class="btn-primary" onclick="connectCustomDb()">Connect</button>
                <button class="btn-success" onclick="forceSyncChrome()" title="Closes Chrome and writes all changes directly into your real Chrome History file">⚡ Sync to Chrome (Close Chrome)</button>
              </div>
            </div>
          </div>
        </div>

        <!-- Database Statistics -->
        <div class="stats-row">
          <div class="stat-box">
            <div class="stat-label">Total URLs</div>
            <div class="stat-value" id="statTotalUrls">0</div>
          </div>
          <div class="stat-box">
            <div class="stat-label">Total Visits</div>
            <div class="stat-value" id="statTotalVisits">0</div>
          </div>
          <div class="stat-box">
            <div class="stat-label">Typed Count</div>
            <div class="stat-value" id="statTotalTyped">0</div>
          </div>
          <div class="stat-box">
            <div class="stat-label">Shown Visits</div>
            <div class="stat-value" id="statFilteredVisits">0</div>
          </div>
        </div>

        <!-- Interactive WebKit Timestamp Calculator -->
        <div style="margin-top: 16px; padding-top: 14px; border-top: 1px solid var(--border);">
          <label>Interactive WebKit / Chrome 17-Digit Timestamp Converter</label>
          <div class="form-grid" style="margin-top: 6px;">
            <div class="col-6">
              <input type="datetime-local" id="calcDatetime" step="1" oninput="convertCalcFromDate()" />
            </div>
            <div class="col-6">
              <input type="text" id="calcWebkit" class="mono" placeholder="17-digit WebKit timestamp" oninput="convertCalcFromWebkit()" />
            </div>
          </div>
          <div style="font-size:0.73rem; color:var(--text-muted); margin-top:5px;">
            Formula: <code>webkit = int((unix_seconds + 11644473600) * 1_000_000)</code> &bull; Epoch: <code>1601-01-01 00:00:00 UTC</code>
          </div>
        </div>
      </section>

      <!-- Card 2: Add Link or Search Query with Custom Date & Time -->
      <section class="card">
        <div class="card-header">
          <div class="card-title">
            <span>2. Add Link or Search Query with Custom Date &amp; Time</span>
            <span class="badge">Auto-syncs urls, visits &amp; keyword_search_terms</span>
          </div>
        </div>

        <form id="addVisitForm" onsubmit="submitAddVisit(event)">
          <div class="form-grid">
            <div class="col-4">
              <label for="addEntryType">Entry Mode</label>
              <select id="addEntryType">
                <option value="auto">Auto-Detect (URL or Google Search)</option>
                <option value="google_search">Google Search Query</option>
                <option value="youtube_search">YouTube Search Query</option>
                <option value="direct_url">Direct Website URL</option>
              </select>
            </div>
            <div class="col-8">
              <label for="addUrl">URL or Search Query (e.g. <code>how to make egg curry</code>) *</label>
              <div class="input-with-btn">
                <input type="text" id="addUrl" required placeholder="https://example.com/article OR how to make egg curry" />
                <button type="button" class="btn-secondary" id="btnFetchTitle" onclick="autoFetchTitle()">Auto-Fetch Title</button>
              </div>
            </div>

            <div class="col-12">
              <label for="addTitle">Page Title</label>
              <input type="text" id="addTitle" placeholder="Leave blank to auto-generate (e.g. 'how to make egg curry - Google Search') or auto-fetch" />
            </div>

            <div class="col-6">
              <label for="addDatetime">Visit Date &amp; Time (down to seconds) *</label>
              <input type="datetime-local" id="addDatetime" step="1" required oninput="updateAddWebkitPreview()" />
              <div class="preset-group">
                <button type="button" class="btn-secondary btn-sm" onclick="setAddDateOffset(0)">Now</button>
                <button type="button" class="btn-secondary btn-sm" onclick="setAddDateOffset(-3600)">-1h</button>
                <button type="button" class="btn-secondary btn-sm" onclick="setAddDateOffset(-86400)">-1d</button>
                <button type="button" class="btn-secondary btn-sm" onclick="setAddDateOffset(-86400*7)">-1w</button>
                <button type="button" class="btn-secondary btn-sm" onclick="setAddDateOffset(-86400*30)">-30d</button>
                <button type="button" class="btn-secondary btn-sm" onclick="setAddDateOffset(-86400*365)">-1y</button>
              </div>
            </div>

            <div class="col-4">
              <label for="addTransition">Transition Type</label>
              <select id="addTransition">
                <option value="805306368">Clicked Link (805306368)</option>
                <option value="838860805">Omnibox Search (838860805)</option>
                <option value="805306369">Typed URL (805306369)</option>
                <option value="805306370">Bookmark (805306370)</option>
                <option value="805306376">Reload (805306376)</option>
              </select>
            </div>

            <div class="col-2" style="grid-column: span 2;">
              <label for="addDuration">Duration (s)</label>
              <input type="number" id="addDuration" min="0" step="1" value="30" />
            </div>

            <div class="col-12">
              <div class="webkit-preview">
                <span>Target 17-Digit WebKit Timestamp: <strong id="addWebkitPreview" class="mono">--</strong></span>
                <button type="submit" class="btn-primary">+ Add Visit &amp; Sync URL Record</button>
              </div>
            </div>
          </div>
        </form>
      </section>

    </div>

    <!-- Card 3: Live Search, Filter & History Table (Visits & URLs) -->
    <section class="card">
      <div class="card-header">
        <div class="card-title">
          <span>3. Chrome History Records</span>
          <span class="badge">Inline Backdate &amp; Edit</span>
        </div>
        <div class="subview-tabs">
          <button id="btnViewVisits" class="btn-primary btn-sm" onclick="switchTableView('visits')">Visits Table (Joined)</button>
          <button id="btnViewUrls" class="btn-secondary btn-sm" onclick="switchTableView('urls')">URLs Summary Table</button>
        </div>
      </div>

      <!-- Search & Date Filter Bar -->
      <div class="form-grid" style="margin-bottom: 16px;">
        <div class="col-4">
          <label for="filterSearch">Live Search (URL or Title substring)</label>
          <input type="text" id="filterSearch" placeholder="Filter by domain, URL, or page title..." oninput="debouncedLoadHistory()" />
        </div>
        <div class="col-3">
          <label for="filterStartDate">From Date / Time</label>
          <input type="datetime-local" id="filterStartDate" step="1" onchange="loadHistory()" />
        </div>
        <div class="col-3">
          <label for="filterEndDate">To Date / Time</label>
          <input type="datetime-local" id="filterEndDate" step="1" onchange="loadHistory()" />
        </div>
        <div class="col-2" style="grid-column: span 2; display:flex; align-items:flex-end; gap:8px;">
          <button class="btn-secondary" style="width:100%;" onclick="clearFilters()">Clear Filters</button>
        </div>
      </div>

      <!-- Visits Table View -->
      <div id="visitsTableContainer" class="table-wrapper">
        <table>
          <thead>
            <tr>
              <th>Visit ID</th>
              <th>Title &amp; URL</th>
              <th>Visit Date &amp; Time (Local)</th>
              <th>WebKit Timestamp (17-digit)</th>
              <th>Transition</th>
              <th>Duration</th>
              <th>URL Stats (Visits / Typed)</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody id="visitsTableBody">
            <tr><td colspan="8" style="text-align:center; padding:24px; color:var(--text-muted);">Loading history...</td></tr>
          </tbody>
        </table>
      </div>

      <!-- URLs Table View -->
      <div id="urlsTableContainer" class="table-wrapper" style="display:none;">
        <table>
          <thead>
            <tr>
              <th>URL ID</th>
              <th>Title &amp; URL</th>
              <th>visit_count</th>
              <th>typed_count</th>
              <th>last_visit_time (Local)</th>
              <th>last_visit_time (WebKit)</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody id="urlsTableBody"></tbody>
        </table>
      </div>
    </section>

    <!-- Card 4: Recent SQL Executed Panel -->
    <section class="card">
      <div class="card-header">
        <div class="card-title">
          <span>4. Recent SQL Executed</span>
          <span class="badge" id="sqlLogCountBadge">0 queries</span>
        </div>
        <div style="display:flex; gap:8px;">
          <button class="btn-secondary btn-sm" onclick="loadHistory()">Refresh</button>
          <button class="btn-secondary btn-sm" onclick="clearSqlLog()">Clear Log</button>
        </div>
      </div>
      <div id="sqlLogPanel" class="sql-panel"></div>
    </section>

  </main>

  <script>
    const WEBKIT_OFFSET = 11644473600;
    let currentState = {
      mode: 'sandbox',
      active_db_path: '',
      visits: [],
      urls: [],
      editingVisitId: null,
      tableView: 'visits'
    };
    let searchTimer = null;

    // --- WebKit Timestamp Conversion in JS (matches Python formula) ---
    function toDatetimeLocalString(dateObj) {
      const pad = (n) => String(n).padStart(2, '0');
      return (
        dateObj.getFullYear() + '-' +
        pad(dateObj.getMonth() + 1) + '-' +
        pad(dateObj.getDate()) + 'T' +
        pad(dateObj.getHours()) + ':' +
        pad(dateObj.getMinutes()) + ':' +
        pad(dateObj.getSeconds())
      );
    }

    function datetimeLocalToWebkit(dtStr) {
      if (!dtStr) return 0;
      const d = new Date(dtStr);
      if (isNaN(d.getTime())) return 0;
      const unixSeconds = d.getTime() / 1000;
      return Math.round((unixSeconds + WEBKIT_OFFSET) * 1000000);
    }

    function webkitToDatetimeLocal(webkitVal) {
      const wk = Number(webkitVal);
      if (!wk || wk <= 0) return '';
      const unixSeconds = (wk / 1000000) - WEBKIT_OFFSET;
      const d = new Date(unixSeconds * 1000);
      if (isNaN(d.getTime())) return '';
      return toDatetimeLocalString(d);
    }

    // --- Interactive Converter Widget ---
    function convertCalcFromDate() {
      const dtStr = document.getElementById('calcDatetime').value;
      const wk = datetimeLocalToWebkit(dtStr);
      document.getElementById('calcWebkit').value = wk ? String(wk) : '';
    }

    function convertCalcFromWebkit() {
      const wkStr = document.getElementById('calcWebkit').value.trim();
      const dtStr = webkitToDatetimeLocal(wkStr);
      if (dtStr) {
        document.getElementById('calcDatetime').value = dtStr;
      }
    }

    // --- Add Form Date Presets ---
    function setAddDateOffset(offsetSeconds) {
      const d = new Date(Date.now() + offsetSeconds * 1000);
      document.getElementById('addDatetime').value = toDatetimeLocalString(d);
      updateAddWebkitPreview();
    }

    function updateAddWebkitPreview() {
      const dtVal = document.getElementById('addDatetime').value;
      const wk = datetimeLocalToWebkit(dtVal);
      document.getElementById('addWebkitPreview').textContent = wk ? String(wk) : '--';
    }

    // --- Notifications & Lock Banner ---
    function showAlert(message, type = 'success', actionsHtml = '') {
      const banner = document.getElementById('alertBanner');
      const msgEl = document.getElementById('alertMessage');
      const actEl = document.getElementById('alertActions');
      banner.className = 'alert-banner show ' + type;
      msgEl.innerHTML = message;
      actEl.innerHTML = actionsHtml;
    }

    function hideAlert() {
      document.getElementById('alertBanner').className = 'alert-banner';
    }

    function escapeHtml(str) {
      if (str === null || str === undefined) return '';
      return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }

    // --- API Wrapper ---
    async function apiCall(path, options = {}) {
      const resp = await fetch(path, {
        headers: { 'Content-Type': 'application/json' },
        ...options
      });
      const data = await resp.json();
      if (!resp.ok) {
        const err = new Error(data.message || data.error || 'Request failed');
        err.data = data;
        err.status = resp.status;
        throw err;
      }
      return data;
    }

    function handleApiError(err, candidatePath = '') {
      if (err.data && err.data.error_code === 'DB_LOCKED') {
        const lockedPath = err.data.db_path || candidatePath || document.getElementById('customDbPathInput').value;
        const safePath = escapeHtml(lockedPath).replace(/\\/g, '\\\\');
        showAlert(
          `<strong>Database Locked:</strong> ${escapeHtml(err.message)}`,
          'error',
          `<button class="btn-primary btn-sm" onclick="cloneWorkingCopy('${safePath}')">Edit a Working Copy Instead</button>
           <button class="btn-secondary btn-sm" onclick="hideAlert()">Dismiss</button>`
        );
      } else {
        showAlert(`<strong>Error:</strong> ${escapeHtml(err.message)}`, 'error',
          `<button class="btn-secondary btn-sm" onclick="hideAlert()">Dismiss</button>`);
      }
    }

    // --- OS Chrome Paths Loading ---
    async function loadOsPaths() {
      try {
        const info = await apiCall('/api/os-paths');
        const sel = document.getElementById('osPathPreset');
        sel.innerHTML = '<option value="">-- Select a detected Chrome profile or default OS path --</option>';
        for (const item of info.detected_paths) {
          const status = item.exists ? '✓ Found on disk' : 'Template path';
          const opt = document.createElement('option');
          opt.value = item.path;
          opt.textContent = `[${status}] ${item.profile}: ${item.path}`;
          sel.appendChild(opt);
        }
      } catch (e) {
        console.error(e);
      }
    }

    async function applyPresetPath() {
      const val = document.getElementById('osPathPreset').value;
      if (val) {
        document.getElementById('customDbPathInput').value = val;
        await connectCustomDb();
      }
    }

    // --- Mode Selection ---
    async function selectModeTab(mode) {
      document.getElementById('tabSandbox').classList.toggle('active', mode === 'sandbox');
      document.getElementById('tabCustom').classList.toggle('active', mode === 'custom');
      document.getElementById('customModeControls').style.display = mode === 'custom' ? 'block' : 'none';

      if (mode === 'sandbox' && currentState.mode !== 'sandbox') {
        try {
          const res = await apiCall('/api/db/mode', {
            method: 'POST',
            body: JSON.stringify({ mode: 'sandbox' })
          });
          showAlert(escapeHtml(res.message), 'success');
          await loadHistory();
        } catch (err) {
          handleApiError(err);
        }
      }
    }

    async function connectCustomDb() {
      const path = document.getElementById('customDbPathInput').value.trim();
      if (!path) {
        showAlert('Please enter or select a path to a Chrome SQLite History file.', 'warning');
        return;
      }
      try {
        const res = await apiCall('/api/db/mode', {
          method: 'POST',
          body: JSON.stringify({ mode: 'custom', path })
        });
        showAlert(escapeHtml(res.message), 'success');
        await loadHistory();
      } catch (err) {
        handleApiError(err, path);
      }
    }

    async function cloneWorkingCopyFromInput() {
      const path = document.getElementById('customDbPathInput').value.trim();
      if (!path) {
        showAlert('Please enter or select a source Chrome History path first.', 'warning');
        return;
      }
      await cloneWorkingCopy(path);
    }

    async function cloneWorkingCopy(sourcePath) {
      try {
        const res = await apiCall('/api/db/clone', {
          method: 'POST',
          body: JSON.stringify({ path: sourcePath })
        });
        document.getElementById('customDbPathInput').value = res.active_db_path;
        showAlert(`Created & connected to working copy: <code>${escapeHtml(res.active_db_path)}</code>`, 'success');
        await loadHistory();
      } catch (err) {
        handleApiError(err, sourcePath);
      }
    }

    async function forceSyncChrome() {
      try {
        const res = await apiCall('/api/db/force-sync-chrome', { method: 'POST', body: '{}' });
        showAlert(`<strong>Synced to Chrome!</strong> ${escapeHtml(res.message)}`, 'success');
        await loadHistory();
      } catch (err) {
        handleApiError(err);
      }
    }

    async function reseedSandbox() {
      try {
        await apiCall('/api/db/reseed', { method: 'POST', body: '{}' });
        showAlert('Sandbox database reset and re-seeded with sample Chrome History records.', 'success');
        await loadHistory();
      } catch (err) {
        handleApiError(err);
      }
    }

    // --- Auto-Fetch Title ---
    async function autoFetchTitle() {
      const urlInput = document.getElementById('addUrl');
      const titleInput = document.getElementById('addTitle');
      const btn = document.getElementById('btnFetchTitle');
      const url = urlInput.value.trim();
      if (!url) {
        showAlert('Please enter a URL first to auto-fetch its title.', 'warning');
        return;
      }
      btn.disabled = true;
      btn.textContent = 'Fetching...';
      try {
        const res = await apiCall(`/api/fetch-title?url=${encodeURIComponent(url)}`);
        if (res.title) {
          titleInput.value = res.title;
          showAlert(`Fetched page title: <strong>${escapeHtml(res.title)}</strong>`, 'success');
        }
      } catch (err) {
        handleApiError(err);
      } finally {
        btn.disabled = false;
        btn.textContent = 'Auto-Fetch Title from URL';
      }
    }

    // --- Add Visit Submit ---
    async function submitAddVisit(e) {
      e.preventDefault();
      const entry_type = document.getElementById('addEntryType').value;
      const url = document.getElementById('addUrl').value.trim();
      const title = document.getElementById('addTitle').value.trim();
      const datetime_local = document.getElementById('addDatetime').value;
      const transition = Number(document.getElementById('addTransition').value);
      const duration_seconds = Number(document.getElementById('addDuration').value || 0);

      try {
        const res = await apiCall('/api/visits', {
          method: 'POST',
          body: JSON.stringify({ url, title, datetime_local, transition, duration_seconds, entry_type })
        });
        const finalUrl = res.resolved_url || url;
        const finalTitle = res.resolved_title || title;
        let msg = `Added visit #${res.visit_id}: <strong>${escapeHtml(finalTitle)}</strong> (<code>${escapeHtml(finalUrl)}</code>, WebKit: <code>${res.webkit_timestamp}</code>).`;
        if (res.search_term) {
          msg += ` Saved search term <code>${escapeHtml(res.search_term)}</code> to <code>keyword_search_terms</code>.`;
        }
        if (res.backup_created) {
          msg += ` Backup saved: <code>${escapeHtml(res.backup_created)}</code>`;
        }
        showAlert(msg, 'success');
        document.getElementById('addUrl').value = '';
        document.getElementById('addTitle').value = '';
        await loadHistory();
      } catch (err) {
        handleApiError(err);
      }
    }

    // --- Inline Editing & Backdating ---
    function startInlineEdit(visitId) {
      currentState.editingVisitId = visitId;
      renderVisitsTable();
    }

    function cancelInlineEdit() {
      currentState.editingVisitId = null;
      renderVisitsTable();
    }

    async function saveInlineEdit(visitId) {
      const url = document.getElementById(`edit-url-${visitId}`).value.trim();
      const title = document.getElementById(`edit-title-${visitId}`).value.trim();
      const datetime_local = document.getElementById(`edit-dt-${visitId}`).value;
      const transition = Number(document.getElementById(`edit-trans-${visitId}`).value);
      const duration_seconds = Number(document.getElementById(`edit-dur-${visitId}`).value || 0);

      try {
        const res = await apiCall(`/api/visits/${visitId}`, {
          method: 'PUT',
          body: JSON.stringify({ url, title, datetime_local, transition, duration_seconds })
        });
        currentState.editingVisitId = null;
        let msg = `Updated visit #${visitId} &rarr; WebKit timestamp <code>${res.webkit_timestamp}</code>. Synchronized <code>urls</code> table.`;
        if (res.backup_created) {
          msg += ` Backup: <code>${escapeHtml(res.backup_created)}</code>`;
        }
        showAlert(msg, 'success');
        await loadHistory();
      } catch (err) {
        handleApiError(err);
      }
    }

    // --- Deleting Visits or Full URLs ---
    async function deleteSingleVisit(visitId) {
      try {
        const res = await apiCall(`/api/visits/${visitId}`, { method: 'DELETE' });
        let msg = `Deleted visit #${visitId}. `;
        if (res.url_deleted) {
          msg += `Parent URL #${res.url_id} reached 0 visits and was automatically removed.`;
        } else {
          msg += `Parent URL #${res.url_id} synced (visit_count = ${res.url_record.visit_count}).`;
        }
        showAlert(msg, 'success');
        await loadHistory();
      } catch (err) {
        handleApiError(err);
      }
    }

    async function deleteFullUrl(urlId) {
      try {
        const res = await apiCall(`/api/urls/${urlId}`, { method: 'DELETE' });
        showAlert(
          `Deleted URL #${urlId} (<code>${escapeHtml(res.deleted_url)}</code>) and ${res.visits_deleted} associated visit(s).`,
          'success'
        );
        await loadHistory();
      } catch (err) {
        handleApiError(err);
      }
    }

    // --- Load & Render History ---
    function debouncedLoadHistory() {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(loadHistory, 180);
    }

    function clearFilters() {
      document.getElementById('filterSearch').value = '';
      document.getElementById('filterStartDate').value = '';
      document.getElementById('filterEndDate').value = '';
      loadHistory();
    }

    function switchTableView(view) {
      currentState.tableView = view;
      document.getElementById('visitsTableContainer').style.display = view === 'visits' ? 'block' : 'none';
      document.getElementById('urlsTableContainer').style.display = view === 'urls' ? 'block' : 'none';
      document.getElementById('btnViewVisits').className = view === 'visits' ? 'btn-primary btn-sm' : 'btn-secondary btn-sm';
      document.getElementById('btnViewUrls').className = view === 'urls' ? 'btn-primary btn-sm' : 'btn-secondary btn-sm';
    }

    async function loadHistory() {
      const search = document.getElementById('filterSearch').value.trim();
      const start_date = document.getElementById('filterStartDate').value;
      const end_date = document.getElementById('filterEndDate').value;

      const qs = new URLSearchParams({ search, start_date, end_date }).toString();
      try {
        const data = await apiCall(`/api/history?${qs}`);
        currentState.mode = data.mode;
        currentState.active_db_path = data.active_db_path;
        currentState.visits = data.visits || [];
        currentState.urls = data.urls || [];

        // Update Top Status Bar & Mode Tabs
        const modePill = document.getElementById('modePill');
        modePill.className = 'pill ' + data.mode;
        modePill.textContent = data.mode === 'sandbox' ? 'Mode: Sandbox DB' : 'Mode: Custom Chrome History';

        document.getElementById('dbPathPill').textContent = data.target_chrome_path || data.active_db_path;
        document.getElementById('tabSandbox').classList.toggle('active', data.mode === 'sandbox');
        document.getElementById('tabCustom').classList.toggle('active', data.mode === 'custom');
        document.getElementById('customModeControls').style.display = data.mode === 'custom' ? 'block' : 'none';
        if (data.mode === 'custom') {
          const targetPath = data.target_chrome_path || data.active_db_path;
          const pathInput = document.getElementById('customDbPathInput');
          if (!pathInput.value) pathInput.value = targetPath;
          const presetSel = document.getElementById('osPathPreset');
          for (const opt of presetSel.options) {
            if (opt.value && opt.value.toLowerCase() === targetPath.toLowerCase()) {
              presetSel.value = opt.value;
              break;
            }
          }
        }

        const backupPill = document.getElementById('backupPill');
        if (data.last_backup_path) {
          backupPill.style.display = 'inline-flex';
          backupPill.textContent = 'Backup: ' + data.last_backup_path.split(/[\\/]/).pop();
          backupPill.title = data.last_backup_path;
        } else {
          backupPill.style.display = 'none';
        }

        // Update Stats
        document.getElementById('statTotalUrls').textContent = data.stats.total_urls;
        document.getElementById('statTotalVisits').textContent = data.stats.total_visits;
        document.getElementById('statTotalTyped').textContent = data.stats.total_typed;
        document.getElementById('statFilteredVisits').textContent = data.stats.filtered_visits;

        renderVisitsTable();
        renderUrlsTable();
        renderSqlLog(data.sql_log || []);
      } catch (err) {
        handleApiError(err);
      }
    }

    function renderVisitsTable() {
      const tbody = document.getElementById('visitsTableBody');
      const visits = currentState.visits;
      if (!visits.length) {
        tbody.innerHTML = '<tr><td colspan="8" style="text-align:center; padding:24px; color:var(--text-muted);">No matching history visits found.</td></tr>';
        return;
      }

      tbody.innerHTML = visits.map(v => {
        const isEditing = currentState.editingVisitId === v.visit_id;
        if (isEditing) {
          return `
            <tr style="background: rgba(59, 130, 246, 0.09);">
              <td class="mono">#${v.visit_id}<br/><small style="color:var(--text-muted)">url_id: ${v.url_id}</small></td>
              <td class="url-cell">
                <input type="text" id="edit-title-${v.visit_id}" class="inline-input" value="${escapeHtml(v.title)}" placeholder="Page Title" />
                <input type="text" id="edit-url-${v.visit_id}" class="inline-input mono" value="${escapeHtml(v.url)}" placeholder="https://..." />
              </td>
              <td>
                <input type="datetime-local" id="edit-dt-${v.visit_id}" class="inline-input" step="1" value="${escapeHtml(v.visit_datetime_local)}" />
              </td>
              <td class="mono" style="font-size:0.75rem; color:var(--text-muted);">
                ${v.visit_time}<br/>(auto-recalculated)
              </td>
              <td>
                <select id="edit-trans-${v.visit_id}" class="inline-input">
                  <option value="805306368" ${v.transition === 805306368 ? 'selected' : ''}>Clicked Link</option>
                  <option value="805306369" ${v.transition === 805306369 ? 'selected' : ''}>Typed URL</option>
                  <option value="805306370" ${v.transition === 805306370 ? 'selected' : ''}>Bookmark</option>
                  <option value="805306376" ${v.transition === 805306376 ? 'selected' : ''}>Reload</option>
                </select>
              </td>
              <td>
                <input type="number" id="edit-dur-${v.visit_id}" class="inline-input" style="width:74px;" min="0" step="1" value="${v.visit_duration_sec}" /> s
              </td>
              <td class="mono" style="font-size:0.76rem;">
                visits: <strong>${v.visit_count}</strong><br/>typed: ${v.typed_count}
              </td>
              <td>
                <div class="actions-cell">
                  <button class="btn-success btn-sm" onclick="saveInlineEdit(${v.visit_id})">Save</button>
                  <button class="btn-secondary btn-sm" onclick="cancelInlineEdit()">Cancel</button>
                </div>
              </td>
            </tr>
          `;
        }

        return `
          <tr>
            <td class="mono">#${v.visit_id}<br/><small style="color:var(--text-muted)">url_id: ${v.url_id}</small></td>
            <td class="url-cell">
              <div class="title-text">${escapeHtml(v.title || '(No title)')}</div>
              <a class="url-link" href="${escapeHtml(v.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(v.url)}</a>
            </td>
            <td class="mono" style="white-space:nowrap;">
              <strong>${escapeHtml(v.visit_display)}</strong>
              <div style="font-size:0.71rem; color:var(--text-muted);">Last URL visit: ${escapeHtml(v.last_visit_display)}</div>
            </td>
            <td class="mono" style="font-size:0.76rem; color:#93c5fd;">${v.visit_time}</td>
            <td><span class="transition-tag" title="Code: ${v.transition}">${escapeHtml(v.transition_label)}</span></td>
            <td class="mono">${v.visit_duration_sec}s</td>
            <td class="mono" style="font-size:0.76rem;">
              visits: <strong>${v.visit_count}</strong><br/>typed: ${v.typed_count}
            </td>
            <td>
              <div class="actions-cell">
                <button class="btn-secondary btn-sm" onclick="startInlineEdit(${v.visit_id})">Edit / Backdate</button>
                <button class="btn-danger btn-sm" onclick="deleteSingleVisit(${v.visit_id})" title="Delete only this visit">Del Visit</button>
                <button class="btn-danger btn-sm" onclick="deleteFullUrl(${v.url_id})" title="Delete URL and all ${v.visit_count} visit(s)">Del Full URL</button>
              </div>
            </td>
          </tr>
        `;
      }).join('');
    }

    function renderUrlsTable() {
      const tbody = document.getElementById('urlsTableBody');
      const urls = currentState.urls;
      if (!urls.length) {
        tbody.innerHTML = '<tr><td colspan="7" style="text-align:center; padding:24px; color:var(--text-muted);">No URL records found.</td></tr>';
        return;
      }
      tbody.innerHTML = urls.map(u => `
        <tr>
          <td class="mono">#${u.id}</td>
          <td class="url-cell">
            <div class="title-text">${escapeHtml(u.title || '(No title)')}</div>
            <a class="url-link" href="${escapeHtml(u.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(u.url)}</a>
          </td>
          <td class="mono"><strong>${u.visit_count}</strong></td>
          <td class="mono">${u.typed_count}</td>
          <td class="mono">${escapeHtml(u.last_visit_display)}</td>
          <td class="mono" style="color:#93c5fd;">${u.last_visit_time}</td>
          <td>
            <button class="btn-danger btn-sm" onclick="deleteFullUrl(${u.id})">Delete Full URL &amp; All Visits</button>
          </td>
        </tr>
      `).join('');
    }

    function renderSqlLog(logs) {
      const panel = document.getElementById('sqlLogPanel');
      document.getElementById('sqlLogCountBadge').textContent = `${logs.length} recent queries`;
      if (!logs.length) {
        panel.innerHTML = '<div style="color:var(--text-muted); padding:8px;">No SQL queries logged yet.</div>';
        return;
      }
      panel.innerHTML = logs.map(item => `
        <div class="sql-entry">
          <span class="sql-verb ${escapeHtml(item.verb)}">${escapeHtml(item.verb)}</span>
          <div class="sql-code">${escapeHtml(item.sql)}</div>
          <div class="sql-meta">${escapeHtml(item.timestamp)} &bull; ${item.duration_ms}ms</div>
        </div>
      `).join('');
    }

    async function clearSqlLog() {
      await apiCall('/api/sql-log/clear', { method: 'POST', body: '{}' });
      await loadHistory();
    }

    // --- Initial Boot ---
    window.addEventListener('DOMContentLoaded', () => {
      setAddDateOffset(0);
      document.getElementById('calcDatetime').value = toDatetimeLocalString(new Date());
      convertCalcFromDate();
      loadOsPaths();
      loadHistory();
    });
  </script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# HTTP Request Handler & REST API
# ---------------------------------------------------------------------------

class HistoryRequestHandler(BaseHTTPRequestHandler):
    db_manager: HistoryDBManager = None  # Attached on server startup

    def log_message(self, format_str, *args):
        # Keep console output clean
        pass

    def _send_json(self, status_code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status_code: int, html_text: str):
        body = html_text.encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw) if raw.strip() else {}

    def _handle_exception(self, exc: Exception):
        if isinstance(exc, DatabaseLockedError):
            self._send_json(
                409,
                {
                    "error": "database_locked",
                    "error_code": "DB_LOCKED",
                    "db_path": exc.db_path,
                    "message": str(exc),
                },
            )
        elif isinstance(exc, FileNotFoundError):
            self._send_json(404, {"error": "not_found", "message": str(exc)})
        elif isinstance(exc, ValueError):
            self._send_json(400, {"error": "bad_request", "message": str(exc)})
        else:
            self._send_json(500, {"error": "internal_error", "message": str(exc)})

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        try:
            if route == "/" or route == "/index.html":
                self._send_html(200, FRONTEND_HTML)
                return

            if route == "/api/os-paths":
                self._send_json(200, detect_os_chrome_paths())
                return

            if route == "/api/history":
                search = query.get("search", [""])[0]
                start_date = query.get("start_date", [""])[0]
                end_date = query.get("end_date", [""])[0]
                transition = query.get("transition", [""])[0]
                use_utc = query.get("use_utc", ["0"])[0] == "1"
                data = self.db_manager.query_history(
                    search=search,
                    start_date=start_date,
                    end_date=end_date,
                    transition=transition,
                    use_utc=use_utc,
                )
                self._send_json(200, data)
                return

            if route == "/api/fetch-title":
                target_url = query.get("url", [""])[0]
                title = fetch_title_from_url(target_url)
                self._send_json(200, {"url": target_url, "title": title})
                return

            if route == "/api/convert-timestamp":
                dt_str = query.get("datetime_local", [""])[0]
                wk_str = query.get("webkit", [""])[0]
                use_utc = query.get("use_utc", ["0"])[0] == "1"
                if dt_str:
                    wk = datetime_local_to_webkit(dt_str, use_utc=use_utc)
                    self._send_json(200, {"datetime_local": dt_str, "webkit_timestamp": wk})
                elif wk_str:
                    wk_int = int(wk_str)
                    self._send_json(
                        200,
                        {
                            "webkit_timestamp": wk_int,
                            "unix_seconds": webkit_to_unix(wk_int),
                            "datetime_local": webkit_to_datetime_local(wk_int, use_utc=use_utc),
                        },
                    )
                else:
                    raise ValueError("Provide either datetime_local or webkit query parameter.")
                return

            self._send_json(404, {"error": "not_found", "message": f"Unknown endpoint: {route}"})
        except Exception as exc:
            self._handle_exception(exc)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path

        try:
            body = self._read_json_body()

            if route == "/api/visits":
                res = self.db_manager.add_visit(
                    url=body.get("url", ""),
                    title=body.get("title", ""),
                    datetime_local=body.get("datetime_local"),
                    webkit_timestamp=body.get("webkit_timestamp"),
                    transition=body.get("transition", DEFAULT_TRANSITION),
                    duration_seconds=body.get("duration_seconds", 0),
                    use_utc=bool(body.get("use_utc", False)),
                    entry_type=body.get("entry_type", "auto"),
                )
                self._send_json(201, res)
                return

            if route == "/api/db/mode":
                res = self.db_manager.switch_mode(
                    mode=body.get("mode", "sandbox"),
                    custom_path=body.get("path", ""),
                    create_if_missing=bool(body.get("create_if_missing", False)),
                )
                self._send_json(200, res)
                return

            if route == "/api/db/clone":
                res = self.db_manager.clone_to_working_copy(source_path=body.get("path", ""))
                self._send_json(200, res)
                return

            if route == "/api/db/force-sync-chrome":
                res = self.db_manager.force_sync_chrome()
                self._send_json(200, res)
                return

            if route == "/api/db/reseed":
                self.db_manager.ensure_sandbox_initialized(force_reseed=True)
                self._send_json(200, {"status": "ok", "message": "Sandbox database re-seeded."})
                return

            if route == "/api/sql-log/clear":
                self.db_manager.sql_log.clear()
                self._send_json(200, {"status": "ok"})
                return

            self._send_json(404, {"error": "not_found", "message": f"Unknown endpoint: {route}"})
        except Exception as exc:
            self._handle_exception(exc)

    def do_PUT(self):
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path

        try:
            body = self._read_json_body()
            m_visit = re.match(r"^/api/visits/(\d+)$", route)
            if m_visit:
                visit_id = int(m_visit.group(1))
                res = self.db_manager.edit_visit(
                    visit_id=visit_id,
                    url=body.get("url"),
                    title=body.get("title"),
                    datetime_local=body.get("datetime_local"),
                    webkit_timestamp=body.get("webkit_timestamp"),
                    transition=body.get("transition"),
                    duration_seconds=body.get("duration_seconds"),
                    use_utc=bool(body.get("use_utc", False)),
                )
                self._send_json(200, res)
                return

            self._send_json(404, {"error": "not_found", "message": f"Unknown endpoint: {route}"})
        except Exception as exc:
            self._handle_exception(exc)

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path

        try:
            m_visit = re.match(r"^/api/visits/(\d+)$", route)
            if m_visit:
                visit_id = int(m_visit.group(1))
                res = self.db_manager.delete_visit(visit_id)
                self._send_json(200, res)
                return

            m_url = re.match(r"^/api/urls/(\d+)$", route)
            if m_url:
                url_id = int(m_url.group(1))
                res = self.db_manager.delete_url(url_id)
                self._send_json(200, res)
                return

            self._send_json(404, {"error": "not_found", "message": f"Unknown endpoint: {route}"})
        except Exception as exc:
            self._handle_exception(exc)


# ---------------------------------------------------------------------------
# Automated Self-Test Suite
# ---------------------------------------------------------------------------

def run_self_tests() -> bool:
    """
    Automated verification suite testing:
    1. Exact 17-digit WebKit timestamp conversion & round-trip for '2022-08-15 14:30:00'.
    2. Adding a link with custom past date '2022-08-15 14:30:00' writes the exact 17-digit
       WebKit timestamp to both `visits.visit_time` and `urls.last_visit_time`.
    3. Adding a second visit to the same URL increments `urls.visit_count` to 2 and sets
       `urls.last_visit_time` to the newer timestamp (and increments `typed_count` for Typed URL).
    4. Modifying/backdating or deleting a visit keeps `urls` and `visits` synchronized
       (and removes the parent `urls` row when `visit_count` reaches 0).
    5. Dynamic schema compatibility when extra modern Chromium columns with NOT NULL constraints exist.
    """
    import tempfile

    print("=" * 72)
    print("Running Chrome History SQLite Studio Automated Self-Test Suite...")
    print("=" * 72)

    temp_dir = tempfile.mkdtemp(prefix="chrome_hist_test_")
    test_db_path = os.path.join(temp_dir, "test_sandbox_History.db")

    try:
        mgr = HistoryDBManager(sandbox_path=test_db_path)
        # Clear initial seed so we test from a clean state
        conn = sqlite3.connect(test_db_path)
        conn.execute("DELETE FROM visits")
        conn.execute("DELETE FROM urls")
        # Add an extra modern Chromium column with NOT NULL constraint and no default
        # to verify PRAGMA table_info dynamic compatibility
        conn.execute("ALTER TABLE visits ADD COLUMN visited_link_id INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE urls ADD COLUMN extra_chromium_text TEXT NOT NULL DEFAULT ''")
        conn.commit()
        conn.close()

        # Test 1: Add a link with custom past date '2022-08-15 14:30:00'
        past_dt_str = "2022-08-15 14:30:00"
        expected_webkit_1 = datetime_local_to_webkit(past_dt_str)
        assert len(str(expected_webkit_1)) == 17, (
            f"Expected 17-digit WebKit timestamp, got {expected_webkit_1}"
        )
        # Verify round-trip formula
        rt_unix = webkit_to_unix(expected_webkit_1)
        assert unix_to_webkit(rt_unix) == expected_webkit_1, "WebKit <-> Unix round-trip mismatch"
        assert webkit_to_display_str(expected_webkit_1) == past_dt_str, (
            f"Expected '{past_dt_str}', got '{webkit_to_display_str(expected_webkit_1)}'"
        )

        test_url = "https://example.org/history-verification"
        res1 = mgr.add_visit(
            url=test_url,
            title="Initial Verification Title",
            datetime_local=past_dt_str,
            transition=805306368,  # Clicked Link
            duration_seconds=45,
        )
        url_id = res1["url_id"]
        visit_1_id = res1["visit_id"]

        conn = sqlite3.connect(test_db_path)
        conn.row_factory = sqlite3.Row
        v_row = conn.execute("SELECT * FROM visits WHERE id = ?", (visit_1_id,)).fetchone()
        u_row = conn.execute("SELECT * FROM urls WHERE id = ?", (url_id,)).fetchone()
        conn.close()

        assert v_row["visit_time"] == expected_webkit_1, (
            f"visits.visit_time {v_row['visit_time']} != {expected_webkit_1}"
        )
        assert u_row["last_visit_time"] == expected_webkit_1, (
            f"urls.last_visit_time {u_row['last_visit_time']} != {expected_webkit_1}"
        )
        assert u_row["visit_count"] == 1, f"Expected visit_count=1, got {u_row['visit_count']}"
        assert u_row["typed_count"] == 0, f"Expected typed_count=0, got {u_row['typed_count']}"
        print(
            f"[PASS] 1. Added visit at '{past_dt_str}' -> 17-digit WebKit timestamp "
            f"{expected_webkit_1} written to both visits.visit_time and urls.last_visit_time."
        )

        # Test 2: Add a second visit to the same URL with a newer timestamp & Typed URL transition
        newer_dt_str = "2023-05-20 09:15:30"
        expected_webkit_2 = datetime_local_to_webkit(newer_dt_str)
        assert expected_webkit_2 > expected_webkit_1

        res2 = mgr.add_visit(
            url=test_url,
            title="Updated Verification Title",
            datetime_local=newer_dt_str,
            transition=805306369,  # Typed URL
            duration_seconds=90,
        )
        visit_2_id = res2["visit_id"]
        assert res2["url_id"] == url_id, "Second visit should reference the same urls.id"

        conn = sqlite3.connect(test_db_path)
        conn.row_factory = sqlite3.Row
        u_row_2 = conn.execute("SELECT * FROM urls WHERE id = ?", (url_id,)).fetchone()
        conn.close()

        assert u_row_2["visit_count"] == 2, f"Expected visit_count=2, got {u_row_2['visit_count']}"
        assert u_row_2["typed_count"] == 1, f"Expected typed_count=1, got {u_row_2['typed_count']}"
        assert u_row_2["last_visit_time"] == expected_webkit_2, (
            f"Expected urls.last_visit_time={expected_webkit_2}, got {u_row_2['last_visit_time']}"
        )
        assert u_row_2["title"] == "Updated Verification Title"
        print(
            f"[PASS] 2. Added second visit at '{newer_dt_str}' -> urls.visit_count=2, "
            f"urls.typed_count=1, urls.last_visit_time={expected_webkit_2}."
        )

        # Test 3: Modify / backdate visit_2 to an earlier date than visit_1 ('2021-01-10 08:00:00')
        # Verify urls.last_visit_time automatically syncs to visit_1's timestamp!
        earlier_dt_str = "2021-01-10 08:00:00"
        expected_webkit_earlier = datetime_local_to_webkit(earlier_dt_str)
        mgr.edit_visit(
            visit_id=visit_2_id,
            datetime_local=earlier_dt_str,
            title="Backdated Visit Title",
        )

        conn = sqlite3.connect(test_db_path)
        conn.row_factory = sqlite3.Row
        u_row_3 = conn.execute("SELECT * FROM urls WHERE id = ?", (url_id,)).fetchone()
        v2_row = conn.execute("SELECT * FROM visits WHERE id = ?", (visit_2_id,)).fetchone()
        conn.close()

        assert v2_row["visit_time"] == expected_webkit_earlier
        assert u_row_3["visit_count"] == 2
        assert u_row_3["last_visit_time"] == expected_webkit_1, (
            "After backdating visit 2 before visit 1, urls.last_visit_time must equal visit 1's time"
        )
        print(
            f"[PASS] 3. Backdated visit #2 to '{earlier_dt_str}' -> urls.last_visit_time "
            f"automatically recalculated to MAX(visit_time) = {expected_webkit_1}."
        )

        # Test 4: Delete visit_1 -> verify urls.visit_count becomes 1 and urls.last_visit_time = visit_2's time
        del1 = mgr.delete_visit(visit_1_id)
        assert not del1["url_deleted"]
        assert del1["url_record"]["visit_count"] == 1
        assert del1["url_record"]["last_visit_time"] == expected_webkit_earlier

        # Delete visit_2 -> verify parent urls row is automatically deleted when visit_count reaches 0
        del2 = mgr.delete_visit(visit_2_id)
        assert del2["url_deleted"] is True

        conn = sqlite3.connect(test_db_path)
        rem_urls = conn.execute("SELECT COUNT(*) FROM urls WHERE id = ?", (url_id,)).fetchone()[0]
        conn.close()
        assert rem_urls == 0, "Parent URL record should be removed when visit_count reaches 0"
        print(
            "[PASS] 4. Deleted visits -> urls.visit_count and urls.last_visit_time stayed "
            "synchronized, and parent URL row was removed when visit_count reached 0."
        )

        print("=" * 72)
        print("ALL SELF-TESTS PASSED SUCCESSFULLY!")
        print("=" * 72)
        return True
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

def _open_ui_in_browser(url: str):
    """Open the web UI in Microsoft Edge on Windows (so Chrome isn't locked), or default browser."""
    import subprocess
    import webbrowser
    try:
        if platform.system().lower() == "windows":
            res = subprocess.run(["cmd", "/c", "start", "msedge", url], capture_output=True)
            if res.returncode == 0:
                return
        webbrowser.open(url)
    except Exception:
        pass


def run_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    initial_custom_path: str = "",
    auto_connect: bool = False,
    open_browser: bool = False,
):
    db_manager = HistoryDBManager(sandbox_path=DEFAULT_SANDBOX_DB)
    if not initial_custom_path and auto_connect:
        detected = detect_os_chrome_paths().get("detected_paths", [])
        for item in detected:
            if item.get("exists"):
                initial_custom_path = item["path"]
                break

    if initial_custom_path:
        try:
            db_manager.switch_mode("custom", initial_custom_path, auto_working_copy=True)
        except Exception as exc:
            print(f"Warning: Could not pre-connect custom path ({exc}); starting in Sandbox mode.")
    HistoryRequestHandler.db_manager = db_manager
    server = ThreadingHTTPServer((host, port), HistoryRequestHandler)
    url = f"http://{host}:{port}"
    print("=" * 72)
    print("Chrome History SQLite Studio is running!")
    print(f"Local Web UI URL : {url}")
    print(f"Active Mode      : {db_manager.mode}")
    print(f"Active Database  : {db_manager.active_db_path}")
    if db_manager.target_chrome_path:
        print(f"Target Chrome DB : {db_manager.target_chrome_path}")
    print("=" * 72)
    if open_browser:
        threading.Timer(0.6, _open_ui_in_browser, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
        server.server_close()


if __name__ == "__main__":
    is_frozen_exe = getattr(sys, "frozen", False)
    parser = argparse.ArgumentParser(description="Chrome History SQLite Studio")
    parser.add_argument("--host", default="127.0.0.1", help="Host interface to bind (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="Port to listen on (default: 8765)")
    parser.add_argument("--custom-path", default="", help="Optional Chrome History path to connect on startup")
    parser.add_argument(
        "--auto-connect",
        action="store_true",
        default=is_frozen_exe,
        help="Automatically connect to this computer's Default Chrome profile on startup",
    )
    parser.add_argument(
        "--open-browser",
        action="store_true",
        default=is_frozen_exe,
        help="Automatically open the UI in Microsoft Edge / browser on startup",
    )
    parser.add_argument("--test", action="store_true", help="Run the automated self-test suite and exit")
    args = parser.parse_args()

    if args.test:
        ok = run_self_tests()
        sys.exit(0 if ok else 1)
    else:
        run_server(
            host=args.host,
            port=args.port,
            initial_custom_path=args.custom_path,
            auto_connect=args.auto_connect,
            open_browser=args.open_browser,
        )
