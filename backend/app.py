import os
import re
from functools import wraps

import psycopg2
from flask import Flask, redirect, render_template, request, session, url_for
from psycopg2.extras import RealDictCursor

from rules import weigh

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "tea-cupping-dev-secret")

ACCOUNTS = {
    "taster": {"password": "tea123456", "role": "writer"},
    "taster2": {"password": "tea123456", "role": "writer"},
    "observer": {"password": "look123456", "role": "reader"},
}


def db():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def login_required(fn):
    @wraps(fn)
    def wrap(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        return fn(*args, **kwargs)

    return wrap


@app.get("/health")
def health():
    return {"status": "ok", "service": "tea-blend-cupping"}


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        name = request.form.get("username", "").strip()
        account = ACCOUNTS.get(name)
        if not account or account["password"] != request.form.get("password", ""):
            error = "用户名或密码错误"
        else:
            session["user"] = name
            session["role"] = account["role"]
            return redirect(url_for("home"))
    return render_template("login.html", error=error)


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
@login_required
def home():
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM cuppings ORDER BY id DESC")
        rows = cur.fetchall()
    return render_template("home.html", rows=rows, can_write=session.get("role") == "writer")


@app.post("/cuppings")
@login_required
def create():
    if session.get("role") != "writer":
        return ("仅审评员可提交拼配审评", 403)
    aroma = float(request.form["aroma"])
    taste = float(request.form["taste"])
    liquor = float(request.form["liquor"])
    lot = request.form["lot"].strip()
    verdict, note, score = weigh(aroma, taste, liquor)
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """INSERT INTO cuppings (lot, aroma, taste, liquor, score, verdict, note, created_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (lot, aroma, taste, liquor, score, verdict, note, session["user"]),
        )
        row = cur.fetchone()
        conn.commit()
    if request.headers.get("HX-Request"):
        return render_template("_row.html", row=row)
    return redirect(url_for("home"))


def parse_invites(raw):
    return [n for n in re.split(r"[，,、;；\s]+", raw) if n]


@app.get("/sessions")
@login_required
def sessions_index():
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """SELECT s.*,
                      (SELECT COUNT(*) FROM session_cuppings c
                        WHERE c.session_id = s.id) AS submitted
                 FROM tasting_sessions s ORDER BY s.id DESC"""
        )
        sessions = cur.fetchall()
    return render_template(
        "sessions.html", sessions=sessions, can_write=session.get("role") == "writer"
    )


@app.post("/sessions")
@login_required
def session_create():
    if session.get("role") != "writer":
        return ("仅审评员可建试饮会", 403)
    name = request.form.get("name", "").strip()
    try:
        planned_pots = int(request.form.get("planned_pots", ""))
    except ValueError:
        planned_pots = 0
    invites = parse_invites(request.form.get("invites", ""))
    if not name or planned_pots < 1 or not invites:
        return ("会名、计划壶数（正整数）、邀请审评员名单均必填", 400)
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """INSERT INTO tasting_sessions (name, planned_pots, created_by)
               VALUES (%s,%s,%s) RETURNING id""",
            (name, planned_pots, session["user"]),
        )
        sid = cur.fetchone()["id"]
        cur.executemany(
            """INSERT INTO session_invites (session_id, taster)
               VALUES (%s,%s) ON CONFLICT DO NOTHING""",
            [(sid, taster) for taster in invites],
        )
        conn.commit()
    return redirect(url_for("session_detail", sid=sid))


@app.get("/sessions/<int:sid>")
@login_required
def session_detail(sid):
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM tasting_sessions WHERE id = %s", (sid,))
        tasting = cur.fetchone()
        if not tasting:
            return ("试饮会不存在", 404)
        cur.execute(
            "SELECT taster FROM session_invites WHERE session_id = %s ORDER BY taster",
            (sid,),
        )
        invites = [r["taster"] for r in cur.fetchall()]
        cur.execute(
            "SELECT * FROM session_cuppings WHERE session_id = %s ORDER BY id DESC",
            (sid,),
        )
        rows = cur.fetchall()
    can_write = session.get("role") == "writer" and session["user"] in invites
    return render_template(
        "session_detail.html",
        tasting=tasting,
        invites=invites,
        rows=rows,
        can_write=can_write,
    )


@app.post("/sessions/<int:sid>/cuppings")
@login_required
def session_cupping_create(sid):
    if session.get("role") != "writer":
        return ("仅审评员可提交会内审评", 403)
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM tasting_sessions WHERE id = %s", (sid,))
        if not cur.fetchone():
            return ("试饮会不存在", 404)
        cur.execute(
            "SELECT 1 FROM session_invites WHERE session_id = %s AND taster = %s",
            (sid, session["user"]),
        )
        if not cur.fetchone():
            return ("未受邀，不能向该试饮会提交审评", 403)
        aroma = float(request.form["aroma"])
        taste = float(request.form["taste"])
        liquor = float(request.form["liquor"])
        lot = request.form["lot"].strip()
        verdict, note, score = weigh(aroma, taste, liquor)
        cur.execute(
            """INSERT INTO session_cuppings
                   (session_id, lot, aroma, taste, liquor, score, verdict, note, created_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (sid, lot, aroma, taste, liquor, score, verdict, note, session["user"]),
        )
        row = cur.fetchone()
        conn.commit()
    if request.headers.get("HX-Request"):
        return render_template("_session_row.html", row=row)
    return redirect(url_for("session_detail", sid=sid))
