#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import html
import hmac
import hashlib
import mimetypes
import os
import re
import secrets
import sqlite3
from datetime import date, datetime, timedelta, timezone
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse, unquote

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "bayoubombers.db"
SESSION_COOKIE = "bayou_session"
SESSIONS: dict[str, dict[str, str]] = {}
SESSION_TTL_SECONDS = 60 * 60 * 12
FORCE_SECURE_COOKIE = os.getenv("BAYOU_COOKIE_SECURE", "false").lower() == "true"
STATIC_DIR = BASE_DIR / "static"
UPLOADS_DIR = STATIC_DIR / "uploads"
TEMPLATES_DIR = BASE_DIR / "templates"
HANDLE_SUFFIX_MIN = 100
HANDLE_SUFFIX_SPAN = 900
ALLOWED_UPLOAD_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"}


def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_email(value: str) -> str:
    return value.strip().lower()


def slugify_text(value: str) -> str:
    normalized_slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return normalized_slug or "member"


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return f"pbkdf2_sha256${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(candidate: str, stored: str) -> bool:
    if stored.startswith("pbkdf2_sha256$"):
        try:
            _, salt_b64, digest_b64 = stored.split("$", 2)
            salt = base64.b64decode(salt_b64)
            expected = base64.b64decode(digest_b64)
        except (ValueError, TypeError):
            return False
        actual = hashlib.pbkdf2_hmac("sha256", candidate.encode("utf-8"), salt, 200_000)
        return hmac.compare_digest(actual, expected)
    return hmac.compare_digest(candidate, stored)


def feet_inches_to_metrics(feet: int, inches: int) -> tuple[int, float]:
    total_inches = max((feet * 12) + inches, 0)
    meters = round(total_inches * 0.0254, 2)
    return total_inches, meters


def recalc_throw_pr(conn: sqlite3.Connection, athlete_id: int, event: str) -> None:
    best = conn.execute(
        "SELECT MAX(total_inches) AS best FROM throw_logs WHERE athlete_id=? AND event=?",
        (athlete_id, event),
    ).fetchone()["best"]
    if best is None:
        return
    feet = best // 12
    inches = best % 12
    value_text = f"{feet}' {inches}\""
    existing = conn.execute(
        "SELECT id,value_numeric FROM prs WHERE athlete_id=? AND category='throw' AND event_or_lift=?",
        (athlete_id, event),
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO prs (athlete_id, category, event_or_lift, value_text, value_numeric, achieved_on) VALUES (?,?,?,?,?,?)",
            (athlete_id, "throw", event, value_text, float(best), date.today().isoformat()),
        )
    elif float(best) > float(existing["value_numeric"]):
        conn.execute(
            "UPDATE prs SET value_text=?, value_numeric=?, achieved_on=? WHERE id=?",
            (value_text, float(best), date.today().isoformat(), existing["id"]),
        )


def recalc_lift_pr(conn: sqlite3.Connection, athlete_id: int, lift_name: str) -> None:
    best = conn.execute(
        "SELECT MAX(projected_max) AS best FROM lift_logs WHERE athlete_id=? AND lift_name=?",
        (athlete_id, lift_name),
    ).fetchone()["best"]
    if best is None:
        return
    value_text = f"{round(float(best), 1)} lbs"
    existing = conn.execute(
        "SELECT id,value_numeric FROM prs WHERE athlete_id=? AND category='lift' AND event_or_lift=?",
        (athlete_id, lift_name),
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO prs (athlete_id, category, event_or_lift, value_text, value_numeric, achieved_on) VALUES (?,?,?,?,?,?)",
            (athlete_id, "lift", lift_name, value_text, float(best), date.today().isoformat()),
        )
    elif float(best) > float(existing["value_numeric"]):
        conn.execute(
            "UPDATE prs SET value_text=?, value_numeric=?, achieved_on=? WHERE id=?",
            (value_text, float(best), date.today().isoformat(), existing["id"]),
        )


