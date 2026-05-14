#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import html
import hmac
import hashlib
import secrets
import sqlite3
from datetime import date, datetime, timedelta, timezone
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "bayoubombers.db"
SESSION_COOKIE = "bayou_session"
SESSIONS: dict[str, dict[str, str]] = {}


def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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
            """
        )

        coach = conn.execute("SELECT id FROM users WHERE username='coach'").fetchone()
        if coach is None:
            conn.execute(
                "INSERT INTO users (username, password, role, name) VALUES (?,?,?,?)",
                ("coach", hash_password("coach123"), "coach", "Head Coach"),
            )
        admin = conn.execute("SELECT id FROM users WHERE username='admin@admin.com'").fetchone()
        if admin is None:
            conn.execute(
                "INSERT INTO users (username, password, role, name) VALUES (?,?,?,?)",
                ("admin@admin.com", hash_password("password123"), "coach", "Admin Coach"),
            )

        athlete_user = conn.execute("SELECT id FROM users WHERE username='athlete' ").fetchone()
        if athlete_user is None:
            conn.execute(
                "INSERT INTO users (username, password, role, name) VALUES (?,?,?,?)",
                ("athlete", hash_password("athlete123"), "athlete", "Sample Athlete"),
            )
            athlete_user = conn.execute("SELECT id FROM users WHERE username='athlete'").fetchone()

        existing_athlete = conn.execute("SELECT id FROM athletes WHERE user_id=?", (athlete_user["id"],)).fetchone()
        if existing_athlete is None:
            conn.execute(
                "INSERT INTO athletes (user_id, sex, events, group_name) VALUES (?,?,?,?)",
                (athlete_user["id"], "Male", "Shot Put,Discus", "Varsity Throws"),
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


def html_page(title: str, body: str, user: sqlite3.Row | None = None) -> str:
    nav = ""
    if user:
        if user["role"] == "coach":
            nav = (
                "<a href='/coach'>Dashboard</a><a href='/coach/athletes'>Athletes</a>"
                "<a href='/coach/modules'>Modules</a><a href='/coach/plans'>Plans</a>"
                "<a href='/coach/reports'>Reports</a><a href='/logout'>Logout</a>"
            )
        else:
            nav = (
                "<a href='/athlete'>Today</a><a href='/athlete/progress'>Progress</a>"
                "<a href='/logout'>Logout</a>"
            )
    return f"""<!doctype html>
