import os
from functools import wraps

import psycopg2
from flask import Flask, redirect, render_template, request, session, url_for
from psycopg2.extras import RealDictCursor

from rules import parse_invitees, weigh

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


@app.get("/sessions")
@login_required
def sessions():
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM tasting_sessions ORDER BY id DESC")
        items = cur.fetchall()
    return render_template(
        "sessions.html", sessions=items, can_write=session.get("role") == "writer"
    )


@app.post("/sessions")
@login_required
def create_session():
    if session.get("role") != "writer":
        return ("观察员不能建会", 403)
    name = request.form.get("name", "").strip()
    invitees = parse_invitees(request.form.get("invitees", ""))
    try:
        planned_pots = int(request.form.get("planned_pots", ""))
    except ValueError:
        planned_pots = 0
    if not name:
        return ("会名不能为空", 400)
    if planned_pots < 1:
        return ("计划壶数至少为 1", 400)
    if not invitees:
        return ("邀请名单不能为空", 400)
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """INSERT INTO tasting_sessions (name, planned_pots, created_by)
               VALUES (%s,%s,%s) RETURNING id""",
            (name, planned_pots, session["user"]),
        )
        session_id = cur.fetchone()["id"]
        cur.executemany(
            "INSERT INTO tasting_invitees (session_id, taster) VALUES (%s,%s)",
            [(session_id, t) for t in invitees],
        )
        conn.commit()
    return redirect(url_for("session_detail", session_id=session_id))


@app.get("/sessions/<int:session_id>")
@login_required
def session_detail(session_id):
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM tasting_sessions WHERE id = %s", (session_id,))
        item = cur.fetchone()
        if item is None:
            return ("试饮会不存在", 404)
        cur.execute(
            "SELECT taster FROM tasting_invitees WHERE session_id = %s ORDER BY taster",
            (session_id,),
        )
        invitees = [row["taster"] for row in cur.fetchall()]
        cur.execute(
            "SELECT * FROM tasting_cuppings WHERE session_id = %s ORDER BY id DESC",
            (session_id,),
        )
        rows = cur.fetchall()
    can_submit = session.get("role") == "writer" and session["user"] in invitees
    return render_template(
        "session_detail.html",
        item=item,
        invitees=invitees,
        rows=rows,
        can_submit=can_submit,
    )


@app.post("/sessions/<int:session_id>/cuppings")
@login_required
def create_session_cupping(session_id):
    if session.get("role") != "writer":
        return ("观察员不能交会内评", 403)
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM tasting_sessions WHERE id = %s", (session_id,))
        item = cur.fetchone()
        if item is None:
            return ("试饮会不存在", 404)
        cur.execute(
            "SELECT 1 FROM tasting_invitees WHERE session_id = %s AND taster = %s",
            (session_id, session["user"]),
        )
        if cur.fetchone() is None:
            return ("未受邀审评员不能向该试饮会交评", 403)
        aroma = float(request.form["aroma"])
        taste = float(request.form["taste"])
        liquor = float(request.form["liquor"])
        verdict, note, score = weigh(aroma, taste, liquor)
        cur.execute(
            "SELECT COUNT(*) AS n FROM tasting_cuppings WHERE session_id = %s",
            (session_id,),
        )
        used = cur.fetchone()["n"]
        if used >= item["planned_pots"]:
            return ("该试饮会计划壶数已满", 403)
        cur.execute(
            """INSERT INTO tasting_cuppings
                   (session_id, pot, aroma, taste, liquor, score, verdict, note, created_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (
                session_id,
                used + 1,
                aroma,
                taste,
                liquor,
                score,
                verdict,
                note,
                session["user"],
            ),
        )
        row = cur.fetchone()
        conn.commit()
    if request.headers.get("HX-Request"):
        return render_template("_session_row.html", row=row)
    return redirect(url_for("session_detail", session_id=session_id))