def init_db() -> None:
    with db_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('coach','athlete')),
                name TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS athletes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER UNIQUE,
                sex TEXT NOT NULL,
                events TEXT NOT NULL,
                group_name TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS training_modules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                description TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS practice_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                practice_date TEXT NOT NULL,
                notes TEXT,
                created_by INTEGER NOT NULL,
                FOREIGN KEY(created_by) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS practice_plan_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id INTEGER NOT NULL,
                module_id INTEGER NOT NULL,
                reps TEXT,
                notes TEXT,
                FOREIGN KEY(plan_id) REFERENCES practice_plans(id),
                FOREIGN KEY(module_id) REFERENCES training_modules(id)
            );

            CREATE TABLE IF NOT EXISTS assignments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id INTEGER NOT NULL,
                athlete_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'assigned',
                assigned_on TEXT NOT NULL,
                completed_on TEXT,
                FOREIGN KEY(plan_id) REFERENCES practice_plans(id),
                FOREIGN KEY(athlete_id) REFERENCES athletes(id)
            );

            CREATE TABLE IF NOT EXISTS throw_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                athlete_id INTEGER NOT NULL,
                event TEXT NOT NULL,
                feet INTEGER NOT NULL,
                inches INTEGER NOT NULL,
                total_inches INTEGER NOT NULL,
                meters REAL NOT NULL,
                practice_date TEXT NOT NULL,
                FOREIGN KEY(athlete_id) REFERENCES athletes(id)
            );

            CREATE TABLE IF NOT EXISTS lift_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                athlete_id INTEGER NOT NULL,
                lift_name TEXT NOT NULL,
                weight REAL NOT NULL,
                reps INTEGER NOT NULL,
                projected_max REAL NOT NULL,
                practice_date TEXT NOT NULL,
                FOREIGN KEY(athlete_id) REFERENCES athletes(id)
            );

            CREATE TABLE IF NOT EXISTS meet_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                athlete_id INTEGER NOT NULL,
                event TEXT NOT NULL,
                feet INTEGER NOT NULL,
                inches INTEGER NOT NULL,
                total_inches INTEGER NOT NULL,
                meters REAL NOT NULL,
                meet_name TEXT NOT NULL,
                meet_date TEXT NOT NULL,
                FOREIGN KEY(athlete_id) REFERENCES athletes(id)
            );

            CREATE TABLE IF NOT EXISTS prs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                athlete_id INTEGER NOT NULL,
                category TEXT NOT NULL,
                event_or_lift TEXT NOT NULL,
                value_text TEXT NOT NULL,
                value_numeric REAL NOT NULL,
                achieved_on TEXT NOT NULL,
                FOREIGN KEY(athlete_id) REFERENCES athletes(id)
            );

            CREATE TABLE IF NOT EXISTS schools (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                coach_user_id INTEGER NOT NULL,
                name TEXT UNIQUE NOT NULL,
                color_primary TEXT NOT NULL DEFAULT '#2563eb',
                color_secondary TEXT NOT NULL DEFAULT '#0f172a',
                symbol_url TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(coach_user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS galleries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                image_url TEXT NOT NULL,
                caption TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS status_updates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                body TEXT NOT NULL,
                image_url TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS site_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS coach_invites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                coach_user_id INTEGER NOT NULL,
                athlete_user_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                FOREIGN KEY(coach_user_id) REFERENCES users(id),
                FOREIGN KEY(athlete_user_id) REFERENCES users(id),
                UNIQUE(coach_user_id, athlete_user_id)
            );
            """
        )

        UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

        ensure_column(conn, "users", "is_admin", "INTEGER", "0")
        ensure_column(conn, "users", "locked", "INTEGER", "0")
        ensure_column(conn, "users", "profile_private", "INTEGER", "0")
        ensure_column(conn, "users", "force_private", "INTEGER", "0")
        ensure_column(conn, "users", "school_id", "INTEGER")
        ensure_column(conn, "users", "bio", "TEXT", "''")
        ensure_column(conn, "users", "status_text", "TEXT", "''")
        ensure_column(conn, "users", "grade_level", "TEXT", "''")
        ensure_column(conn, "users", "age", "INTEGER")
        ensure_column(conn, "users", "height", "TEXT", "''")
        ensure_column(conn, "users", "weight", "TEXT", "''")
        ensure_column(conn, "users", "hometown", "TEXT", "''")
        ensure_column(conn, "users", "state", "TEXT", "''")
        ensure_column(conn, "users", "profile_image_url", "TEXT", "''")
        ensure_column(conn, "users", "email", "TEXT", "''")
        ensure_column(conn, "athletes", "coach_user_id", "INTEGER")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_status_updates_user_created ON status_updates(user_id, created_at)")

        users = conn.execute("SELECT id, username, name, email FROM users ORDER BY id").fetchall()
        for row in users:
            existing_email = normalize_email(row["email"] or "")
            if existing_email:
                continue
            username_value = row["username"] or ""
            if "@" in username_value:
                seed = username_value
            else:
                seed_source = username_value or row["name"] or "member"
                seed = f"{seed_source}@bayoubombers.app"
            conn.execute("UPDATE users SET email=? WHERE id=?", (next_available_email(conn, seed, row["id"]), row["id"]))
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_unique ON users(email)")

        set_setting(
            conn,
            "about_text",
            get_setting(conn, "about_text", "Bayou Bombers is a modern throws training platform for athletes and coaches."),
        )
        set_setting(conn, "home_cover_url", get_setting(conn, "home_cover_url", ""))
        set_setting(conn, "registration_open", get_setting(conn, "registration_open", "1"))
        set_setting(conn, "hide_minor_images_public", get_setting(conn, "hide_minor_images_public", "1"))

        coach = conn.execute("SELECT id FROM users WHERE username='coach'").fetchone()
        if coach is None:
            conn.execute(
                "INSERT INTO users (username, email, password, role, name) VALUES (?,?,?,?,?)",
                ("coach", next_available_email(conn, "coach@bayoubombers.app"), hash_password("coach123"), "coach", "Head Coach"),
            )
            coach = conn.execute("SELECT id FROM users WHERE username='coach'").fetchone()
        admin = conn.execute("SELECT id FROM users WHERE username='admin@admin.com'").fetchone()
        if admin is None:
            conn.execute(
                "INSERT INTO users (username, email, password, role, name) VALUES (?,?,?,?,?)",
                (
                    "admin@admin.com",
                    next_available_email(conn, "admin@admin.com"),
                    hash_password("password123"),
                    "coach",
                    "Admin Coach",
                ),
            )
            admin = conn.execute("SELECT id FROM users WHERE username='admin@admin.com'").fetchone()
        if admin is None:
            raise RuntimeError("Failed to initialize admin account")
        conn.execute("UPDATE users SET is_admin=1 WHERE id=?", (admin["id"],))

        athlete_user = conn.execute("SELECT id FROM users WHERE username='athlete' ").fetchone()
        if athlete_user is None:
            conn.execute(
                "INSERT INTO users (username, email, password, role, name) VALUES (?,?,?,?,?)",
                (
                    "athlete",
                    next_available_email(conn, "athlete@bayoubombers.app"),
                    hash_password("athlete123"),
                    "athlete",
                    "Sample Athlete",
                ),
            )
            athlete_user = conn.execute("SELECT id FROM users WHERE username='athlete'").fetchone()

        existing_athlete = conn.execute("SELECT id FROM athletes WHERE user_id=?", (athlete_user["id"],)).fetchone()
        if existing_athlete is None:
            conn.execute(
                "INSERT INTO athletes (user_id, sex, events, group_name, coach_user_id) VALUES (?,?,?,?,?)",
                (athlete_user["id"], "Male", "Shot Put,Discus", "Varsity Throws", coach["id"]),
            )

        module_count = conn.execute("SELECT COUNT(*) AS c FROM training_modules").fetchone()["c"]
        if module_count == 0:
            conn.executemany(
                "INSERT INTO training_modules (name, category, description) VALUES (?,?,?)",
                [
                    ("South African Drill", "Technique", "3x8 reps each side"),
                    ("Power Position Throws", "Throws", "6 quality reps per event"),
                    ("Javelin Crossovers", "Technique", "4 sets of 20m"),
                    ("Burnout Set", "Weight Room", "AMRAP at 70% after top set"),
                ],
            )


def esc(value: object) -> str:
    return html.escape(str(value))


def read_template(name: str) -> str:
    path = TEMPLATES_DIR / name
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    allowed_tables = {
        "users",
        "athletes",
        "training_modules",
        "practice_plans",
        "practice_plan_items",
        "assignments",
        "throw_logs",
        "lift_logs",
        "meet_results",
        "prs",
        "schools",
        "galleries",
        "site_settings",
        "coach_invites",
    }
    if table not in allowed_tables:
        raise ValueError("Unsupported table name")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", column):
        raise ValueError("Invalid column name")
    # Safe interpolation: table is strictly allowlisted above and cannot be user-controlled.
    cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(c["name"] == column for c in cols)


def ensure_column(conn: sqlite3.Connection, table: str, column: str, sql_type: str, default_sql: str = "") -> None:
    if table not in {"users", "athletes"}:
        raise ValueError("Unsupported table for schema alteration")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", column):
        raise ValueError("Invalid column name")
    if sql_type not in {"INTEGER", "TEXT", "REAL"}:
        raise ValueError("Unsupported SQL type")
    if not column_exists(conn, table, column):
        default_part = f" DEFAULT {default_sql}" if default_sql else ""
        # Safe interpolation: table/column/sql_type are validated to strict allowlists/patterns.
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}{default_part}")


def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM site_settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO site_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def setting_bool(conn: sqlite3.Connection, key: str, default: bool = False) -> bool:
    return get_setting(conn, key, "1" if default else "0") == "1"


def next_available_email(conn: sqlite3.Connection, seed: str, exclude_user_id: int | None = None) -> str:
    seed = normalize_email(seed)
    local_part, _, domain_part = seed.partition("@")
    local = slugify_text(local_part or "member")
    domain = re.sub(r"[^a-z0-9.-]+", "", domain_part.lower()).strip(".") or "bayoubombers.app"
    for suffix in range(0, 1000):
        candidate = f"{local}@{domain}" if suffix == 0 else f"{local}-{suffix}@{domain}"
        params: tuple[object, ...]
        query = "SELECT id FROM users WHERE email=?"
        if exclude_user_id is None:
            params = (candidate,)
        else:
            query += " AND id<>?"
            params = (candidate, exclude_user_id)
        if conn.execute(query, params).fetchone() is None:
            return candidate
    raise RuntimeError("Unable to generate a unique email address")


def is_valid_email(value: str) -> bool:
    email = normalize_email(value)
    if not email or " " in email or email.count("@") != 1:
        return False
    local_part, domain_part = email.split("@", 1)
    if not local_part or not domain_part or "." not in domain_part:
        return False
    if domain_part.startswith(".") or domain_part.endswith(".") or ".." in domain_part:
        return False
    return True


def unique_handle(conn: sqlite3.Connection, handle: str, exclude_user_id: int | None = None) -> str:
    cleaned = handle.strip()
    if not cleaned:
        return ""
    candidate = cleaned
    for _ in range(1000):
        existing = conn.execute(
            "SELECT id FROM users WHERE username=?" + (" AND id<>?" if exclude_user_id is not None else ""),
            (candidate, exclude_user_id) if exclude_user_id is not None else (candidate,),
        ).fetchone()
        if existing is None:
            return candidate
        candidate = f"{cleaned}-{secrets.randbelow(HANDLE_SUFFIX_SPAN) + HANDLE_SUFFIX_MIN}"
    raise RuntimeError("Unable to generate a unique handle")


def default_handle_from_email(email: str) -> str:
    local_part = email.split("@", 1)[0]
    return slugify_text(local_part)


def optional_http_url(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        return ""
    parsed = urlparse(cleaned)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return cleaned
    return ""


def optional_image_url(value: str) -> str:
    cleaned = optional_http_url(value)
    if not cleaned:
        return ""
    parsed = urlparse(cleaned)
    image_type = mimetypes.guess_type(parsed.path)[0] or ""
    if image_type.startswith("image/"):
        return cleaned
    return ""


def parse_multipart(content_type: str, body: bytes) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
    """Parse multipart/form-data body. Returns (fields, files) where files are (filename, bytes)."""
    boundary_m = re.search(r"boundary=([^\s;,]+)", content_type)
    if not boundary_m:
        return {}, {}
    boundary = boundary_m.group(1).strip('"').encode("latin-1")
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes]] = {}
    delimiter = b"--" + boundary
    parts = body.split(delimiter)
    for part in parts[1:]:
        if part[:2] == b"--":
            break
        if part[:2] == b"\r\n":
            part = part[2:]
        if part[-2:] == b"\r\n":
            part = part[:-2]
        if b"\r\n\r\n" not in part:
            continue
        raw_headers, _, content = part.partition(b"\r\n\r\n")
        header_map: dict[str, str] = {}
        for line in raw_headers.decode("utf-8", "replace").split("\r\n"):
            if ":" in line:
                k, _, v = line.partition(":")
                header_map[k.strip().lower()] = v.strip()
        disp = header_map.get("content-disposition", "")
        name_m = re.search(r'name="([^"]*)"', disp)
        fname_m = re.search(r'filename="([^"]*)"', disp)
        if not name_m:
            continue
        field_name = name_m.group(1)
        if fname_m and fname_m.group(1):
            files[field_name] = (fname_m.group(1), content)
        else:
            fields[field_name] = content.decode("utf-8", "replace")
    return fields, files


def save_upload(filename: str, data: bytes) -> str:
    """Save an uploaded image file to UPLOADS_DIR. Returns the URL path or ''."""
    if not data or not filename:
        return ""
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_UPLOAD_EXTS:
        return ""
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    unique_name = f"{secrets.token_hex(16)}{ext}"
    (UPLOADS_DIR / unique_name).write_bytes(data)
    return f"/static/uploads/{unique_name}"


def format_text_block(value: str, empty_message: str) -> str:
    text = value.strip()
    if not text:
        return f"<p class='muted'>{esc(empty_message)}</p>"
    return "<p>" + esc(text).replace("\n", "<br>") + "</p>"


def dashboard_path(user: sqlite3.Row) -> str:
    if user["is_admin"]:
        return "/admin"
    return "/coach" if user["role"] == "coach" else "/athlete"


def render_status_feed(updates: list[sqlite3.Row], empty_message: str = "No status updates yet.") -> str:
    if not updates:
        return f"<div class='card'><p class='muted'>{esc(empty_message)}</p></div>"
    cards = []
    for update in updates:
        image_html = f"<img class='status-image' src='{esc(update['image_url'])}' alt='status image'>" if update["image_url"] else ""
        cards.append(
            "<article class='card status-card'>"
            f"<div class='status-meta'>{esc(update['created_at'])}</div>"
            f"{format_text_block(update['body'], empty_message)}"
            f"{image_html}"
            "</article>"
        )
    return "".join(cards)


def can_display_public_images(
    target_user: sqlite3.Row,
    current_user: sqlite3.Row | None,
    hide_minor_images: bool,
    is_owner: bool,
) -> bool:
    if not hide_minor_images:
        return True
    if target_user["role"] != "athlete":
        return True
    if (target_user["age"] or 0) >= 18:
        return True
    if is_owner:
        return True
    if current_user and current_user["is_admin"]:
        return True
    return False


def html_page(title: str, body: str, user: sqlite3.Row | None = None, profile_colors: tuple[str, str] | None = None) -> str:
    primary = "#2563eb"
    secondary = "#0f172a"
    if profile_colors:
        primary, secondary = profile_colors
    elif user and user["school_id"]:
        with db_conn() as conn:
            school = conn.execute("SELECT color_primary,color_secondary FROM schools WHERE id=?", (user["school_id"],)).fetchone()
            if school:
                primary = school["color_primary"] or primary
                secondary = school["color_secondary"] or secondary
    nav_links = ["<a href='/'>Home</a>"]
    if user:
        if user["is_admin"]:
            nav_links.extend(
                [
                    "<a href='/admin'>Admin</a>",
                    "<a href='/search'>Search</a>",
                    f"<a href='/profile/{user['id']}'>Profile</a>",
                    "<a href='/profile'>Settings</a>",
                    "<a href='/logout'>Logout</a>",
                ]
            )
        elif user["role"] == "coach":
            nav_links.extend(
                [
                    "<a href='/coach'>Dashboard</a>",
                    "<a href='/coach/athletes'>Athletes</a>",
                    "<a href='/coach/modules'>Modules</a>",
                    "<a href='/coach/plans'>Plans</a>",
                    "<a href='/coach/reports'>Reports</a>",
                    "<a href='/coach/schools'>Schools</a>",
                    "<a href='/search'>Search</a>",
                    f"<a href='/profile/{user['id']}'>Profile</a>",
                    "<a href='/logout'>Logout</a>",
                ]
            )
        else:
            nav_links.extend(
                [
                    "<a href='/athlete'>Today</a>",
                    "<a href='/athlete/progress'>Progress</a>",
                    "<a href='/search'>Search</a>",
                    f"<a href='/profile/{user['id']}'>Profile</a>",
                    "<a href='/logout'>Logout</a>",
                ]
            )
    else:
        nav_links.extend(["<a href='/search'>Search</a>", "<a href='/login'>Login / Register</a>"])
    nav = "".join(nav_links)
    return f"""<!doctype html>
<html>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <title>{esc(title)}</title>
  <style>:root{{--school-primary:{esc(primary)};--school-secondary:{esc(secondary)};}}</style>
  <link rel='stylesheet' href='/static/master.css'>
  <script src='/static/app.js' defer></script>
</head>
<body>
  <header class='site-header'>
    <div class='site-header-inner'>
      <a class='site-brand' href='/'>
        <span class='site-brand-mark'>BB</span>
        <span><strong>Bayou Bombers</strong><small>Throws training hub</small></span>
      </a>
      <button class='nav-toggle' aria-label='Toggle navigation' aria-expanded='false'>&#9776;</button>
      <nav class='site-nav' id='site-nav'>{nav}</nav>
    </div>
  </header>
  <main class='page-shell'>{body}</main>
</body>
</html>"""


def to_int(value: str, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def to_float(value: str, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def clamp(value: int, minimum: int, maximum: int) -> int:
    return max(min(value, maximum), minimum)


class AppHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.route("GET")

    def do_POST(self) -> None:
        self.route("POST")

    def route(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        user = self.current_user()

        if path.startswith("/static/") and method == "GET":
            return self.serve_static(path)

        if path == "/":
            return self.respond_html(home_page(user))

        if path == "/login" and method == "GET":
            return self.respond_html(login_page())

        if path == "/login" and method == "POST":
            form = self.read_form()
            return self.handle_login(form)

        if path == "/register" and method == "GET":
            return self.respond_html(self.register_page())

        if path == "/register" and method == "POST":
            return self.handle_register()

        if path == "/search" and method == "GET":
            return self.respond_html(self.search_profiles(query.get("q", [""])[0], user))

        if path.startswith("/profile/") and method == "GET":
            return self.respond_html(self.public_profile(path, user))

        if path == "/logout":
            return self.handle_logout()

        if user is None:
            return self.respond_html(login_page("Please log in first."), status=401)

        if user["locked"]:
            return self.respond_html(login_page("Your account is locked. Contact admin."), status=403)

        if path.startswith("/coach") and user["role"] != "coach":
            return self.respond_html(html_page("Unauthorized", "<div class='card'>Coach access only.</div>", user), status=403)

        if path.startswith("/athlete") and user["role"] != "athlete":
            return self.respond_html(html_page("Unauthorized", "<div class='card'>Athlete access only.</div>", user), status=403)

        if user["is_admin"] and path == "/admin" and method == "GET":
            return self.respond_html(self.admin_page(user))

        if user["is_admin"] and path == "/admin/settings" and method == "POST":
            return self.update_admin_settings(user)

        if user["is_admin"] and path == "/admin/account-action" and method == "POST":
            return self.admin_account_action(user)

        if path == "/profile" and method == "GET":
            return self.respond_html(self.my_profile(user))

        if path == "/profile" and method == "POST":
            return self.update_profile(user)

        if path == "/profile/status" and method == "POST":
            return self.add_status_update(user)

        if path == "/profile/gallery" and method == "POST":
            return self.add_gallery_photo(user)

        if path == "/coach/schools" and method == "GET":
            return self.respond_html(self.coach_schools(user))

        if path == "/coach/schools" and method == "POST":
            return self.add_school(user)

        if path == "/coach/schools/update" and method == "POST":
            return self.update_school(user)

        if path == "/coach/athlete/privacy" and method == "POST":
            return self.coach_athlete_privacy(user)

        if path == "/coach/athlete/school" and method == "POST":
            return self.coach_athlete_school(user)

        if path == "/coach":
            return self.respond_html(self.coach_dashboard(user))

        if path == "/coach/athletes" and method == "GET":
            return self.respond_html(self.coach_athletes(user))

        if path == "/coach/athletes" and method == "POST":
            return self.add_athlete(user)

        if path == "/coach/modules" and method == "GET":
            return self.respond_html(self.coach_modules(user))

        if path == "/coach/modules" and method == "POST":
            return self.add_module(user)

        if path == "/coach/plans" and method == "GET":
            return self.respond_html(self.coach_plans(user))

        if path == "/coach/plans" and method == "POST":
            return self.add_plan(user)

        if path == "/coach/athletes/search" and method == "GET":
            return self.respond_html(self.coach_athlete_search(user, query.get("q", [""])[0]))

        if path == "/coach/invite" and method == "POST":
            return self.coach_send_invite(user)

        if path == "/coach/assign" and method == "POST":
            return self.assign_plan(user)

        if path == "/coach/reports":
            return self.respond_html(self.coach_reports(user))

        if path == "/athlete":
            return self.respond_html(self.athlete_dashboard(user))

        if path == "/athlete/progress":
            return self.respond_html(self.athlete_progress(user))

        if path == "/athlete/invite/respond" and method == "POST":
            return self.respond_invite(user)

        if path.startswith("/athlete/assignment/") and path.endswith("/complete") and method == "POST":
            return self.complete_assignment(user, path)

        if path == "/athlete/throws" and method == "POST":
            return self.log_throw(user)

        if path == "/athlete/lifts" and method == "POST":
            return self.log_lift(user)

        if path == "/athlete/meets" and method == "POST":
            return self.log_meet(user)

        return self.respond_html(html_page("Not Found", "<div class='card'>Route not found.</div>", user), status=404)

    def current_user(self) -> sqlite3.Row | None:
        self.cleanup_sessions()
        cookie_header = self.headers.get("Cookie", "")
        jar = cookies.SimpleCookie()
        jar.load(cookie_header)
        token = jar.get(SESSION_COOKIE)
        if token is None:
            return None
        session = SESSIONS.get(token.value)
        if not session:
            return None
        created_ts = float(session.get("created_ts", "0"))
        if utc_now().timestamp() - created_ts > SESSION_TTL_SECONDS:
            SESSIONS.pop(token.value, None)
            return None
        with db_conn() as conn:
            return conn.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()

    def cleanup_sessions(self) -> None:
        now_ts = utc_now().timestamp()
        expired = [
            token
            for token, data in SESSIONS.items()
            if now_ts - float(data.get("created_ts", "0")) > SESSION_TTL_SECONDS
        ]
        for token in expired:
            SESSIONS.pop(token, None)

    def read_form_with_files(self) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
        content_type = self.headers.get("Content-Type", "")
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        if "multipart/form-data" in content_type:
            return parse_multipart(content_type, raw)
        fields = {k: v[0] for k, v in parse_qs(raw.decode("utf-8")).items()}
        return fields, {}

    def read_form(self) -> dict[str, str]:
        fields, _ = self.read_form_with_files()
        return fields

    def respond_html(self, content: str, status: int = 200, extra_headers: dict[str, str] | None = None) -> None:
        payload = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def redirect(self, location: str, cookie_header: str | None = None) -> None:
        headers = {"Location": location}
        if cookie_header:
            headers["Set-Cookie"] = cookie_header
        self.respond_html("", status=302, extra_headers=headers)

    def respond_bytes(self, payload: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def serve_static(self, path: str) -> None:
        rel = path.removeprefix("/static/")
        # Reject any path containing traversal sequences before resolving
        if ".." in rel or rel.startswith("/"):
            return self.respond_html("Not Found", status=404)
        target = (STATIC_DIR / rel).resolve()
        static_root = STATIC_DIR.resolve()
        if not str(target).startswith(str(static_root) + "/") and target != static_root:
            return self.respond_html("Not Found", status=404)
        if not target.exists() or not target.is_file():
            return self.respond_html("Not Found", status=404)
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.respond_bytes(target.read_bytes(), mime)

    def session_cookie_flags(self) -> str:
        secure = "; Secure" if FORCE_SECURE_COOKIE else ""
        return f"HttpOnly; Path=/; SameSite=Lax{secure}"

    def handle_login(self, form: dict[str, str]) -> None:
        email = normalize_email(form.get("email", ""))
        password = form.get("password", "")
        with db_conn() as conn:
            user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if user is None or not verify_password(password, user["password"]):
            return self.respond_html(login_page("Invalid email or password."), status=401)
        if user["locked"]:
            return self.respond_html(login_page("Your account is locked."), status=403)
        token = secrets.token_urlsafe(24)
        SESSIONS[token] = {"user_id": str(user["id"]), "created_ts": str(utc_now().timestamp())}
        cookie = f"{SESSION_COOKIE}={token}; {self.session_cookie_flags()}"
        self.redirect(dashboard_path(user), cookie)

    def register_page(self, error_message: str = "") -> str:
        with db_conn() as conn:
            if not setting_bool(conn, "registration_open", True):
                return html_page("Registration Closed", "<div class='card'>New account creation is currently disabled.</div>")
        error_html = f"<div class='notice card'>{esc(error_message)}</div>" if error_message else ""
        body = f"""
        {error_html}
        <div class='card'>
          <h2>Create Account</h2>
          <form method='post' action='/register'>
            <label>Name<input name='name' required></label>
            <div class='row'>
              <label>Email<input type='email' name='email' required></label>
              <label>Handle<input name='username' placeholder='Optional public handle'></label>
            </div>
            <label>Password<input type='password' name='password' required></label>
            <div class='row'>
              <label>Role<select name='role'><option value='athlete'>Athlete</option><option value='coach'>Coach</option></select></label>
              <label>Age<input type='number' min='1' max='99' name='age'></label>
            </div>
            <p class='muted'>Athletes are assigned to schools by coaches after account creation.</p>
            <button type='submit'>Create Account</button>
          </form>
          <p class='muted' style='margin-top:10px'>Already have an account? <a href='/login'>Sign in</a>.</p>
        </div>
        """
        return html_page("Register", body)

    def handle_register(self) -> None:
        form = self.read_form()
        with db_conn() as conn:
            if not setting_bool(conn, "registration_open", True):
                return self.respond_html(html_page("Registration Closed", "<div class='card'>Registration is disabled.</div>"), status=403)
            email = normalize_email(form.get("email", ""))
            username = form.get("username", "").strip() or default_handle_from_email(email)
            role = form.get("role", "athlete")
            if role not in {"coach", "athlete"}:
                role = "athlete"
            if not is_valid_email(email):
                return self.respond_html(self.register_page("Enter a valid email address."), status=400)
            exists = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
            if exists:
                return self.respond_html(self.register_page("An account with that email already exists."), status=400)
            username = unique_handle(conn, username)
            try:
                conn.execute(
                    "INSERT INTO users (username,email,password,role,name,age,school_id) VALUES (?,?,?,?,?,?,?)",
                    (
                        username,
                        email,
                        hash_password(form.get("password", "")),
                        role,
                        form.get("name", "User").strip() or "User",
                        to_int(form.get("age", "0"), 0) or None,
                        None,
                    ),
                )
            except sqlite3.IntegrityError:
                return self.respond_html(self.register_page("That email or handle is already in use."), status=400)
            if role == "athlete":
                created = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
                conn.execute(
                    "INSERT INTO athletes (user_id,sex,events,group_name) VALUES (?,?,?,?)",
                    (created["id"], "Unspecified", "Shot Put", ""),
                )
        self.redirect("/login")

    def handle_logout(self) -> None:
        cookie_header = self.headers.get("Cookie", "")
        jar = cookies.SimpleCookie()
        jar.load(cookie_header)
        token = jar.get(SESSION_COOKIE)
        if token:
            SESSIONS.pop(token.value, None)
        expired = f"{SESSION_COOKIE}=deleted; Path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT"
        self.redirect("/", expired)

    def search_profiles(self, q: str, current_user: sqlite3.Row | None) -> str:
        term = q.strip().lower()
        with db_conn() as conn:
            users = conn.execute(
                """
                SELECT u.*, COALESCE(s.name,'') AS school_name
                FROM users u
                LEFT JOIN schools s ON s.id=u.school_id
                WHERE u.role IN ('coach','athlete') AND u.is_admin=0 AND u.locked=0
                ORDER BY u.name
                """
            ).fetchall()
            hide_minor_images = setting_bool(conn, "hide_minor_images_public", True)
        cards = []
        for row in users:
            if row["profile_private"] or row["force_private"]:
                if not current_user or (current_user["id"] != row["id"] and not current_user["is_admin"]):
                    continue
            haystack = f"{(row['name'] or '').lower()} {(row['username'] or '').lower()}"
            if term and term not in haystack:
                continue
            avatar = row["profile_image_url"] or "/static/default-avatar.svg"
            is_owner = bool(current_user and current_user["id"] == row["id"])
            if not can_display_public_images(row, current_user, hide_minor_images, is_owner):
                avatar = "/static/default-avatar.svg"
            display_handle = f"@{row['username']}" if row["username"] else row["role"]
            cards.append(
                f"<a class='card profile-link' href='/profile/{row['id']}'>"
                f"<div class='profile-head'><img class='avatar' src='{esc(avatar)}' alt='avatar'>"
                f"<div><h3>{esc(row['name'])}</h3><p class='muted'>{esc(display_handle)} • {esc(row['role'])} {esc(row['school_name'])}</p></div></div></a>"
            )
        cards_html = "".join(cards) or "<div class='card'>No profiles found.</div>"
        body = (
            "<div class='card'><h2>Search Coaches & Athletes</h2>"
            "<form method='get' action='/search'><div class='inline'>"
            f"<input name='q' value='{esc(q)}' placeholder='Search by name or handle'>"
            "<button type='submit'>Search</button></div></form>"
            "<p class='muted'>Private profiles are hidden from search and viewing.</p></div>"
            f"<div class='grid' style='margin-top:12px'>{cards_html}</div>"
        )
        return html_page("Search", body, current_user)

    def public_profile(self, path: str, current_user: sqlite3.Row | None) -> str:
        user_id = to_int(unquote(path.split("/")[-1]), 0)
        with db_conn() as conn:
            target = conn.execute(
                "SELECT u.*, COALESCE(s.name,'') AS school_name, s.color_primary, s.color_secondary, s.symbol_url "
                "FROM users u LEFT JOIN schools s ON s.id=u.school_id WHERE u.id=?",
                (user_id,),
            ).fetchone()
            if target is None or target["is_admin"] or target["locked"]:
                return html_page("Not Found", "<div class='card'>Profile not found.</div>", current_user)
            is_owner = bool(current_user and current_user["id"] == target["id"])
            if (target["profile_private"] or target["force_private"]) and not is_owner and not (current_user and current_user["is_admin"]):
                return html_page("Private Profile", "<div class='card'>This profile is private.</div>", current_user)
            photos = conn.execute("SELECT image_url, caption FROM galleries WHERE user_id=? ORDER BY id DESC", (target["id"],)).fetchall()
            statuses = conn.execute(
                "SELECT body, image_url, created_at FROM status_updates WHERE user_id=? ORDER BY created_at DESC, id DESC LIMIT 12",
                (target["id"],),
            ).fetchall()
            hide_minor_images = setting_bool(conn, "hide_minor_images_public", True)
            # Athlete-specific: meet results and coach info
            meet_results_rows: list[sqlite3.Row] = []
            coach_info: sqlite3.Row | None = None
            if target["role"] == "athlete":
                athlete_rec = conn.execute("SELECT id, coach_user_id FROM athletes WHERE user_id=?", (target["id"],)).fetchone()
                if athlete_rec:
                    meet_results_rows = conn.execute(
                        "SELECT meet_name,event,feet,inches,meters,meet_date FROM meet_results WHERE athlete_id=? ORDER BY meet_date DESC, id DESC LIMIT 20",
                        (athlete_rec["id"],),
                    ).fetchall()
                    if athlete_rec["coach_user_id"]:
                        coach_info = conn.execute("SELECT id,name FROM users WHERE id=? AND role='coach'", (athlete_rec["coach_user_id"],)).fetchone()

        # Use profile owner's school colors if available
        prof_primary = target["color_primary"] if target["color_primary"] else "#2563eb"
        prof_secondary = target["color_secondary"] if target["color_secondary"] else "#0f172a"
        profile_colors = (prof_primary, prof_secondary)

        can_show_images = can_display_public_images(target, current_user, hide_minor_images, is_owner)
        profile_img = target["profile_image_url"] if can_show_images and target["profile_image_url"] else "/static/default-avatar.svg"
        school_symbol = f"<img class='school-symbol' src='{esc(target['symbol_url'])}' alt='school symbol'>" if target["symbol_url"] else ""
        coach_html = (
            f"<p>Coach: <a href='/profile/{coach_info['id']}'>{esc(coach_info['name'])}</a></p>"
            if coach_info
            else ""
        )
        owner_tools = ""
        if is_owner:
            owner_tools = f"""
            <div class='profile-owner-tools'>
              <a class='btn' href='/profile'>Edit Profile</a>
              <a class='btn secondary' href='{esc(dashboard_path(target))}'>Dashboard</a>
            </div>
            <div class='grid profile-owner-grid'>
              <div class='card'>
                <h3>Post Status Update</h3>
                <form method='post' action='/profile/status' enctype='multipart/form-data'>
                  <label>Update<textarea name='body' required></textarea></label>
                  <label>Image (optional)<input type='file' name='image' accept='image/*'></label>
                  <button type='submit'>Post Update</button>
                </form>
              </div>
              <div class='card'>
                <h3>Add Gallery Photo</h3>
                <form method='post' action='/profile/gallery' enctype='multipart/form-data'>
                  <label>Photo<input type='file' name='image' accept='image/*' required></label>
                  <label>Caption<input name='caption'></label>
                  <button type='submit'>Upload Photo</button>
                </form>
              </div>
            </div>
            """
        gallery_html = (
            "".join(
                f"<figure class='gallery-item'><img src='{esc(p['image_url'])}' alt='gallery image'><figcaption>{esc(p['caption'])}</figcaption></figure>"
                for p in photos
            )
            if can_show_images
            else "<p class='muted'>Images hidden by under-18 privacy setting.</p>"
        )
        if not gallery_html:
            gallery_html = "<p class='muted'>No gallery photos.</p>"

        meet_feed_html = ""
        if meet_results_rows:
            meet_rows = "".join(
                f"<tr><td>{esc(m['meet_date'])}</td><td>{esc(m['meet_name'])}</td><td>{esc(m['event'])}</td><td>{m['feet']}' {m['inches']}\"</td><td>{m['meters']}m</td></tr>"
                for m in meet_results_rows
            )
            meet_feed_html = f"<div class='card' style='margin-top:12px'><h3>Meet Results</h3><table><tr><th>Date</th><th>Meet</th><th>Event</th><th>Distance</th><th>Meters</th></tr>{meet_rows}</table></div>"

        body = f"""
        <div class='card school-accent profile-hero'>
          <div class='profile-head'>
            <img class='avatar large' src='{esc(profile_img)}' alt='avatar'>
            <div>
              <h2>{esc(target['name'])} {school_symbol}</h2>
              <p class='muted'>{('@' + esc(target['username'])) if target['username'] else esc(target['role']).title()} {'• ' + esc(target['school_name']) if target['school_name'] else ''}</p>
              {format_text_block(target['status_text'] or '', 'No headline status yet.')}
              {coach_html}
            </div>
          </div>
          {format_text_block(target['bio'] or '', 'No bio provided.')}
          <div class='tag-wrap'>
            <span class='tag profile-stat-chip'>Grade: {esc(target['grade_level'] or 'N/A')}</span>
            <span class='tag profile-stat-chip'>Age: {esc(target['age'] or 'N/A')}</span>
            <span class='tag profile-stat-chip'>Height: {esc(target['height'] or 'N/A')}</span>
            <span class='tag profile-stat-chip'>Weight: {esc(target['weight'] or 'N/A')}</span>
            <span class='tag profile-stat-chip'>Hometown: {esc(target['hometown'] or 'N/A')} {esc(target['state'] or '')}</span>
          </div>
          {owner_tools}
        </div>
        {meet_feed_html}
        <div class='card profile-section' style='margin-top:12px'><h3>Status Feed</h3>{render_status_feed(statuses)}</div>
        <div class='card profile-section' style='margin-top:12px'><h3>Gallery</h3><div class='gallery'>{gallery_html}</div></div>
        """
        return html_page("Public Profile", body, current_user, profile_colors=profile_colors)

    def my_profile(self, user: sqlite3.Row, error_message: str = "") -> str:
        with db_conn() as conn:
            schools = conn.execute("SELECT name FROM schools ORDER BY name").fetchall()
            photos = conn.execute("SELECT id, image_url, caption FROM galleries WHERE user_id=? ORDER BY id DESC", (user["id"],)).fetchall()
            statuses = conn.execute(
                "SELECT body, image_url, created_at FROM status_updates WHERE user_id=? ORDER BY created_at DESC, id DESC LIMIT 8",
                (user["id"],),
            ).fetchall()
            school_name = ""
            if user["school_id"]:
                school = conn.execute("SELECT name FROM schools WHERE id=?", (user["school_id"],)).fetchone()
                school_name = school["name"] if school else ""
        school_options = "".join(f"<option value='{esc(s['name'])}'></option>" for s in schools)
        photo_rows = "".join(
            f"<div class='gallery-admin-row'><img src='{esc(p['image_url'])}' alt='gallery'><span>{esc(p['caption'])}</span></div>"
            for p in photos
        ) or "<p class='muted'>No gallery photos.</p>"
        error_html = f"<div class='notice card'>{esc(error_message)}</div>" if error_message else ""
        school_field = (
            "<label>School<input list='schools' name='school_name' value='"
            + esc(school_name)
            + "' placeholder='Must be a coach-defined school'></label>"
            + f"<datalist id='schools'>{school_options}</datalist>"
            if user["role"] == "coach" or user["is_admin"]
            else f"<div class='muted'>School: <strong>{esc(school_name or 'Not assigned yet')}</strong> (set by your coach)</div>"
        )
        body = f"""
        {error_html}
        <div class='grid'>
          <div class='card school-accent'>
            <div class='section-head'>
              <div>
                <h2>Profile Details</h2>
                <p class='muted'>Manage your public profile, headline, and contact email.</p>
              </div>
              <a class='btn secondary' href='/profile/{user["id"]}'>View Public Profile</a>
            </div>
            <form method='post' action='/profile' enctype='multipart/form-data'>
              <label>Name<input name='name' value='{esc(user['name'])}' required></label>
              <div class='row'>
                <label>Email<input type='email' name='email' value='{esc(user['email'] or '')}' required></label>
                <label>Handle<input name='username' value='{esc(user['username'] or '')}'></label>
              </div>
              <label>Headline Status<input name='status_text' value='{esc(user['status_text'] or '')}'></label>
              <label>Bio<textarea name='bio'>{esc(user['bio'] or '')}</textarea></label>
              <label>Profile Photo{(' — current: <img src="' + esc(user['profile_image_url']) + '" style="height:40px;border-radius:8px;vertical-align:middle">') if user['profile_image_url'] else ''}<input type='file' name='profile_image' accept='image/*'></label>
              <div class='row'>
                <label>Grade Level<input name='grade_level' value='{esc(user['grade_level'] or '')}'></label>
                <label>Age<input type='number' min='1' max='99' name='age' value='{esc(user['age'] or '')}'></label>
              </div>
              <div class='row'>
                <label>Height<input name='height' value='{esc(user['height'] or '')}'></label>
                <label>Weight<input name='weight' value='{esc(user['weight'] or '')}'></label>
              </div>
              <div class='row'>
                <label>Hometown<input name='hometown' value='{esc(user['hometown'] or '')}'></label>
                <label>State<input name='state' value='{esc(user['state'] or '')}'></label>
              </div>
              {school_field}
              <label class='checkbox-row'><input type='checkbox' name='profile_private' value='1' {'checked' if user['profile_private'] else ''}><span>Hide my profile from public search and profile views</span></label>
              <p class='muted checkbox-help'>When enabled, only you, your coach, and admins can reliably access this profile.</p>
              <button type='submit'>Save Profile</button>
            </form>
          </div>
          <div class='stack'>
            <div class='card'>
              <h3>Share an Update</h3>
              <form method='post' action='/profile/status' enctype='multipart/form-data'>
                <label>Update<textarea name='body' required></textarea></label>
                <label>Image (optional)<input type='file' name='image' accept='image/*'></label>
                <button type='submit'>Post Update</button>
              </form>
            </div>
            <div class='card'>
              <h3>Gallery Uploads</h3>
              <form method='post' action='/profile/gallery' enctype='multipart/form-data'>
                <label>Photo<input type='file' name='image' accept='image/*' required></label>
                <label>Caption<input name='caption'></label>
                <button type='submit'>Upload Photo</button>
              </form>
              <div style='margin-top:12px'>{photo_rows}</div>
            </div>
            <div class='card'>
              <h3>Recent Status Feed</h3>
              {render_status_feed(statuses, "Your updates will appear here.")}
            </div>
          </div>
        </div>
        """
        return html_page("My Profile", body, user)

    def update_profile(self, user: sqlite3.Row) -> None:
        form, files = self.read_form_with_files()
        school_name = form.get("school_name", "").strip()
        email = normalize_email(form.get("email", ""))
        username = form.get("username", "").strip()
        if not is_valid_email(email):
            return self.respond_html(self.my_profile(user, "Enter a valid email address."), status=400)
        # Handle profile image upload
        new_image_url = user["profile_image_url"] or ""
        if "profile_image" in files:
            fname, fdata = files["profile_image"]
            saved = save_upload(fname, fdata)
            if saved:
                new_image_url = saved
        with db_conn() as conn:
            email_exists = conn.execute("SELECT id FROM users WHERE email=? AND id<>?", (email, user["id"])).fetchone()
            if email_exists:
                return self.respond_html(self.my_profile(user, "That email address is already in use."), status=400)
            if username and unique_handle(conn, username, user["id"]) != username:
                return self.respond_html(self.my_profile(user, "That handle is already in use."), status=400)
            school_id = user["school_id"]
            if user["role"] == "coach" or user["is_admin"]:
                school_id = None
                if school_name:
                    school = conn.execute("SELECT id FROM schools WHERE name=?", (school_name,)).fetchone()
                    school_id = school["id"] if school else None
            try:
                conn.execute(
                    """
                    UPDATE users SET
                        name=?, username=?, email=?, status_text=?, bio=?, profile_image_url=?, grade_level=?, age=?,
                        height=?, weight=?, hometown=?, state=?, school_id=?, profile_private=?
                    WHERE id=?
                    """,
                    (
                        form.get("name", user["name"]).strip() or user["name"],
                        username,
                        email,
                        form.get("status_text", "").strip(),
                        form.get("bio", "").strip(),
                        new_image_url,
                        form.get("grade_level", "").strip(),
                        to_int(form.get("age", "0"), 0) or None,
                        form.get("height", "").strip(),
                        form.get("weight", "").strip(),
                        form.get("hometown", "").strip(),
                        form.get("state", "").strip(),
                        school_id,
                        1 if form.get("profile_private") == "1" else 0,
                        user["id"],
                    ),
                )
            except sqlite3.IntegrityError:
                return self.respond_html(self.my_profile(user, "That email or handle is already in use."), status=400)
        self.redirect("/profile")

    def add_status_update(self, user: sqlite3.Row) -> None:
        form, files = self.read_form_with_files()
        body = form.get("body", "").strip()
        if not body:
            return self.redirect(f"/profile/{user['id']}")
        image_url = ""
        if "image" in files:
            fname, fdata = files["image"]
            image_url = save_upload(fname, fdata)
        with db_conn() as conn:
            conn.execute(
                "INSERT INTO status_updates (user_id, body, image_url, created_at) VALUES (?,?,?,?)",
                (user["id"], body, image_url, utc_now().isoformat(timespec="seconds")),
            )
        self.redirect(f"/profile/{user['id']}")

    def add_gallery_photo(self, user: sqlite3.Row) -> None:
        form, files = self.read_form_with_files()
        image_url = ""
        if "image" in files:
            fname, fdata = files["image"]
            image_url = save_upload(fname, fdata)
        if not image_url:
            return self.redirect(f"/profile/{user['id']}")
        with db_conn() as conn:
            conn.execute(
                "INSERT INTO galleries (user_id, image_url, caption, created_at) VALUES (?,?,?,?)",
                (user["id"], image_url, form.get("caption", "").strip(), utc_now().isoformat(timespec="seconds")),
            )
        self.redirect(f"/profile/{user['id']}")

    def admin_page(self, user: sqlite3.Row, error_message: str = "") -> str:
        with db_conn() as conn:
            accounts = conn.execute(
                "SELECT id,username,email,name,role,locked,is_admin FROM users ORDER BY is_admin DESC, id"
            ).fetchall()
            about_text = get_setting(conn, "about_text", "")
            home_cover = get_setting(conn, "home_cover_url", "")
            registration_open = setting_bool(conn, "registration_open", True)
            hide_minor_images_public = setting_bool(conn, "hide_minor_images_public", True)
        rows: list[str] = []
        for a in accounts:
            delete_opt = "" if a["is_admin"] else "<option value='delete'>Delete Account</option>"
            rows.append(
                f"<tr><td>{a['id']}</td><td>{esc(a['name'])}</td><td>{esc(a['email'])}</td><td>{esc(a['username'])}</td><td>{esc(a['role'])}</td><td>{'Yes' if a['locked'] else 'No'}</td>"
                f"<td><form method='post' action='/admin/account-action' class='inline'><input type='hidden' name='user_id' value='{a['id']}'>"
                f"<select name='action'><option value='lock'>Lock</option><option value='unlock'>Unlock</option><option value='reset'>Reset Password</option>{delete_opt}</select><button type='submit'>Apply</button></form></td></tr>"
            )
        account_rows = "".join(rows)
        error_html = f"<div class='notice card'>{esc(error_message)}</div>" if error_message else ""
        body = f"""
        {error_html}
        <div class='grid'>
          <div class='card'>
            <h2>Site Settings</h2>
            <form method='post' action='/admin/settings'>
              <label>About Text<textarea name='about_text'>{esc(about_text)}</textarea></label>
              <label>Home Cover Image URL<input name='home_cover_url' value='{esc(home_cover)}'></label>
              <label><input type='checkbox' name='registration_open' value='1' {'checked' if registration_open else ''}> Enable new account creation</label>
              <label><input type='checkbox' name='hide_minor_images_public' value='1' {'checked' if hide_minor_images_public else ''}> Hide under-18 athlete images publicly</label>
              <label>Admin Email<input type='email' name='admin_email' value='{esc(user['email'])}' required></label>
              <label>Admin Handle<input name='admin_username' value='{esc(user['username'])}'></label>
              <label>Admin New Password<input type='password' name='admin_password'></label>
              <button type='submit'>Save Admin Settings</button>
            </form>
          </div>
          <div class='card'>
            <h2>All Accounts</h2>
            <p class='muted'>Password reset sets a temporary password of <strong>Reset123!</strong>; share it securely and require user to update afterward.</p>
            <table><tr><th>ID</th><th>Name</th><th>Email</th><th>Handle</th><th>Role</th><th>Locked</th><th>Actions</th></tr>{account_rows}</table>
          </div>
        </div>
        """
        return html_page("Admin", body, user)

    def update_admin_settings(self, user: sqlite3.Row) -> None:
        form = self.read_form()
        admin_email = normalize_email(form.get("admin_email", ""))
        admin_username = form.get("admin_username", "").strip()
        admin_password = form.get("admin_password", "")
        if not is_valid_email(admin_email):
            return self.respond_html(self.admin_page(user, "Enter a valid admin email address."), status=400)
        with db_conn() as conn:
            set_setting(conn, "about_text", form.get("about_text", "").strip())
            set_setting(conn, "home_cover_url", form.get("home_cover_url", "").strip())
            set_setting(conn, "registration_open", "1" if form.get("registration_open") == "1" else "0")
            set_setting(conn, "hide_minor_images_public", "1" if form.get("hide_minor_images_public") == "1" else "0")
            email_exists = conn.execute("SELECT id FROM users WHERE email=? AND id<>?", (admin_email, user["id"])).fetchone()
            if email_exists:
                return self.respond_html(self.admin_page(user, "That admin email is already assigned to another account."), status=400)
            try:
                conn.execute("UPDATE users SET email=? WHERE id=?", (admin_email, user["id"]))
                if admin_username:
                    exists = conn.execute("SELECT id FROM users WHERE username=? AND id<>?", (admin_username, user["id"])).fetchone()
                    if not exists:
                        conn.execute("UPDATE users SET username=? WHERE id=?", (admin_username, user["id"]))
                if admin_password:
                    conn.execute("UPDATE users SET password=? WHERE id=?", (hash_password(admin_password), user["id"]))
            except sqlite3.IntegrityError:
                return self.respond_html(self.admin_page(user, "That admin email or handle is already in use."), status=400)
        self.redirect("/admin")

    def admin_account_action(self, user: sqlite3.Row) -> None:
        form = self.read_form()
        account_id = to_int(form.get("user_id", "0"), 0)
        action = form.get("action", "")
        with db_conn() as conn:
            target = conn.execute("SELECT * FROM users WHERE id=?", (account_id,)).fetchone()
            if target is None:
                return self.redirect("/admin")
            if action == "lock":
                conn.execute("UPDATE users SET locked=1 WHERE id=?", (account_id,))
            elif action == "unlock":
                conn.execute("UPDATE users SET locked=0 WHERE id=?", (account_id,))
            elif action == "reset":
                conn.execute("UPDATE users SET password=? WHERE id=?", (hash_password("Reset123!"), account_id))
            elif action == "delete" and not target["is_admin"]:
                athlete = conn.execute("SELECT id FROM athletes WHERE user_id=?", (account_id,)).fetchone()
                if athlete:
                    conn.execute("DELETE FROM assignments WHERE athlete_id=?", (athlete["id"],))
                    conn.execute("DELETE FROM throw_logs WHERE athlete_id=?", (athlete["id"],))
                    conn.execute("DELETE FROM lift_logs WHERE athlete_id=?", (athlete["id"],))
                    conn.execute("DELETE FROM meet_results WHERE athlete_id=?", (athlete["id"],))
                    conn.execute("DELETE FROM prs WHERE athlete_id=?", (athlete["id"],))
                    conn.execute("DELETE FROM athletes WHERE id=?", (athlete["id"],))
                conn.execute("DELETE FROM status_updates WHERE user_id=?", (account_id,))
                conn.execute("DELETE FROM galleries WHERE user_id=?", (account_id,))
                conn.execute("DELETE FROM users WHERE id=?", (account_id,))
        self.redirect("/admin")

    def coach_schools(self, user: sqlite3.Row) -> str:
        with db_conn() as conn:
            schools = conn.execute("SELECT * FROM schools WHERE coach_user_id=? ORDER BY id DESC", (user["id"],)).fetchall()
        school_rows = []
        for s in schools:
            cp = esc(s["color_primary"])
            cs = esc(s["color_secondary"])
            sym = f'<img src="{esc(s["symbol_url"])}" style="height:28px">' if s["symbol_url"] else "—"
            swatch_style = "display:inline-block;width:20px;height:20px;border-radius:4px;border:1px solid #ccc;vertical-align:middle"
            school_rows.append(
                f"<tr><td>{esc(s['name'])}</td>"
                f"<td><span style='{swatch_style};background:{cp}'></span> {cp}</td>"
                f"<td><span style='{swatch_style};background:{cs}'></span> {cs}</td>"
                f"<td>{sym}</td></tr>"
            )
        rows = "".join(school_rows) or "<tr><td colspan='4' class='muted'>No schools yet.</td></tr>"
        body = f"""
        <div class='grid'>
          <div class='card'>
            <h3>Create School</h3>
            <form method='post' action='/coach/schools' enctype='multipart/form-data'>
              <label>School Name<input name='name' required></label>
              <div class='row'>
                <label>Primary Color<input type='color' name='color_primary' value='#2563eb'></label>
                <label>Secondary Color<input type='color' name='color_secondary' value='#0f172a'></label>
              </div>
              <label>School Symbol / Logo<input type='file' name='symbol' accept='image/*'></label>
              <button type='submit'>Save School</button>
            </form>
          </div>
          <div class='card'><h3>My Schools</h3><table><tr><th>Name</th><th>Primary</th><th>Secondary</th><th>Symbol</th></tr>{rows}</table></div>
        </div>
        <div class='card' style='margin-top:12px'>
          <h3>Edit Existing Schools</h3>
          <div class='stack'>
            {
                "".join(
                    (
                        f"<form method='post' action='/coach/schools/update' enctype='multipart/form-data' class='school-edit-form'>"
                        f"<input type='hidden' name='school_id' value='{s['id']}'>"
                        f"<div class='row'><label>School Name<input name='name' value='{esc(s['name'])}' required></label>"
                        f"<label>Replace Symbol / Logo<input type='file' name='symbol' accept='image/*'></label></div>"
                        f"<div class='row'><label>Primary Color<input type='color' name='color_primary' value='{esc(s['color_primary'])}'></label>"
                        f"<label>Secondary Color<input type='color' name='color_secondary' value='{esc(s['color_secondary'])}'></label></div>"
                        f"<button type='submit'>Update School</button>"
                        f"</form>"
                    )
                    for s in schools
                )
                or "<p class='muted'>Create a school first to enable editing.</p>"
            }
          </div>
        </div>
        """
        return html_page("Schools", body, user)

    def add_school(self, user: sqlite3.Row) -> None:
        form, files = self.read_form_with_files()
        name = form.get("name", "").strip()
        if not name:
            return self.redirect("/coach/schools")
        symbol_url = ""
        if "symbol" in files:
            fname, fdata = files["symbol"]
            symbol_url = save_upload(fname, fdata)
        with db_conn() as conn:
            existing = conn.execute("SELECT id FROM schools WHERE name=?", (name,)).fetchone()
            if existing:
                return self.redirect("/coach/schools")
            conn.execute(
                "INSERT INTO schools (coach_user_id, name, color_primary, color_secondary, symbol_url) VALUES (?,?,?,?,?)",
                (
                    user["id"],
                    name,
                    form.get("color_primary", "#2563eb"),
                    form.get("color_secondary", "#0f172a"),
                    symbol_url,
                ),
            )
        self.redirect("/coach/schools")

    def update_school(self, user: sqlite3.Row) -> None:
        form, files = self.read_form_with_files()
        school_id = to_int(form.get("school_id", "0"), 0)
        name = form.get("name", "").strip()
        if not school_id or not name:
            return self.redirect("/coach/schools")
        with db_conn() as conn:
            existing = conn.execute("SELECT * FROM schools WHERE id=? AND coach_user_id=?", (school_id, user["id"])).fetchone()
            if existing is None:
                return self.redirect("/coach/schools")
            symbol_url = existing["symbol_url"] or ""
            if "symbol" in files:
                fname, fdata = files["symbol"]
                uploaded = save_upload(fname, fdata)
                if uploaded:
                    symbol_url = uploaded
            try:
                conn.execute(
                    "UPDATE schools SET name=?, color_primary=?, color_secondary=?, symbol_url=? WHERE id=? AND coach_user_id=?",
                    (
                        name,
                        form.get("color_primary", existing["color_primary"] or "#2563eb"),
                        form.get("color_secondary", existing["color_secondary"] or "#0f172a"),
                        symbol_url,
                        school_id,
                        user["id"],
                    ),
                )
            except sqlite3.IntegrityError:
                pass
        self.redirect("/coach/schools")

    def coach_athlete_privacy(self, user: sqlite3.Row) -> None:
        form = self.read_form()
        athlete_id = to_int(form.get("athlete_id", "0"), 0)
        force_private = 1 if form.get("force_private") == "1" else 0
        with db_conn() as conn:
            athlete = conn.execute("SELECT user_id, coach_user_id FROM athletes WHERE id=?", (athlete_id,)).fetchone()
            if athlete and athlete["coach_user_id"] == user["id"]:
                conn.execute("UPDATE users SET force_private=? WHERE id=?", (force_private, athlete["user_id"]))
        self.redirect("/coach/athletes")

    def coach_athlete_school(self, user: sqlite3.Row) -> None:
        form = self.read_form()
        user_id = to_int(form.get("user_id", "0"), 0)
        school_id = to_int(form.get("school_id", "0"), 0)
        with db_conn() as conn:
            athlete = conn.execute("SELECT coach_user_id FROM athletes WHERE user_id=?", (user_id,)).fetchone()
            school = conn.execute("SELECT id FROM schools WHERE id=? AND coach_user_id=?", (school_id, user["id"])).fetchone() if school_id else None
            if athlete and athlete["coach_user_id"] == user["id"]:
                conn.execute("UPDATE users SET school_id=? WHERE id=?", (school["id"] if school else None, user_id))
        self.redirect("/coach/athletes")

    def coach_dashboard(self, user: sqlite3.Row) -> str:
        today = date.today().isoformat()
        week_ago = (date.today() - timedelta(days=7)).isoformat()
        with db_conn() as conn:
            total_athletes = conn.execute("SELECT COUNT(*) AS c FROM athletes").fetchone()["c"]
            todays_assignments = conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM assignments a
                JOIN practice_plans p ON p.id=a.plan_id
                WHERE p.practice_date=?
                """,
                (today,),
            ).fetchone()["c"]
            completed_today = conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM assignments a
                JOIN practice_plans p ON p.id=a.plan_id
                WHERE p.practice_date=? AND a.status='completed'
                """,
                (today,),
            ).fetchone()["c"]
            recent_prs = conn.execute(
                """
                SELECT u.name, p.category, p.event_or_lift, p.value_text, p.achieved_on
                FROM prs p
                JOIN athletes a ON a.id=p.athlete_id
                LEFT JOIN users u ON u.id=a.user_id
                ORDER BY p.achieved_on DESC, p.id DESC LIMIT 8
                """
            ).fetchall()
            stale_athletes = conn.execute(
                """
                SELECT COALESCE(u.name,'Athlete #' || a.id) AS athlete_name
                FROM athletes a
                LEFT JOIN users u ON u.id=a.user_id
                WHERE a.id NOT IN (
                  SELECT athlete_id FROM throw_logs WHERE practice_date>=?
                )
                LIMIT 10
                """,
                (week_ago,),
            ).fetchall()

        completion_pct = round((completed_today / todays_assignments) * 100, 1) if todays_assignments else 0
        prs_html = "".join(
            f"<tr><td>{esc(r['name'] or 'Unknown')}</td><td>{esc(r['category'])}</td>"
            f"<td>{esc(r['event_or_lift'])}</td><td>{esc(r['value_text'])}</td><td>{esc(r['achieved_on'])}</td></tr>"
            for r in recent_prs
        ) or "<tr><td colspan='5' class='muted'>No PR data yet.</td></tr>"
        stale_html = "".join(f"<span class='tag'>{esc(r['athlete_name'])}</span>" for r in stale_athletes) or "<span class='muted'>All athletes have throw data in the past week.</span>"

        body = f"""
        <div class='grid'>
          <div class='card'><h3>{total_athletes}</h3><div class='muted'>Athletes</div></div>
          <div class='card'><h3>{todays_assignments}</h3><div class='muted'>Today's Assignments</div></div>
          <div class='card'><h3>{completed_today}</h3><div class='muted'>Completed Today</div></div>
          <div class='card'><h3>{completion_pct}%</h3><div class='muted'>Completion Rate</div></div>
        </div>
        <div class='card' style='margin-top:12px'>
          <h3>Recent PR Updates</h3>
          <table>
            <tr><th>Athlete</th><th>Type</th><th>Discipline</th><th>Value</th><th>Date</th></tr>
            {prs_html}
          </table>
        </div>
        <div class='card' style='margin-top:12px'>
          <h3>Throw Alerts (No throw entries in 7+ days)</h3>
          {stale_html}
        </div>
        """
        return html_page("Coach Dashboard", body, user)

    def coach_athletes(self, user: sqlite3.Row, error_message: str = "") -> str:
        with db_conn() as conn:
            athletes = conn.execute(
                """
                SELECT a.id, COALESCE(u.name,'Unlinked') AS name, COALESCE(u.username,'(none)') AS username,
                       COALESCE(u.email,'') AS email,
                       a.sex, a.events, COALESCE(a.group_name,'') AS group_name,
                       u.profile_private, u.force_private, u.id AS user_id, COALESCE(s.name,'') AS school_name,
                       s.id AS school_id_current
                FROM athletes a
                LEFT JOIN users u ON u.id=a.user_id
                LEFT JOIN schools s ON s.id=u.school_id
                WHERE a.coach_user_id=?
                ORDER BY name
                """,
                (user["id"],),
            ).fetchall()
            school_choices = conn.execute("SELECT id,name FROM schools WHERE coach_user_id=? ORDER BY name", (user["id"],)).fetchall()

        school_options = "".join(f"<option value='{s['id']}'>{esc(s['name'])}</option>" for s in school_choices)

        rows = []
        for r in athletes:
            selected_school = "".join(
                f"<option value='{s['id']}' {'selected' if r['school_id_current'] == s['id'] else ''}>{esc(s['name'])}</option>"
                for s in school_choices
            )
            rows.append(
                f"<tr>"
                f"<td><a href='/profile/{r['user_id']}'>{esc(r['name'])}</a></td>"
                f"<td>{esc(r['email'])}</td>"
                f"<td>{esc(r['events'])}</td>"
                f"<td>{esc(r['school_name']) or '—'}</td>"
                f"<td>{'🔒' if r['profile_private'] or r['force_private'] else '🔓'}</td>"
                f"<td class='roster-actions'>"
                f"<form method='post' action='/coach/athlete/privacy'>"
                f"<input type='hidden' name='athlete_id' value='{r['id']}'>"
                f"<select name='force_private'><option value='0'>Public</option><option value='1' {'selected' if r['force_private'] else ''}>Force Private</option></select>"
                f"<button type='submit' class='btn-small'>Privacy</button></form>"
                f"<form method='post' action='/coach/athlete/school'>"
                f"<input type='hidden' name='user_id' value='{r['user_id']}'>"
                f"<select name='school_id'><option value=''>No School</option>{selected_school}</select>"
                f"<button type='submit' class='btn-small'>School</button></form>"
                f"</td></tr>"
            )
        roster_html = "".join(rows) or "<tr><td colspan='6' class='muted'>No athletes in your roster. Use 'Add Athlete' or invite athletes via search.</td></tr>"

        error_html = f"<div class='notice card'>{esc(error_message)}</div>" if error_message else ""

        body = f"""
        {error_html}
        <div class='section-head'>
          <h2>Athlete Roster</h2>
          <a class='btn secondary' href='/coach/athletes/search'>Search &amp; Invite Athletes</a>
        </div>
        <div class='grid'>
          <div class='card'>
            <h3>Create New Athlete Account</h3>
            <form method='post' action='/coach/athletes' autocomplete='off'>
              <label>Name<input name='name' required></label>
              <div class='row'>
                <label>Email<input type='email' name='email' autocomplete='off' autocapitalize='off' spellcheck='false' required></label>
                <label>Handle<input name='username' placeholder='Optional'></label>
              </div>
              <label>Password<input type='password' name='password' autocomplete='new-password' required></label>
              <div class='row'>
                <label>Sex<select name='sex'><option>Male</option><option>Female</option></select></label>
                <label>Group<input name='group_name' placeholder='Varsity Throws'></label>
              </div>
              <label>Events (comma-separated)<input name='events' value='Shot Put,Discus'></label>
              <button type='submit'>Create Athlete</button>
            </form>
          </div>
          <div class='card' style='overflow-x:auto'>
            <h3>Roster</h3>
            <table class='roster-table'>
              <tr><th>Name</th><th>Email</th><th>Events</th><th>School</th><th>Privacy</th><th>Actions</th></tr>
              {roster_html}
            </table>
          </div>
        </div>
        """
        return html_page("Athlete Roster", body, user)

    def add_athlete(self, user: sqlite3.Row) -> None:
        form = self.read_form()
        email = normalize_email(form.get("email", ""))
        username = form.get("username", "").strip() or default_handle_from_email(email)
        if not is_valid_email(email):
            return self.respond_html(self.coach_athletes(user, "Enter a valid athlete email address."), status=400)
        with db_conn() as conn:
            exists = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
            if exists:
                return self.respond_html(self.coach_athletes(user, "That athlete email already exists."), status=400)
            username = unique_handle(conn, username)
            # Apply coach's school to the new athlete if coach has one
            coach_school = conn.execute("SELECT school_id FROM users WHERE id=?", (user["id"],)).fetchone()
            athlete_school_id = coach_school["school_id"] if coach_school and coach_school["school_id"] else None
            try:
                conn.execute(
                    "INSERT INTO users (username,email,password,role,name,school_id) VALUES (?,?,?,?,?,?)",
                    (
                        username,
                        email,
                        hash_password(form.get("password", "athlete123")),
                        "athlete",
                        form.get("name", "Athlete"),
                        athlete_school_id,
                    ),
                )
            except sqlite3.IntegrityError:
                return self.respond_html(self.coach_athletes(user, "That athlete email or handle is already in use."), status=400)
            user_id = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()["id"]
            conn.execute(
                "INSERT INTO athletes (user_id,sex,events,group_name,coach_user_id) VALUES (?,?,?,?,?)",
                (user_id, form.get("sex", "Male"), form.get("events", "Shot Put"), form.get("group_name", ""), user["id"]),
            )
        self.redirect("/coach/athletes")

    def coach_athlete_search(self, user: sqlite3.Row, q: str = "") -> str:
        term = q.strip().lower()
        with db_conn() as conn:
            # Search ALL athletes (including hidden), excluding those already in coach's roster
            all_athletes = conn.execute(
                """
                SELECT u.id, u.name, u.username, u.email, u.profile_private, u.force_private,
                       a.id AS athlete_id, a.events, a.coach_user_id,
                       COALESCE(ci.status,'') AS invite_status
                FROM athletes a
                JOIN users u ON u.id=a.user_id
                LEFT JOIN coach_invites ci ON ci.athlete_user_id=u.id AND ci.coach_user_id=?
                WHERE u.locked=0 AND (a.coach_user_id IS NULL OR a.coach_user_id!=?)
                ORDER BY u.name
                """,
                (user["id"], user["id"]),
            ).fetchall()
        cards = []
        for row in all_athletes:
            haystack = f"{(row['name'] or '').lower()} {(row['username'] or '').lower()} {(row['email'] or '').lower()}"
            if term and term not in haystack:
                continue
            invite_status = row["invite_status"]
            if invite_status == "pending":
                action_html = "<span class='tag'>Invite Pending</span>"
            elif invite_status == "accepted":
                action_html = "<span class='tag'>Already on roster</span>"
            else:
                action_html = (
                    f"<form method='post' action='/coach/invite'>"
                    f"<input type='hidden' name='athlete_user_id' value='{row['id']}'>"
                    f"<button type='submit' class='btn-small'>Invite</button></form>"
                )
            privacy_badge = " <span class='tag' style='font-size:0.75rem'>Private</span>" if row["profile_private"] or row["force_private"] else ""
            cards.append(
                f"<div class='card'><div class='profile-head'>"
                f"<div><h4>{esc(row['name'])}{privacy_badge}</h4>"
                f"<p class='muted'>{esc(row['events'] or '')}</p></div>"
                f"</div>{action_html}</div>"
            )
        cards_html = "".join(cards) or "<div class='card muted'>No athletes found.</div>"
        body = (
            "<div class='section-head'><h2>Search &amp; Invite Athletes</h2>"
            f"<a class='btn secondary' href='/coach/athletes'>Back to Roster</a></div>"
            "<div class='card'><form method='get' action='/coach/athletes/search'><div class='inline'>"
            f"<input name='q' value='{esc(q)}' placeholder='Search by name, handle or email'>"
            "<button type='submit'>Search</button></div></form>"
            "<p class='muted'>This list shows athletes not yet on your roster. Private athletes are also shown here.</p></div>"
            f"<div class='grid' style='margin-top:12px'>{cards_html}</div>"
        )
        return html_page("Search Athletes", body, user)

    def coach_send_invite(self, user: sqlite3.Row) -> None:
        form = self.read_form()
        athlete_user_id = to_int(form.get("athlete_user_id", "0"), 0)
        if not athlete_user_id:
            return self.redirect("/coach/athletes/search")
        with db_conn() as conn:
            target = conn.execute("SELECT id FROM users WHERE id=? AND role='athlete'", (athlete_user_id,)).fetchone()
            if target is None:
                return self.redirect("/coach/athletes/search")
            # Check athlete is not already on this coach's roster
            already = conn.execute(
                "SELECT id FROM athletes WHERE user_id=? AND coach_user_id=?", (athlete_user_id, user["id"])
            ).fetchone()
            if already:
                return self.redirect("/coach/athletes")
            try:
                conn.execute(
                    "INSERT INTO coach_invites (coach_user_id, athlete_user_id, status, created_at) VALUES (?,?,?,?)",
                    (user["id"], athlete_user_id, "pending", utc_now().isoformat(timespec="seconds")),
                )
            except sqlite3.IntegrityError:
                pass  # invite already exists
        self.redirect("/coach/athletes/search")

    def respond_invite(self, user: sqlite3.Row) -> None:
        form = self.read_form()
        invite_id = to_int(form.get("invite_id", "0"), 0)
        action = form.get("action", "")
        with db_conn() as conn:
            invite = conn.execute(
                "SELECT * FROM coach_invites WHERE id=? AND athlete_user_id=? AND status='pending'",
                (invite_id, user["id"]),
            ).fetchone()
            if invite is None:
                return self.redirect("/athlete")
            if action == "accept":
                conn.execute("UPDATE coach_invites SET status='accepted' WHERE id=?", (invite_id,))
                # Set the athlete's coach_user_id
                athlete_rec = conn.execute("SELECT id FROM athletes WHERE user_id=?", (user["id"],)).fetchone()
                if athlete_rec:
                    conn.execute("UPDATE athletes SET coach_user_id=? WHERE id=?", (invite["coach_user_id"], athlete_rec["id"]))
                # Apply coach's school to athlete
                coach_school = conn.execute("SELECT school_id FROM users WHERE id=?", (invite["coach_user_id"],)).fetchone()
                if coach_school and coach_school["school_id"]:
                    conn.execute("UPDATE users SET school_id=? WHERE id=?", (coach_school["school_id"], user["id"]))
            else:
                conn.execute("UPDATE coach_invites SET status='denied' WHERE id=?", (invite_id,))
        self.redirect("/athlete")

    def coach_modules(self, user: sqlite3.Row) -> str:
        with db_conn() as conn:
            modules = conn.execute("SELECT * FROM training_modules ORDER BY id DESC").fetchall()

        rows = "".join(
            f"<tr><td>{m['id']}</td><td>{esc(m['name'])}</td><td>{esc(m['category'])}</td><td>{esc(m['description'])}</td></tr>"
            for m in modules
        )
        body = f"""
        <div class='grid'>
          <div class='card'>
            <h3>Create Training Module</h3>
            <form method='post' action='/coach/modules'>
              <label>Module Name<input name='name' required></label>
              <label>Category<select name='category'><option>Technique</option><option>Throws</option><option>Weight Room</option></select></label>
              <label>Description<textarea name='description' required></textarea></label>
              <button type='submit'>Save Module</button>
            </form>
          </div>
          <div class='card'>
            <h3>Module Library</h3>
            <table><tr><th>ID</th><th>Name</th><th>Category</th><th>Description</th></tr>{rows}</table>
          </div>
        </div>
        """
        return html_page("Modules", body, user)

    def add_module(self, user: sqlite3.Row) -> None:
        form = self.read_form()
        with db_conn() as conn:
            conn.execute(
                "INSERT INTO training_modules (name,category,description) VALUES (?,?,?)",
                (form.get("name", "Module"), form.get("category", "Technique"), form.get("description", "")),
            )
        self.redirect("/coach/modules")

    def coach_plans(self, user: sqlite3.Row) -> str:
        with db_conn() as conn:
            modules = conn.execute("SELECT id,name,category FROM training_modules ORDER BY name").fetchall()
            plans = conn.execute("SELECT * FROM practice_plans WHERE created_by=? ORDER BY practice_date DESC, id DESC", (user["id"],)).fetchall()
            athletes = conn.execute(
                "SELECT a.id, COALESCE(u.name, 'Athlete #' || a.id) AS athlete_name FROM athletes a LEFT JOIN users u ON u.id=a.user_id WHERE a.coach_user_id=? ORDER BY athlete_name",
                (user["id"],),
            ).fetchall()

            plan_cards = []
            for p in plans:
                items = conn.execute(
                    """
                    SELECT m.name, m.category, i.reps, i.notes
                    FROM practice_plan_items i JOIN training_modules m ON m.id=i.module_id
                    WHERE i.plan_id=?
                    """,
                    (p["id"],),
                ).fetchall()
                item_list = "".join(
                    f"<li>{esc(it['name'])} ({esc(it['category'])}) - {esc(it['reps'] or '')} {esc(it['notes'] or '')}</li>"
                    for it in items
                ) or "<li class='muted'>No items.</li>"
                assigned_count = conn.execute("SELECT COUNT(*) AS c FROM assignments WHERE plan_id=?", (p["id"],)).fetchone()["c"]
                assigned_athletes = conn.execute(
                    """
                    SELECT COALESCE(u.name, 'Athlete #' || a.id) AS athlete_name
                    FROM assignments ass
                    JOIN athletes a ON a.id=ass.athlete_id
                    LEFT JOIN users u ON u.id=a.user_id
                    WHERE ass.plan_id=?
                    ORDER BY athlete_name
                    """,
                    (p["id"],),
                ).fetchall()
                assigned_tags = "".join(f"<span class='tag'>{esc(a['athlete_name'])}</span>" for a in assigned_athletes) or "<span class='muted'>No athletes assigned yet.</span>"
                athlete_checks = "".join(
                    f"<label class='checkbox-row plan-checkbox'><input type='checkbox' class='plan-athlete-choice' value='{a['id']}'><span>{esc(a['athlete_name'])}</span></label>"
                    for a in athletes
                ) or "<p class='muted'>Add athletes to your roster before assigning plans.</p>"
                plan_cards.append(
                    f"<div class='card plan-card'><h4>{esc(p['title'])}</h4><div class='muted'>{esc(p['practice_date'])} • {assigned_count} assigned</div>"
                    f"<p>{esc(p['notes'] or 'No plan-level notes.')}</p>"
                    f"<h5>Modules</h5><ul>{item_list}</ul>"
                    f"<h5>Assigned Athletes</h5><div class='tag-wrap'>{assigned_tags}</div>"
                    f"<form method='post' action='/coach/assign'><input type='hidden' name='plan_id' value='{p['id']}'><input type='hidden' name='athlete_ids' value=''>"
                    "<div class='plan-select-tools'>"
                    "<label class='checkbox-row plan-checkbox'><input type='checkbox' class='plan-select-all'><span>Select all athletes</span></label>"
                    "</div>"
                    f"<div class='plan-athlete-list'>{athlete_checks}</div>"
                    "<button type='submit'>Assign to Selected Athletes</button></form></div>"
                )

        module_options = "".join(
            f"<option value='{m['id']}'>{esc(m['name'])} ({esc(m['category'])})</option>" for m in modules
        )
        athlete_help = ", ".join(esc(a["athlete_name"]) for a in athletes) or "No athletes found"
        plans_html = "".join(plan_cards) if plan_cards else "<div class='card'>No plans yet.</div>"

        body = f"""
        <div class='grid plan-layout'>
          <div class='card school-accent'>
            <h3>Create Practice Plan</h3>
            <p class='muted'>Build a plan first, then assign it to one or more athletes from a selectable list.</p>
            <form method='post' action='/coach/plans'>
              <label>Title<input name='title' required></label>
              <div class='row'>
                <label>Date<input type='date' name='practice_date' value='{date.today().isoformat()}' required></label>
                <label>Module<select name='module_id'>{module_options}</select></label>
              </div>
              <div class='row'>
                <label>Reps / Volume<input name='reps' placeholder='3x5 or 6 throws'></label>
                <label>Notes<input name='notes' placeholder='Quality over volume'></label>
              </div>
              <label>Plan Notes<textarea name='plan_notes'></textarea></label>
              <button type='submit'>Save Practice Plan</button>
            </form>
            <p class='muted'>Current roster: {athlete_help}</p>
          </div>
          <div class='stack'>
            <div class='card'>
              <h3>Practice Plans</h3>
              <p class='muted'>Review plan details and assign quickly to one, many, or all athletes.</p>
            </div>
            {plans_html}
          </div>
        </div>
        """
        return html_page("Practice Plans", body, user)

    def add_plan(self, user: sqlite3.Row) -> None:
        form = self.read_form()
        practice_date = form.get("practice_date", date.today().isoformat())
        with db_conn() as conn:
            conn.execute(
                "INSERT INTO practice_plans (title,practice_date,notes,created_by) VALUES (?,?,?,?)",
                (form.get("title", "Practice"), practice_date, form.get("plan_notes", ""), user["id"]),
            )
            plan_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            conn.execute(
                "INSERT INTO practice_plan_items (plan_id,module_id,reps,notes) VALUES (?,?,?,?)",
                (plan_id, to_int(form.get("module_id", "1"), 1), form.get("reps", ""), form.get("notes", "")),
            )
        self.redirect("/coach/plans")

    def assign_plan(self, user: sqlite3.Row) -> None:
        form = self.read_form()
        plan_id = to_int(form.get("plan_id", "0"), 0)
        athlete_ids_raw = form.get("athlete_ids", "")
        athlete_ids = [to_int(x, -1) for x in athlete_ids_raw.split(",") if x.strip()]
        if not athlete_ids or plan_id < 1:
            self.redirect("/coach/plans")
            return
        with db_conn() as conn:
            plan = conn.execute("SELECT id FROM practice_plans WHERE id=? AND created_by=?", (plan_id, user["id"])).fetchone()
            if plan is None:
                return self.redirect("/coach/plans")
            for athlete_id in athlete_ids:
                if athlete_id < 1:
                    continue
                athlete = conn.execute("SELECT id FROM athletes WHERE id=? AND coach_user_id=?", (athlete_id, user["id"])).fetchone()
                if athlete is None:
                    continue
                existing = conn.execute(
                    "SELECT id FROM assignments WHERE plan_id=? AND athlete_id=?",
                    (plan_id, athlete_id),
                ).fetchone()
                if existing is None:
                    conn.execute(
                        "INSERT INTO assignments (plan_id,athlete_id,status,assigned_on) VALUES (?,?,?,?)",
                        (plan_id, athlete_id, "assigned", date.today().isoformat()),
                    )
        self.redirect("/coach/plans")

    def coach_reports(self, user: sqlite3.Row) -> str:
        since = (date.today() - timedelta(days=7)).isoformat()
        with db_conn() as conn:
            weekly_completion = conn.execute(
                """
                SELECT COALESCE(u.name,'Athlete #' || a.id) AS athlete_name,
                       SUM(CASE WHEN ass.status='completed' THEN 1 ELSE 0 END) AS done,
                       COUNT(*) AS total
                FROM assignments ass
                JOIN athletes a ON a.id=ass.athlete_id
                LEFT JOIN users u ON u.id=a.user_id
                JOIN practice_plans p ON p.id=ass.plan_id
                WHERE p.practice_date>=?
                GROUP BY ass.athlete_id
                ORDER BY athlete_name
                """,
                (since,),
            ).fetchall()
            throw_trend = conn.execute(
                """
                SELECT practice_date, AVG(total_inches) AS avg_inches
                FROM throw_logs
                WHERE practice_date>=?
                GROUP BY practice_date
                ORDER BY practice_date
                """,
                (since,),
            ).fetchall()

        rows = "".join(
            f"<tr><td>{esc(r['athlete_name'])}</td><td>{r['done']}</td><td>{r['total']}</td><td>{round((r['done']/r['total']*100),1) if r['total'] else 0}%</td></tr>"
            for r in weekly_completion
        ) or "<tr><td colspan='4' class='muted'>No weekly assignment data.</td></tr>"

        trend_tags = "".join(
            f"<span class='tag'>{esc(t['practice_date'])}: {round((t['avg_inches'] or 0)/12,1)} ft avg</span>"
            for t in throw_trend
        ) or "<span class='muted'>No throw trend data in past week.</span>"

        body = f"""
        <div class='grid'>
          <div class='card'>
            <h3>Weekly Completion (Last 7 Days)</h3>
            <table><tr><th>Athlete</th><th>Done</th><th>Total</th><th>Completion</th></tr>{rows}</table>
          </div>
          <div class='card'>
            <h3>Throw Trend Snapshot</h3>
            {trend_tags}
            <p class='muted'>For MVP this replaces a full charting package and still gives date-by-date trend visibility.</p>
          </div>
        </div>
        """
        return html_page("Reports", body, user)

    def athlete_context(self, user: sqlite3.Row) -> sqlite3.Row | None:
        with db_conn() as conn:
            return conn.execute("SELECT * FROM athletes WHERE user_id=?", (user["id"],)).fetchone()

    def athlete_dashboard(self, user: sqlite3.Row) -> str:
        athlete = self.athlete_context(user)
        if athlete is None:
            return html_page("Athlete", "<div class='card'>No athlete profile is linked to this account.</div>", user)

        today = date.today().isoformat()
        with db_conn() as conn:
            assignments = conn.execute(
                """
                SELECT ass.id AS assignment_id, ass.status, p.title, p.practice_date, p.notes
                FROM assignments ass
                JOIN practice_plans p ON p.id=ass.plan_id
                WHERE ass.athlete_id=? AND p.practice_date<=?
                ORDER BY p.practice_date DESC
                """,
                (athlete["id"], today),
            ).fetchall()
            items_by_assignment: dict[int, list[sqlite3.Row]] = {}
            for ass in assignments:
                items_by_assignment[ass["assignment_id"]] = conn.execute(
                    """
                    SELECT m.name, m.category, i.reps, i.notes
                    FROM practice_plan_items i
                    JOIN assignments a ON a.plan_id=i.plan_id
                    JOIN training_modules m ON m.id=i.module_id
                    WHERE a.id=?
                    """,
                    (ass["assignment_id"],),
                ).fetchall()
            # Pending coach invites
            pending_invites = conn.execute(
                """
                SELECT ci.id, ci.coach_user_id, u.name AS coach_name
                FROM coach_invites ci
                JOIN users u ON u.id=ci.coach_user_id
                WHERE ci.athlete_user_id=? AND ci.status='pending'
                ORDER BY ci.created_at DESC
                """,
                (user["id"],),
            ).fetchall()

        invite_banners = "".join(
            f"<div class='invite-banner card notice'>"
            f"<p>🏅 Coach <strong>{esc(inv['coach_name'])}</strong> wants to add you to their roster.</p>"
            f"<div class='inline'>"
            f"<form method='post' action='/athlete/invite/respond'>"
            f"<input type='hidden' name='invite_id' value='{inv['id']}'>"
            f"<input type='hidden' name='action' value='accept'>"
            f"<button type='submit'>Accept</button></form>"
            f"<form method='post' action='/athlete/invite/respond'>"
            f"<input type='hidden' name='invite_id' value='{inv['id']}'>"
            f"<input type='hidden' name='action' value='deny'>"
            f"<button type='submit' class='secondary'>Deny</button></form>"
            f"</div></div>"
            for inv in pending_invites
        )

        assignment_cards = []
        for ass in assignments:
            items = items_by_assignment.get(ass["assignment_id"], [])
            item_list = "".join(
                f"<li>{esc(i['name'])} ({esc(i['category'])}) — {esc(i['reps'] or '')} {esc(i['notes'] or '')}</li>"
                for i in items
            ) or "<li class='muted'>No item details</li>"
            complete_button = ""
            if ass["status"] != "completed":
                complete_button = (
                    f"<form method='post' action='/athlete/assignment/{ass['assignment_id']}/complete'>"
                    "<button type='submit'>Mark Completed</button></form>"
                )
            assignment_cards.append(
                f"<div class='card'><h4>{esc(ass['title'])}</h4><div class='muted'>{esc(ass['practice_date'])} — {esc(ass['status'])}</div>"
                f"<ul>{item_list}</ul>{complete_button}</div>"
            )

        assignments_html = "".join(assignment_cards) if assignment_cards else "<div class='card'>No assignments yet.</div>"
        body = f"""
        {invite_banners}
        <div class='card'>
          <h3>Welcome back, {esc(user['name'])}</h3>
          <p class='muted'>Events: {esc(athlete['events'])} | Group: {esc(athlete['group_name'] or 'N/A')}</p>
        </div>
        <div class='grid' style='margin-top:12px'>
          <div>
            <h3 style='margin:0 0 10px'>Assignments</h3>
            {assignments_html}
          </div>
          <div>
            <div class='card'>
              <h3>Log Throw</h3>
              <form method='post' action='/athlete/throws'>
                <label>Event<select name='event'><option>Shot Put</option><option>Discus</option><option>Javelin</option></select></label>
                <div class='row'><label>Feet<input type='number' name='feet' min='0' required></label><label>Inches<input type='number' name='inches' min='0' max='11' required></label></div>
                <button type='submit'>Save Throw</button>
              </form>
            </div>
            <div class='card' style='margin-top:12px'>
              <h3>Log Lift</h3>
              <form method='post' action='/athlete/lifts'>
                <label>Lift Name<input name='lift_name' value='Power Clean' required></label>
                <div class='row'><label>Weight (lbs)<input type='number' step='0.1' name='weight' required></label><label>Reps<input type='number' name='reps' min='1' required></label></div>
                <button type='submit'>Save Lift</button>
              </form>
            </div>
            <div class='card' style='margin-top:12px'>
              <h3>Log Meet Result</h3>
              <form method='post' action='/athlete/meets'>
                <label>Meet Name<input name='meet_name' required></label>
                <label>Event<select name='event'><option>Shot Put</option><option>Discus</option><option>Javelin</option></select></label>
                <div class='row'><label>Feet<input type='number' name='feet' min='0' required></label><label>Inches<input type='number' name='inches' min='0' max='11' required></label></div>
                <button type='submit'>Save Meet Performance</button>
              </form>
            </div>
          </div>
        </div>
        """
        return html_page("Athlete Dashboard", body, user)

    def complete_assignment(self, user: sqlite3.Row, path: str) -> None:
        assignment_id = to_int(path.split("/")[3], 0)
        athlete = self.athlete_context(user)
        if athlete is None:
            return self.redirect("/athlete")
        with db_conn() as conn:
            conn.execute(
                "UPDATE assignments SET status='completed', completed_on=? WHERE id=? AND athlete_id=?",
                (date.today().isoformat(), assignment_id, athlete["id"]),
            )
        self.redirect("/athlete")

    def log_throw(self, user: sqlite3.Row) -> None:
        athlete = self.athlete_context(user)
        if athlete is None:
            return self.redirect("/athlete")
        form = self.read_form()
        feet = max(to_int(form.get("feet", "0"), 0), 0)
        inches = clamp(to_int(form.get("inches", "0"), 0), 0, 11)
        total_inches, meters = feet_inches_to_metrics(feet, inches)
        with db_conn() as conn:
            conn.execute(
                "INSERT INTO throw_logs (athlete_id,event,feet,inches,total_inches,meters,practice_date) VALUES (?,?,?,?,?,?,?)",
                (athlete["id"], form.get("event", "Shot Put"), feet, inches, total_inches, meters, date.today().isoformat()),
            )
            recalc_throw_pr(conn, athlete["id"], form.get("event", "Shot Put"))
        self.redirect("/athlete")

    def log_lift(self, user: sqlite3.Row) -> None:
        athlete = self.athlete_context(user)
        if athlete is None:
            return self.redirect("/athlete")
        form = self.read_form()
        weight = max(to_float(form.get("weight", "0"), 0.0), 0.0)
        reps = max(to_int(form.get("reps", "1"), 1), 1)
        # Epley-estimated one-rep max (approximation): weight * (1 + reps/30).
        # Most accurate around 1-10 reps and increasingly unreliable beyond that range.
        projected_max = round(weight * (1 + reps / 30), 1)
        lift_name = form.get("lift_name", "Lift").strip() or "Lift"
        with db_conn() as conn:
            conn.execute(
                "INSERT INTO lift_logs (athlete_id,lift_name,weight,reps,projected_max,practice_date) VALUES (?,?,?,?,?,?)",
                (athlete["id"], lift_name, weight, reps, projected_max, date.today().isoformat()),
            )
            recalc_lift_pr(conn, athlete["id"], lift_name)
        self.redirect("/athlete")

    def log_meet(self, user: sqlite3.Row) -> None:
        athlete = self.athlete_context(user)
        if athlete is None:
            return self.redirect("/athlete")
        form = self.read_form()
        feet = max(to_int(form.get("feet", "0"), 0), 0)
        inches = clamp(to_int(form.get("inches", "0"), 0), 0, 11)
        total_inches, meters = feet_inches_to_metrics(feet, inches)
        event = form.get("event", "Shot Put")
        with db_conn() as conn:
            conn.execute(
                "INSERT INTO meet_results (athlete_id,event,feet,inches,total_inches,meters,meet_name,meet_date) VALUES (?,?,?,?,?,?,?,?)",
                (
                    athlete["id"],
                    event,
                    feet,
                    inches,
                    total_inches,
                    meters,
                    form.get("meet_name", "Meet"),
                    date.today().isoformat(),
                ),
            )
            recalc_throw_pr(conn, athlete["id"], event)
        self.redirect("/athlete")

    def athlete_progress(self, user: sqlite3.Row) -> str:
        athlete = self.athlete_context(user)
        if athlete is None:
            return html_page("Progress", "<div class='card'>No athlete profile is linked to this account.</div>", user)

        with db_conn() as conn:
            prs = conn.execute(
                "SELECT category,event_or_lift,value_text,achieved_on FROM prs WHERE athlete_id=? ORDER BY category,event_or_lift",
                (athlete["id"],),
            ).fetchall()
            throws = conn.execute(
                "SELECT event, feet, inches, meters, practice_date FROM throw_logs WHERE athlete_id=? ORDER BY id DESC LIMIT 10",
                (athlete["id"],),
            ).fetchall()
            lifts = conn.execute(
                "SELECT lift_name, weight, reps, projected_max, practice_date FROM lift_logs WHERE athlete_id=? ORDER BY id DESC LIMIT 10",
                (athlete["id"],),
            ).fetchall()
            meets = conn.execute(
                "SELECT meet_name,event,feet,inches,meters,meet_date FROM meet_results WHERE athlete_id=? ORDER BY id DESC LIMIT 10",
                (athlete["id"],),
            ).fetchall()

        pr_tags = "".join(
            f"<span class='tag'>{esc(p['category'])}: {esc(p['event_or_lift'])} — {esc(p['value_text'])} ({esc(p['achieved_on'])})</span>"
            for p in prs
        ) or "<span class='muted'>No PRs yet.</span>"

        throw_rows = "".join(
            f"<tr><td>{esc(t['practice_date'])}</td><td>{esc(t['event'])}</td><td>{t['feet']}' {t['inches']}\"</td><td>{t['meters']}m</td></tr>"
            for t in throws
        ) or "<tr><td colspan='4' class='muted'>No throw logs yet.</td></tr>"

        lift_rows = "".join(
            f"<tr><td>{esc(l['practice_date'])}</td><td>{esc(l['lift_name'])}</td><td>{l['weight']}</td><td>{l['reps']}</td><td>{l['projected_max']}</td></tr>"
            for l in lifts
        ) or "<tr><td colspan='5' class='muted'>No lift logs yet.</td></tr>"

        meet_rows = "".join(
            f"<tr><td>{esc(m['meet_date'])}</td><td>{esc(m['meet_name'])}</td><td>{esc(m['event'])}</td><td>{m['feet']}' {m['inches']}\"</td><td>{m['meters']}m</td></tr>"
            for m in meets
        ) or "<tr><td colspan='5' class='muted'>No meet entries yet.</td></tr>"

        body = f"""
        <div class='card'><h3>Personal Records</h3>{pr_tags}</div>
        <div class='grid' style='margin-top:12px'>
          <div class='card'><h3>Recent Throws</h3><table><tr><th>Date</th><th>Event</th><th>Distance</th><th>Meters</th></tr>{throw_rows}</table></div>
          <div class='card'><h3>Recent Lifts</h3><table><tr><th>Date</th><th>Lift</th><th>Weight</th><th>Reps</th><th>Proj. Max</th></tr>{lift_rows}</table></div>
        </div>
        <div class='card' style='margin-top:12px'><h3>Meet Performances</h3><table><tr><th>Date</th><th>Meet</th><th>Event</th><th>Distance</th><th>Meters</th></tr>{meet_rows}</table></div>
        """
        return html_page("Athlete Progress", body, user)


def home_page(user: sqlite3.Row | None = None) -> str:
    with db_conn() as conn:
        about = esc(get_setting(conn, "about_text", "Bayou Bombers is a modern throws platform."))
        cover = esc(get_setting(conn, "home_cover_url", ""))
    if user:
        primary_cta_text = "Open Dashboard"
        primary_cta_url = dashboard_path(user)
        secondary_cta_text = "View Profile"
        secondary_cta_url = f"/profile/{user['id']}"
        welcome = f"<span class='eyebrow'>Welcome back, {esc(user['name'])}</span>"
    else:
        primary_cta_text = "Login / Register"
        primary_cta_url = "/login"
        secondary_cta_text = "Search Public Profiles"
        secondary_cta_url = "/search"
        welcome = "<span class='eyebrow'>Bayou Bombers Throws Club</span>"
    tmpl = read_template("home.html")
    if not tmpl:
        tmpl = """
        <section class='hero home-hero' style="--cover:url('{{cover_url}}')">
          <div class='hero-panel'>
            {{welcome_badge}}
            <h1>Bayou Bombers</h1>
            <p>{{about_text}}</p>
            <div class='inline'><a class='btn' href='{{primary_cta_url}}'>{{primary_cta_text}}</a><a class='btn secondary' href='{{secondary_cta_url}}'>{{secondary_cta_text}}</a></div>
          </div>
        </section>
        """
    body = (
        tmpl.replace("{{about_text}}", about)
        .replace("{{cover_url}}", cover)
        .replace("{{primary_cta_text}}", esc(primary_cta_text))
        .replace("{{primary_cta_url}}", esc(primary_cta_url))
        .replace("{{secondary_cta_text}}", esc(secondary_cta_text))
        .replace("{{secondary_cta_url}}", esc(secondary_cta_url))
        .replace("{{welcome_badge}}", welcome)
    )
    return html_page("Bayou Bombers", body)


def login_page(message: str | None = None) -> str:
    msg_html = f"<div class='notice card' style='margin-bottom:12px'>{esc(message)}</div>" if message else ""
    tmpl = read_template("login.html")
    if not tmpl:
        tmpl = """
        <div class='grid login-shell'>
          <div class='card'>
            <h2>Login</h2>
            <form method='post' action='/login'>
              <label>Email<input type='email' name='email' required></label>
              <label>Password<input type='password' name='password' required></label>
              <button type='submit'>Sign In</button>
            </form>
          </div>
          <div class='card'>
            <h3>About</h3>
            <p>Sign in with your email to manage training, profiles, and updates.</p>
            <p class='muted'>Use search to browse public coach and athlete profiles or head back home for the latest cover image and announcements.</p>
            <div class='inline'><a class='btn secondary' href='/search'>Search Public Profiles</a><a class='btn secondary' href='/'>Home</a><a class='btn secondary' href='/register'>Need an account? Register</a></div>
          </div>
        </div>
        """
    return html_page("Bayou Bombers Login", msg_html + tmpl)


def run_server(host: str, port: int) -> None:
    init_db()
    server = ThreadingHTTPServer((host, port), AppHandler)
    print(f"Bayou Bombers app running at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server...")
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Bayou Bombers MVP web app")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true", help="Initialize DB and exit")
    args = parser.parse_args()

    init_db()
    if args.check:
        print(f"Database ready at {DB_PATH}")
        return
    run_server(args.host, args.port)


if __name__ == "__main__":
    main()
