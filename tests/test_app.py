import re

from app import create_app


def test_health_endpoint_does_not_require_database():
    client = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False}).test_client()

    response = client.get("/health")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_readiness_endpoint_reports_database_status(monkeypatch):
    import app as app_module

    monkeypatch.setattr(app_module, "ping_database", lambda: True)
    client = create_app({"TESTING": True}).test_client()

    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ready", "database": True}


def test_readiness_endpoint_reports_database_unavailable(monkeypatch):
    import app as app_module

    monkeypatch.setattr(app_module, "ping_database", lambda: False)
    client = create_app({"TESTING": True}).test_client()

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.get_json() == {"status": "unavailable", "database": False}


def test_login_page_renders_and_provides_csrf_token():
    client = create_app({"TESTING": True}).test_client()

    response = client.get("/login")

    assert response.status_code == 200
    assert b'name="csrf_token"' in response.data
    assert b"Sign in" in response.data


def test_login_post_without_csrf_token_is_rejected():
    client = create_app({"TESTING": True}).test_client()

    response = client.post("/login", data={"username": "student", "password": "not-a-password"})

    assert response.status_code == 400


def test_protected_students_page_redirects_to_login():
    client = create_app({"TESTING": True}).test_client()

    response = client.get("/students")

    assert response.status_code == 302
    assert response.headers["Location"].startswith("/login")


def test_login_uses_password_hash_and_sets_session(monkeypatch):
    from app import auth

    monkeypatch.setattr(
        auth,
        "authenticate",
        lambda _username, _password, _remote_addr: (17, False, 3),
    )
    client = create_app({"TESTING": True}).test_client()
    login_page = client.get("/login")
    token = re.search(rb'<input[^>]*name="csrf_token"[^>]*value="([^"]+)"', login_page.data)
    assert token is not None

    response = client.post(
        "/login?next=/health",
        data={"csrf_token": token.group(1).decode(), "username": "manager", "password": "correct horse battery"},
    )

    assert response.status_code == 302
    assert response.headers["Location"] == "/health"
    with client.session_transaction() as session:
        assert session["user_id"] == 17
        assert session["session_version"] == 3


def test_login_displays_rate_limit_message(monkeypatch):
    from app import auth

    monkeypatch.setattr(auth, "authenticate", lambda _username, _password, _remote_addr: (None, True, None))
    client = create_app({"TESTING": True}).test_client()
    login_page = client.get("/login")
    token = re.search(rb'<input[^>]*name="csrf_token"[^>]*value="([^"]+)"', login_page.data)
    assert token is not None

    response = client.post(
        "/login",
        data={"csrf_token": token.group(1).decode(), "username": "manager", "password": "incorrect"},
    )

    assert response.status_code == 200
    assert b"Too many sign-in attempts" in response.data


def test_mess_manager_cannot_view_student_operations(monkeypatch):
    from app import auth, routes

    monkeypatch.setattr(auth, "fetch_one", lambda _sql, _params: {"user_id": 4, "username": "mess", "email": "mess@example.test", "session_version": 0})
    monkeypatch.setattr(auth, "roles_for_user", lambda _user_id: {"Mess Manager"})
    monkeypatch.setattr(routes, "fetch_all", lambda _sql, _params=(): [])
    client = create_app({"TESTING": True}).test_client()
    with client.session_transaction() as session:
        session["user_id"] = 4
        session["session_version"] = 0

    response = client.get("/operations")

    assert response.status_code == 200
    assert b"ATTENDANCE" not in response.data
    assert b"VISITORS" not in response.data
    assert b"Menus and inventory" in response.data

    student_list = client.get("/students")
    assert student_list.status_code == 302

    token = re.search(rb'<input[^>]*name="csrf_token"[^>]*value="([^"]+)"', response.data)
    assert token is not None
    leave_response = client.post(
        "/operations/leave",
        data={"csrf_token": token.group(1).decode(), "student_id": "1", "start_date": "2030-01-01", "end_date": "2030-01-02", "reason": "test"},
    )
    complaint_response = client.post(
        "/operations/complaints",
        data={"csrf_token": token.group(1).decode(), "student_id": "1", "category": "test", "description": "test"},
    )
    assert leave_response.status_code == 302
    assert complaint_response.status_code == 302


def test_accountant_cannot_open_hostel_setup(monkeypatch):
    from app import auth

    monkeypatch.setattr(auth, "fetch_one", lambda _sql, _params: {"user_id": 5, "username": "accounts", "email": "accounts@example.test", "session_version": 0})
    monkeypatch.setattr(auth, "roles_for_user", lambda _user_id: {"Accountant"})
    client = create_app({"TESTING": True}).test_client()
    with client.session_transaction() as session:
        session["user_id"] = 5
        session["session_version"] = 0

    response = client.get("/hostels")

    assert response.status_code == 302
    assert response.headers["Location"] == "/"


def test_non_super_admin_cannot_open_user_management(monkeypatch):
    from app import auth

    monkeypatch.setattr(auth, "fetch_one", lambda _sql, _params: {"user_id": 6, "username": "warden", "email": "warden@example.test", "session_version": 0})
    monkeypatch.setattr(auth, "roles_for_user", lambda _user_id: {"Warden"})
    client = create_app({"TESTING": True}).test_client()
    with client.session_transaction() as session:
        session["user_id"] = 6
        session["session_version"] = 0

    response = client.get("/users")

    assert response.status_code == 302
    assert response.headers["Location"] == "/"


def test_stale_session_version_is_logged_out(monkeypatch):
    from app import auth

    monkeypatch.setattr(auth, "fetch_one", lambda _sql, _params: {"user_id": 7, "username": "reset", "email": "reset@example.test", "session_version": 2})
    client = create_app({"TESTING": True}).test_client()
    with client.session_transaction() as session:
        session["user_id"] = 7
        session["session_version"] = 1

    response = client.get("/students")

    assert response.status_code == 302
    assert response.headers["Location"].startswith("/login")
    with client.session_transaction() as session:
        assert "user_id" not in session


def test_all_templates_compile():
    app = create_app({"TESTING": True})

    with app.app_context():
        for template_name in (
            "base.html",
            "auth/login.html",
            "dashboard/index.html",
            "dashboard/student.html",
            "records/students.html",
            "records/hostels.html",
            "records/allocations.html",
            "records/finance.html",
            "records/operations.html",
            "records/staff.html",
            "records/users.html",
            "records/receipt.html",
            "records/notifications.html",
            "records/audit.html",
            "auth/change_password.html",
        ):
            app.jinja_env.get_template(template_name)


def test_csv_export_neutralizes_spreadsheet_formulas():
    from app.routes import csv_safe_rows

    rows = csv_safe_rows([
        {"name": "=SUM(A1:A2)", "phone": "+15550000000", "safe": "Hostel block"},
        {"name": "@cmd", "phone": "-2", "safe": "ordinary"},
    ])

    assert rows == [
        {"name": "'=SUM(A1:A2)", "phone": "'+15550000000", "safe": "Hostel block"},
        {"name": "'@cmd", "phone": "'-2", "safe": "ordinary"},
    ]