<html>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <title>{esc(title)}</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 0; background:#f5f7fa; color:#1f2937; }}
    header {{ background:#0f172a; color:#fff; padding:16px; }}
    nav a {{ color:#bfdbfe; margin-right:12px; text-decoration:none; font-weight:600; }}
    main {{ padding:16px; max-width:1100px; margin:0 auto; }}
    .grid {{ display:grid; gap:12px; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); }}
    .card {{ background:#fff; border-radius:12px; padding:14px; box-shadow:0 2px 8px rgba(15,23,42,.08); }}
    .muted {{ color:#6b7280; font-size:.92rem; }}
    table {{ width:100%; border-collapse:collapse; font-size:.95rem; }}
    th,td {{ border-bottom:1px solid #e5e7eb; padding:8px; text-align:left; }}
    input,select,button,textarea {{ width:100%; padding:8px; border:1px solid #cbd5e1; border-radius:8px; margin-top:4px; }}
    button {{ background:#2563eb; color:#fff; font-weight:700; cursor:pointer; }}
    .row {{ display:grid; grid-template-columns:1fr 1fr; gap:10px; }}
    .tag {{ display:inline-block; background:#e2e8f0; border-radius:999px; padding:4px 10px; margin:2px; font-size:.85rem; }}
  </style>
</head>
<body>
  <header><strong>Bayou Bombers Throws App</strong><div style='margin-top:8px'>{nav}</div></header>
  <main>{body}</main>
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
        user = self.current_user()

        if path == "/":
            if user is None:
                return self.respond_html(login_page())
            return self.redirect("/coach" if user["role"] == "coach" else "/athlete")

        if path == "/login" and method == "POST":
            form = self.read_form()
            return self.handle_login(form)

        if path == "/logout":
            return self.handle_logout()

        if user is None:
            return self.respond_html(login_page("Please log in first."), status=401)

        if path.startswith("/coach") and user["role"] != "coach":
            return self.respond_html(html_page("Unauthorized", "<div class='card'>Coach access only.</div>", user), status=403)

        if path.startswith("/athlete") and user["role"] != "athlete":
            return self.respond_html(html_page("Unauthorized", "<div class='card'>Athlete access only.</div>", user), status=403)

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

        if path == "/coach/assign" and method == "POST":
            return self.assign_plan(user)

        if path == "/coach/reports":
            return self.respond_html(self.coach_reports(user))

        if path == "/athlete":
            return self.respond_html(self.athlete_dashboard(user))

        if path == "/athlete/progress":
            return self.respond_html(self.athlete_progress(user))

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
        cookie_header = self.headers.get("Cookie", "")
        jar = cookies.SimpleCookie()
        jar.load(cookie_header)
        token = jar.get(SESSION_COOKIE)
        if token is None:
            return None
        session = SESSIONS.get(token.value)
        if not session:
            return None
        with db_conn() as conn:
            return conn.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()

    def read_form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8")
        parsed = parse_qs(raw)
        return {k: v[0] for k, v in parsed.items()}

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

    def session_cookie_flags(self) -> str:
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        secure = "" if host in {"127.0.0.1", "localhost"} else "; Secure"
        return f"HttpOnly; Path=/; SameSite=Lax{secure}"

    def handle_login(self, form: dict[str, str]) -> None:
        username = form.get("username", "").strip()
        password = form.get("password", "")
        with db_conn() as conn:
            user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if user is None or not verify_password(password, user["password"]):
            return self.respond_html(login_page("Invalid username or password."), status=401)
        token = secrets.token_urlsafe(24)
        SESSIONS[token] = {"user_id": str(user["id"]), "created": datetime.now(timezone.utc).isoformat()}
        cookie = f"{SESSION_COOKIE}={token}; {self.session_cookie_flags()}"
        self.redirect("/coach" if user["role"] == "coach" else "/athlete", cookie)

    def handle_logout(self) -> None:
        cookie_header = self.headers.get("Cookie", "")
        jar = cookies.SimpleCookie()
        jar.load(cookie_header)
        token = jar.get(SESSION_COOKIE)
        if token:
            SESSIONS.pop(token.value, None)
        expired = f"{SESSION_COOKIE}=deleted; Path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT"
        self.redirect("/", expired)

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

    def coach_athletes(self, user: sqlite3.Row) -> str:
        with db_conn() as conn:
            athletes = conn.execute(
                """
                SELECT a.id, COALESCE(u.name,'Unlinked') AS name, COALESCE(u.username,'(none)') AS username,
                       a.sex, a.events, COALESCE(a.group_name,'') AS group_name
                FROM athletes a
                LEFT JOIN users u ON u.id=a.user_id
                ORDER BY name
                """
            ).fetchall()

        rows = "".join(
            f"<tr><td>{r['id']}</td><td>{esc(r['name'])}</td><td>{esc(r['username'])}</td>"
            f"<td>{esc(r['sex'])}</td><td>{esc(r['events'])}</td><td>{esc(r['group_name'])}</td></tr>"
            for r in athletes
        ) or "<tr><td colspan='6' class='muted'>No athletes yet.</td></tr>"

        body = f"""
        <div class='grid'>
          <div class='card'>
            <h3>Add Athlete</h3>
            <form method='post' action='/coach/athletes'>
              <label>Name<input name='name' required></label>
              <label>Username<input name='username' required></label>
              <label>Password<input type='password' name='password' required></label>
              <div class='row'>
                <label>Sex<select name='sex'><option>Male</option><option>Female</option></select></label>
                <label>Group<input name='group_name' placeholder='Varsity Throws'></label>
              </div>
              <label>Events (comma-separated)<input name='events' value='Shot Put,Discus'></label>
              <button type='submit'>Create Athlete</button>
            </form>
          </div>
          <div class='card'>
            <h3>Athlete Roster</h3>
            <table>
              <tr><th>ID</th><th>Name</th><th>Username</th><th>Sex</th><th>Events</th><th>Group</th></tr>
              {rows}
            </table>
          </div>
        </div>
        """
        return html_page("Athlete Setup", body, user)

    def add_athlete(self, user: sqlite3.Row) -> None:
        form = self.read_form()
        username = form.get("username", "").strip()
        with db_conn() as conn:
            exists = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
            if exists:
                return self.respond_html(self.coach_athletes(user).replace("<main>", "<main><div class='card'>Username already exists.</div>", 1), status=400)
            conn.execute(
                "INSERT INTO users (username,password,role,name) VALUES (?,?,?,?)",
                (
                    username,
                    hash_password(form.get("password", "athlete123")),
                    "athlete",
                    form.get("name", "Athlete"),
                ),
            )
            user_id = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()["id"]
            conn.execute(
                "INSERT INTO athletes (user_id,sex,events,group_name) VALUES (?,?,?,?)",
                (user_id, form.get("sex", "Male"), form.get("events", "Shot Put"), form.get("group_name", "")),
            )
        self.redirect("/coach/athletes")

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
            plans = conn.execute("SELECT * FROM practice_plans ORDER BY practice_date DESC, id DESC").fetchall()
            athletes = conn.execute(
                "SELECT a.id, COALESCE(u.name, 'Athlete #' || a.id) AS athlete_name FROM athletes a LEFT JOIN users u ON u.id=a.user_id ORDER BY athlete_name"
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
                plan_cards.append(
                    f"<div class='card'><h4>{esc(p['title'])}</h4><div class='muted'>{esc(p['practice_date'])} | {assigned_count} assigned</div><ul>{item_list}</ul>"
                    f"<form method='post' action='/coach/assign'><input type='hidden' name='plan_id' value='{p['id']}'>"
                    "<label>Assign to Athletes (comma-separated athlete IDs)"
                    "<input name='athlete_ids' placeholder='1,2,3'></label><button type='submit'>Assign Plan</button></form></div>"
                )

        module_options = "".join(
            f"<option value='{m['id']}'>{esc(m['name'])} ({esc(m['category'])})</option>" for m in modules
        )
        athlete_help = ", ".join(f"{a['id']}={esc(a['athlete_name'])}" for a in athletes) or "No athletes found"

        body = f"""
        <div class='grid'>
          <div class='card'>
            <h3>Create Practice Plan</h3>
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
            <p class='muted'>Athlete IDs: {athlete_help}</p>
          </div>
          <div>
            {''.join(plan_cards) if plan_cards else "<div class='card'>No plans yet.</div>"}
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
        if not athlete_ids:
            self.redirect("/coach/plans")
            return
        with db_conn() as conn:
            for athlete_id in athlete_ids:
                if athlete_id < 1:
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

        body = f"""
        <div class='card'>
          <h3>Welcome {esc(user['name'])}</h3>
          <p class='muted'>Events: {esc(athlete['events'])} | Group: {esc(athlete['group_name'] or 'N/A')}</p>
        </div>
        <div class='grid' style='margin-top:12px'>
          <div>
            {''.join(assignment_cards) if assignment_cards else "<div class='card'>No assignments yet.</div>"}
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
        # Epley-estimated one-rep max: weight * (1 + reps/30).
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


def login_page(message: str | None = None) -> str:
    msg_html = f"<div class='card' style='margin-bottom:12px'>{esc(message)}</div>" if message else ""
    body = f"""
    {msg_html}
    <div class='grid'>
      <div class='card'>
        <h2>Login</h2>
        <form method='post' action='/login'>
          <label>Username<input name='username' required></label>
          <label>Password<input type='password' name='password' required></label>
          <button type='submit'>Sign In</button>
        </form>
      </div>
      <div class='card'>
        <h3>Demo Accounts</h3>
        <p><strong>Admin:</strong> admin@admin.com / password123</p>
        <p><strong>Coach:</strong> coach / coach123</p>
        <p><strong>Athlete:</strong> athlete / athlete123</p>
        <p class='muted'>The app is preloaded with athlete setup, modules, and production-ready MVP data tables.</p>
      </div>
    </div>
    """
    return html_page("Bayou Bombers Login", body)


def run_server(host: str, port: int) -> None:
    init_db()
    server = ThreadingHTTPServer((host, port), AppHandler)
    print(f"Bayou Bombers app running at http://{host}:{port}")
    print("Default admin login: admin@admin.com / password123")
    print("Demo coach login: coach / coach123")
    print("Demo athlete login: athlete / athlete123")
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
