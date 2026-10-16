from functools import wraps
import hashlib
import hmac
from typing import Callable

from flask import Blueprint, current_app, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from app.db import cursor, fetch_all, fetch_one, get_connection

auth_bp = Blueprint("auth", __name__)


def roles_for_user(user_id: int) -> set[str]:
    rows = fetch_all(
        "SELECT r.role_name FROM roles r "
        "JOIN user_roles ur ON ur.role_id = r.role_id "
        "WHERE ur.user_id = %s",
        (user_id,),
    )
    return {row["role_name"] for row in rows}


def authenticate(username: str, password: str, remote_addr: str | None) -> tuple[int | None, bool, int | None]:
    normalized_username = username.strip().casefold()
    raw_key = f"{normalized_username}\0{remote_addr or 'unknown'}".encode("utf-8")
    attempt_key = hmac.new(current_app.secret_key.encode("utf-8"), raw_key, hashlib.sha256).hexdigest()
    lock_name = "hostel-login:" + attempt_key[:51]
    connection = get_connection()
    lock_acquired = False
    try:
        with cursor(dictionary=False) as db_cursor:
            db_cursor.execute("SELECT GET_LOCK(%s, 3)", (lock_name,))
            lock_acquired = db_cursor.fetchone()[0] == 1
            if not lock_acquired:
                return None, True, None

            db_cursor.execute(
                "SELECT COUNT(*) FROM login_attempts WHERE attempt_key=%s AND succeeded=FALSE "
                "AND attempted_at >= NOW() - INTERVAL 15 MINUTE",
                (attempt_key,),
            )
            if db_cursor.fetchone()[0] >= 5:
                return None, True, None

            db_cursor.execute(
                "SELECT user_id,password_hash,session_version FROM users "
                "WHERE (username=%s OR email=%s) AND is_active=TRUE",
                (username.strip(), username.strip().lower()),
            )
            user = db_cursor.fetchone()
            user_id = user[0] if user and check_password_hash(user[1], password) else None
            session_version = user[2] if user_id is not None else None
            db_cursor.execute(
                "INSERT INTO login_attempts (attempt_key,succeeded) VALUES (%s,%s)",
                (attempt_key, user_id is not None),
            )
        connection.commit()
        return user_id, False, session_version
    except Exception:
        connection.rollback()
        raise
    finally:
        if lock_acquired:
            with cursor(dictionary=False) as db_cursor:
                db_cursor.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))
                db_cursor.fetchone()


def login_required(view: Callable):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user_id = session.get("user_id")
        if not user_id:
            return redirect(url_for("auth.login", next=request.path))
        g.current_user = fetch_one(
            "SELECT user_id, username, email, session_version FROM users "
            "WHERE user_id = %s AND is_active = TRUE",
            (user_id,),
        )
        if g.current_user is None or session.get("session_version") != g.current_user["session_version"]:
            session.clear()
            return redirect(url_for("auth.login"))
        g.current_roles = roles_for_user(user_id)
        return view(*args, **kwargs)

    return wrapped


def roles_required(*allowed_roles: str):
    def decorator(view: Callable):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if not g.current_roles.intersection(allowed_roles):
                flash("You do not have permission to access that page.", "error")
                return redirect(url_for("dashboard.index"))
            return view(*args, **kwargs)

        return wrapped

    return decorator


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user_id, rate_limited, session_version = authenticate(username, password, request.remote_addr)
        if user_id is not None:
            session.clear()
            session["user_id"] = user_id
            session["session_version"] = session_version
            session.permanent = True
            destination = request.args.get("next")
            if destination and destination.startswith("/") and not destination.startswith("//"):
                return redirect(destination)
            return redirect(url_for("dashboard.index"))
        flash("Too many sign-in attempts. Try again in 15 minutes." if rate_limited else "Invalid username/email or password.", "error")
    return render_template("auth/login.html")


@auth_bp.post("/logout")
@login_required
def logout():
    session.clear()
    flash("You have been signed out.", "success")
    return redirect(url_for("auth.login"))