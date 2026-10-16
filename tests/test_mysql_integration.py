import os
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from app import create_app
from app.db import cursor, get_connection
from app.services.finance import create_fee_structure, create_invoice, record_payment
from app.auth import authenticate
from app.services.hostel import RuleViolation, check_in, checkout, create_reservation
from app.services.operations import adjust_inventory
from werkzeug.security import check_password_hash, generate_password_hash


@pytest.fixture
def mysql_app():
    database = os.getenv("HOSTEL_TEST_DATABASE")
    if not database:
        pytest.skip("Set HOSTEL_TEST_DATABASE to an already-migrated disposable database to run MySQL integration tests.")
    config = {
        "host": os.getenv("HOSTEL_TEST_HOST", "127.0.0.1"),
        "port": int(os.getenv("HOSTEL_TEST_PORT", "3306")),
        "database": database,
        "user": os.getenv("HOSTEL_TEST_USER", "root"),
        "password": os.getenv("HOSTEL_TEST_PASSWORD", ""),
        "pool_size": 4,
        "autocommit": False,
        "use_unicode": True,
    }
    socket_path = os.getenv("HOSTEL_TEST_UNIX_SOCKET")
    if socket_path:
        config.pop("host")
        config["unix_socket"] = socket_path
    yield create_app({"TESTING": True, "DB_CONFIG": config})


def test_mysql_booking_and_checkout_rules(mysql_app):
    suffix = uuid.uuid4().hex[:10]
    with mysql_app.app_context():
        with cursor(dictionary=False) as db_cursor:
            db_cursor.execute("INSERT INTO hostel_blocks (block_name) VALUES (%s)", ("TEST-" + suffix,))
            block_id = db_cursor.lastrowid
            db_cursor.execute("INSERT INTO rooms (block_id,room_number) VALUES (%s,%s)", (block_id, "1"))
            room_id = db_cursor.lastrowid
            db_cursor.execute("INSERT INTO beds (room_id,bed_label) VALUES (%s,'A'),(%s,'B')", (room_id, room_id))
            db_cursor.execute(
                "INSERT INTO students (student_number,first_name,last_name,email,admission_date) "
                "VALUES (%s,'Test','One',%s,CURRENT_DATE),(%s,'Test','Two',%s,CURRENT_DATE)",
                ("T-" + suffix + "-1", suffix + "1@example.test", "T-" + suffix + "-2", suffix + "2@example.test"),
            )
            db_cursor.execute("SELECT student_id FROM students WHERE student_number IN (%s,%s) ORDER BY student_number", ("T-" + suffix + "-1", "T-" + suffix + "-2"))
            student_one, student_two = [row[0] for row in db_cursor.fetchall()]
            db_cursor.execute("SELECT bed_id FROM beds WHERE room_id=%s ORDER BY bed_label", (room_id,))
            bed_one, bed_two = [row[0] for row in db_cursor.fetchall()]
        get_connection().commit()

        start = date(2030, 1, 1)
        end = date(2030, 1, 10)
        audit_baseline = mysql_scalar("SELECT COUNT(*) FROM audit_logs WHERE action='allocation.reserved'", ())
        first_allocation = create_reservation(student_one, bed_one, start, end, None)
        assert mysql_scalar("SELECT COUNT(*) FROM audit_logs WHERE action='allocation.reserved' AND entity_id=%s", (first_allocation,)) == 1
        with pytest.raises(RuleViolation, match="overlapping"):
            create_reservation(student_two, bed_one, date(2030, 1, 9), date(2030, 1, 12), None)
        assert mysql_scalar("SELECT COUNT(*) FROM audit_logs WHERE action='allocation.reserved'", ()) == audit_baseline + 1

        # Half-open periods may touch at an endpoint without overlapping.
        create_reservation(student_two, bed_one, end, date(2030, 1, 15), None)
        with pytest.raises(RuleViolation, match="overlapping"):
            create_reservation(student_one, bed_two, date(2030, 1, 2), date(2030, 1, 3), None)

        today = date.today()
        with cursor(dictionary=False) as db_cursor:
            db_cursor.execute(
                "INSERT INTO allocations (student_id,bed_id,planned_start_date,planned_end_date,checked_in_at,status) "
                "VALUES (%s,%s,%s,%s,%s,'checked_in')",
                (student_one, bed_two, today - timedelta(days=5), today - timedelta(days=2), datetime.now() - timedelta(days=5)),
            )
            physical_allocation = db_cursor.lastrowid
        get_connection().commit()
        reservation_id = create_reservation(student_two, bed_two, today, today + timedelta(days=2), None)
        with pytest.raises(RuleViolation, match="physically occupied"):
            check_in(reservation_id)

        with cursor(dictionary=False) as db_cursor:
            db_cursor.execute("INSERT INTO fee_categories (category_name) VALUES (%s)", ("Test fee " + suffix,))
            category_id = db_cursor.lastrowid
            db_cursor.execute("INSERT INTO inventory_items (item_name,unit_name,reorder_level) VALUES (%s,'unit',1)", ("Test stock " + suffix,))
            item_id = db_cursor.lastrowid
        get_connection().commit()
        fee_structure_id = create_fee_structure(category_id, Decimal("100.00"), "monthly", today, None)
        invoice_id = create_invoice(student_one, "TEST-INV-" + suffix, today, today, fee_structure_id, "Hostel accommodation", None, None)

        assert checkout(physical_allocation, None) is False
        assert mysql_scalar("SELECT status='checked_in' FROM allocations WHERE allocation_id=%s", (physical_allocation,)) == 1
        assert mysql_scalar("SELECT COUNT(*) FROM checkout_clearances WHERE allocation_id=%s AND decision='blocked'", (physical_allocation,)) == 1

        record_payment(student_one, invoice_id, "TEST-RCT-" + suffix, Decimal("100.00"), "cash", None, None)

        assert checkout(physical_allocation, None) is True
        assert mysql_scalar("SELECT status='completed' AND checked_out_at IS NOT NULL FROM allocations WHERE allocation_id=%s", (physical_allocation,)) == 1
        assert mysql_scalar("SELECT COUNT(*) FROM checkout_clearances WHERE allocation_id=%s", (physical_allocation,)) == 2

        adjust_inventory(item_id, Decimal("5.00"), "Test receipt", None)
        adjust_inventory(item_id, Decimal("-3.00"), "Test issue", None)
        with pytest.raises(RuleViolation, match="negative"):
            adjust_inventory(item_id, Decimal("-3.00"), "Over-issue test", None)
        assert mysql_scalar("SELECT COALESCE(SUM(quantity_change),0) FROM inventory_transactions WHERE inventory_item_id=%s", (item_id,)) == Decimal("2.00")


def test_mysql_concurrent_reservations_serialize_on_bed(mysql_app):
    suffix = uuid.uuid4().hex[:10]
    with mysql_app.app_context():
        with cursor(dictionary=False) as db_cursor:
            db_cursor.execute("INSERT INTO hostel_blocks (block_name) VALUES (%s)", ("RACE-" + suffix,))
            block_id = db_cursor.lastrowid
            db_cursor.execute("INSERT INTO rooms (block_id,room_number) VALUES (%s,'1')", (block_id,))
            room_id = db_cursor.lastrowid
            db_cursor.execute("INSERT INTO beds (room_id,bed_label) VALUES (%s,'A')", (room_id,))
            bed_id = db_cursor.lastrowid
            db_cursor.execute(
                "INSERT INTO students (student_number,first_name,last_name,email,admission_date) "
                "VALUES (%s,'Race','One',%s,CURRENT_DATE),(%s,'Race','Two',%s,CURRENT_DATE)",
                ("R-" + suffix + "-1", suffix + "a@example.test", "R-" + suffix + "-2", suffix + "b@example.test"),
            )
            db_cursor.execute("SELECT student_id FROM students WHERE student_number IN (%s,%s) ORDER BY student_number", ("R-" + suffix + "-1", "R-" + suffix + "-2"))
            student_ids = [row[0] for row in db_cursor.fetchall()]
        get_connection().commit()

    def attempt(student_id):
        with mysql_app.app_context():
            try:
                create_reservation(student_id, bed_id, date(2040, 1, 1), date(2040, 1, 5), None)
                return "reserved"
            except RuleViolation:
                return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(attempt, student_ids))
    assert sorted(outcomes) == ["conflict", "reserved"]


def test_mysql_staff_pages_and_reports_render(mysql_app):
    suffix = uuid.uuid4().hex[:10]
    with mysql_app.app_context():
        with cursor(dictionary=False) as db_cursor:
            db_cursor.execute(
                "INSERT INTO users (username,email,password_hash) VALUES (%s,%s,'test-only-hash')",
                ("page-test-" + suffix, suffix + "@example.test"),
            )
            user_id = db_cursor.lastrowid
            db_cursor.execute("SELECT role_id FROM roles WHERE role_name='Super Admin'")
            role_id = db_cursor.fetchone()[0]
            db_cursor.execute("INSERT INTO user_roles (user_id,role_id) VALUES (%s,%s)", (user_id, role_id))
        get_connection().commit()

    client = mysql_app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = user_id
        session["session_version"] = 0
    for path in (
        "/",
        "/students",
        "/hostels",
        "/allocations",
        "/finance",
        "/operations",
        "/staff",
        "/notifications",
        "/audit",
        "/reports/occupancy.csv",
        "/reports/finance.csv",
    ):
        response = client.get(path)
        assert response.status_code == 200, f"{path} returned {response.status_code}: {response.data[:500]!r}"


def test_mysql_login_throttles_repeated_failures(mysql_app):
    suffix = uuid.uuid4().hex[:10]
    username = "throttle-" + suffix
    email = suffix + "@example.test"
    with mysql_app.app_context():
        with cursor(dictionary=False) as db_cursor:
            db_cursor.execute(
                "INSERT INTO users (username,email,password_hash) VALUES (%s,%s,'not-used-in-this-test')",
                (username, email),
            )
        get_connection().commit()

        for _ in range(5):
            user_id, limited, _session_version = authenticate(username, "wrong-password", "192.0.2.10")
            assert user_id is None
            assert limited is False

        user_id, limited, _session_version = authenticate(username, "wrong-password", "192.0.2.10")
        assert user_id is None
        assert limited is True


def test_mysql_login_authenticates_password_hash(mysql_app):
    suffix = uuid.uuid4().hex[:10]
    username = "login-" + suffix
    password = "integration-test-password"
    with mysql_app.app_context():
        with cursor(dictionary=False) as db_cursor:
            db_cursor.execute(
                "INSERT INTO users (username,email,password_hash) VALUES (%s,%s,%s)",
                (username, suffix + "@example.test", generate_password_hash(password)),
            )
            user_id = db_cursor.lastrowid
        get_connection().commit()

        authenticated_id, rate_limited, session_version = authenticate(username, password, "192.0.2.11")

        assert authenticated_id == user_id
        assert rate_limited is False
        assert session_version == 0
        assert mysql_scalar(
            "SELECT COUNT(*) FROM login_attempts WHERE succeeded=TRUE AND attempt_key IS NOT NULL",
            (),
        ) >= 1


def test_mysql_work_orders_can_be_assigned_and_reassigned(mysql_app):
    suffix = uuid.uuid4().hex[:10]
    with mysql_app.app_context():
        with cursor(dictionary=False) as db_cursor:
            db_cursor.execute("INSERT INTO users (username,email,password_hash) VALUES (%s,%s,'test')", ("assign-" + suffix, suffix + "@example.test"))
            user_id = db_cursor.lastrowid
            db_cursor.execute("SELECT role_id FROM roles WHERE role_name='Super Admin'")
            db_cursor.execute("INSERT INTO user_roles (user_id,role_id) VALUES (%s,%s)", (user_id, db_cursor.fetchone()[0]))
            db_cursor.execute("INSERT INTO staff (staff_number,first_name,last_name,job_title) VALUES (%s,'Work','One','Technician'),(%s,'Work','Two','Technician')", ("WS-" + suffix + "-1", "WS-" + suffix + "-2"))
            db_cursor.execute("SELECT staff_id FROM staff WHERE staff_number IN (%s,%s) ORDER BY staff_number", ("WS-" + suffix + "-1", "WS-" + suffix + "-2"))
            staff_one, staff_two = [row[0] for row in db_cursor.fetchall()]
            db_cursor.execute("INSERT INTO students (student_number,first_name,last_name,email,admission_date) VALUES (%s,'Issue','Reporter',%s,CURRENT_DATE)", ("WC-" + suffix, suffix + "c@example.test"))
            student_id = db_cursor.lastrowid
            db_cursor.execute("INSERT INTO complaints (student_id,category,description) VALUES (%s,'test','maintenance test')", (student_id,))
            complaint_id = db_cursor.lastrowid
        get_connection().commit()

    client = mysql_app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = user_id
        session["session_version"] = 0
    operations_page = client.get("/operations")
    token = re.search(rb'<input[^>]*name="csrf_token"[^>]*value="([^"]+)"', operations_page.data)
    assert token is not None

    response = client.post(
        f"/operations/complaints/{complaint_id}/work-order",
        data={"csrf_token": token.group(1).decode(), "description": "Repair test", "assigned_staff_id": str(staff_one)},
    )
    assert response.status_code == 302
    with mysql_app.app_context():
        work_order_id = mysql_scalar("SELECT work_order_id FROM maintenance_work_orders WHERE complaint_id=%s", (complaint_id,))
        assert mysql_scalar("SELECT assigned_staff_id FROM maintenance_work_orders WHERE work_order_id=%s", (work_order_id,)) == staff_one

    response = client.post(
        f"/operations/work-orders/{work_order_id}/assign",
        data={"csrf_token": token.group(1).decode(), "staff_id": str(staff_two)},
    )
    assert response.status_code == 302
    with mysql_app.app_context():
        assert mysql_scalar("SELECT assigned_staff_id FROM maintenance_work_orders WHERE work_order_id=%s", (work_order_id,)) == staff_two


def test_mysql_room_edit_and_safe_bed_deactivation(mysql_app):
    suffix = uuid.uuid4().hex[:10]
    with mysql_app.app_context():
        with cursor(dictionary=False) as db_cursor:
            db_cursor.execute("INSERT INTO users (username,email,password_hash) VALUES (%s,%s,'test')", ("space-" + suffix, suffix + "@example.test"))
            user_id = db_cursor.lastrowid
            db_cursor.execute("SELECT role_id FROM roles WHERE role_name='Super Admin'")
            role_id = db_cursor.fetchone()[0]
            db_cursor.execute("INSERT INTO user_roles (user_id,role_id) VALUES (%s,%s)", (user_id, role_id))
            db_cursor.execute("INSERT INTO hostel_blocks (block_name) VALUES (%s)", ("EDIT-" + suffix,))
            block_id = db_cursor.lastrowid
            db_cursor.execute("INSERT INTO rooms (block_id,room_number) VALUES (%s,'OLD')", (block_id,))
            room_id = db_cursor.lastrowid
            db_cursor.execute("INSERT INTO rooms (block_id,room_number) VALUES (%s,'EMPTY')", (block_id,))
            empty_room_id = db_cursor.lastrowid
            db_cursor.execute("INSERT INTO beds (room_id,bed_label) VALUES (%s,'RESERVED'),(%s,'FREE')", (room_id, room_id))
            db_cursor.execute("SELECT bed_id FROM beds WHERE room_id=%s ORDER BY bed_label", (room_id,))
            reserved_bed, free_bed = [row[0] for row in db_cursor.fetchall()]
            db_cursor.execute("INSERT INTO students (student_number,first_name,last_name,email,admission_date) VALUES (%s,'Space','Test',%s,CURRENT_DATE)", ("SP-" + suffix, suffix + "s@example.test"))
            student_id = db_cursor.lastrowid
        get_connection().commit()
        create_reservation(student_id, reserved_bed, date.today() + timedelta(days=30), date.today() + timedelta(days=60), None)

    client = mysql_app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = user_id
        session["session_version"] = 0
    response = client.get("/hostels")
    assert response.status_code == 200
    token = re.search(rb'<input[^>]*name="csrf_token"[^>]*value="([^"]+)"', response.data)
    assert token is not None

    response = client.post(
        f"/hostels/rooms/{room_id}/update",
        data={"csrf_token": token.group(1).decode(), "floor_number": "2", "room_number": "NEW", "room_type": "single"},
    )
    assert response.status_code == 302
    with mysql_app.app_context():
        assert mysql_scalar("SELECT room_number FROM rooms WHERE room_id=%s", (room_id,)) == "NEW"

    response = client.post(f"/hostels/beds/{reserved_bed}/deactivate", data={"csrf_token": token.group(1).decode()})
    assert response.status_code == 302
    response = client.get("/hostels")
    assert b"future reservation" in response.data
    with mysql_app.app_context():
        assert mysql_scalar("SELECT is_active FROM beds WHERE bed_id=%s", (reserved_bed,)) == 1

    response = client.post(f"/hostels/beds/{free_bed}/deactivate", data={"csrf_token": token.group(1).decode()})
    assert response.status_code == 302
    with mysql_app.app_context():
        assert mysql_scalar("SELECT is_active FROM beds WHERE bed_id=%s", (free_bed,)) == 0

    response = client.post(f"/hostels/rooms/{empty_room_id}/deactivate", data={"csrf_token": token.group(1).decode()})
    assert response.status_code == 302
    with mysql_app.app_context():
        assert mysql_scalar("SELECT is_active FROM rooms WHERE room_id=%s", (empty_room_id,)) == 0


def test_mysql_retiring_fee_structure_preserves_posted_invoice(mysql_app):
    suffix = uuid.uuid4().hex[:10]
    today = date.today()
    with mysql_app.app_context():
        with cursor(dictionary=False) as db_cursor:
            db_cursor.execute("INSERT INTO users (username,email,password_hash) VALUES (%s,%s,'test')", ("fee-admin-" + suffix, suffix + "@example.test"))
            user_id = db_cursor.lastrowid
            db_cursor.execute("SELECT role_id FROM roles WHERE role_name='Accountant'")
            role_id = db_cursor.fetchone()[0]
            db_cursor.execute("INSERT INTO user_roles (user_id,role_id) VALUES (%s,%s)", (user_id, role_id))
            db_cursor.execute("INSERT INTO students (student_number,first_name,last_name,email,admission_date) VALUES (%s,'Fee','Test',%s,CURRENT_DATE)", ("FS-" + suffix, suffix + "f@example.test"))
            student_id = db_cursor.lastrowid
            db_cursor.execute("INSERT INTO fee_categories (category_name) VALUES (%s)", ("Fee test " + suffix,))
            category_id = db_cursor.lastrowid
        get_connection().commit()
        fee_structure_id = create_fee_structure(category_id, Decimal("125.00"), "monthly", today, None)
        invoice_id = create_invoice(student_id, "FEE-INV-" + suffix, today, today, fee_structure_id, "Recorded monthly charge", None, None)

    client = mysql_app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = user_id
        session["session_version"] = 0
    finance_page = client.get("/finance")
    assert finance_page.status_code == 200
    token = re.search(rb'<input[^>]*name="csrf_token"[^>]*value="([^"]+)"', finance_page.data)
    assert token is not None

    response = client.post(
        f"/finance/fee-structures/{fee_structure_id}/deactivate",
        data={"csrf_token": token.group(1).decode()},
    )
    assert response.status_code == 302
    with mysql_app.app_context():
        assert mysql_scalar("SELECT is_active FROM fee_structures WHERE fee_structure_id=%s", (fee_structure_id,)) == 0
        assert mysql_scalar("SELECT unit_amount FROM invoice_items WHERE invoice_id=%s", (invoice_id,)) == Decimal("125.00")


def test_mysql_user_management_and_password_reset(mysql_app):
    suffix = uuid.uuid4().hex[:10]
    with mysql_app.app_context():
        with cursor(dictionary=False) as db_cursor:
            db_cursor.execute("INSERT INTO users (username,email,password_hash) VALUES (%s,%s,'test')", ("user-admin-" + suffix, suffix + "@example.test"))
            admin_id = db_cursor.lastrowid
            db_cursor.execute("SELECT role_id FROM roles WHERE role_name='Super Admin'")
            db_cursor.execute("INSERT INTO user_roles (user_id,role_id) VALUES (%s,%s)", (admin_id, db_cursor.fetchone()[0]))
        get_connection().commit()

    client = mysql_app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = admin_id
        session["session_version"] = 0
    page = client.get("/users")
    assert page.status_code == 200
    token = re.search(rb'<input[^>]*name="csrf_token"[^>]*value="([^"]+)"', page.data)
    assert token is not None
    csrf = token.group(1).decode()

    response = client.post(
        "/users",
        data={
            "csrf_token": csrf,
            "username": "managed-" + suffix,
            "email": suffix + "-managed@example.com",
            "role_name": "Warden",
            "password": "initial-test-password-2026",
            "confirm_password": "initial-test-password-2026",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    alert_text = re.search(rb'<div class="alert[^>]*>(.*?)<button', response.data, re.DOTALL)
    assert b"User account created." in response.data, alert_text.group(1) if alert_text else response.status_code
    with mysql_app.app_context():
        managed_id = mysql_scalar("SELECT user_id FROM users WHERE username=%s", ("managed-" + suffix,))
        assert mysql_scalar("SELECT COUNT(*) FROM audit_logs WHERE action='user.created' AND entity_id=%s", (managed_id,)) == 1
        assert mysql_scalar(
            "SELECT COUNT(*) FROM user_roles ur JOIN roles r ON r.role_id=ur.role_id WHERE ur.user_id=%s AND r.role_name='Warden'",
            (managed_id,),
        ) == 1

    duplicate = client.post(
        "/users",
        data={
            "csrf_token": csrf,
            "username": "managed-" + suffix,
            "email": suffix + "-duplicate@example.com",
            "role_name": "Warden",
            "password": "initial-test-password-2026",
            "confirm_password": "initial-test-password-2026",
        },
        follow_redirects=True,
    )
    assert duplicate.status_code == 200
    with mysql_app.app_context():
        assert mysql_scalar("SELECT COUNT(*) FROM audit_logs WHERE action='user.created' AND entity_id=%s", (managed_id,)) == 1

    response = client.post(
        f"/users/{managed_id}/roles",
        data={"csrf_token": csrf, "role_names": ["Accountant"]},
    )
    assert response.status_code == 302
    with mysql_app.app_context():
        assert mysql_scalar(
            "SELECT COUNT(*) FROM user_roles ur JOIN roles r ON r.role_id=ur.role_id WHERE ur.user_id=%s AND r.role_name='Accountant'",
            (managed_id,),
        ) == 1

    response = client.post(
        f"/users/{managed_id}/password",
        data={"csrf_token": csrf, "new_password": "administrator-reset-password", "confirm_password": "administrator-reset-password"},
    )
    assert response.status_code == 302
    with mysql_app.app_context():
        stored_hash = mysql_scalar("SELECT password_hash FROM users WHERE user_id=%s", (managed_id,))
        assert check_password_hash(stored_hash, "administrator-reset-password")

    with client.session_transaction() as session:
        session["user_id"] = managed_id
        session["session_version"] = 0
    stale_session = client.get("/account/password")
    assert stale_session.status_code == 302
    assert stale_session.headers["Location"].startswith("/login")
    login_page = client.get("/login")
    fresh_token = re.search(rb'<input[^>]*name="csrf_token"[^>]*value="([^"]+)"', login_page.data)
    assert fresh_token is not None

    with client.session_transaction() as session:
        session["user_id"] = managed_id
        session["session_version"] = 1
    response = client.post(
        "/account/password",
        data={
            "csrf_token": fresh_token.group(1).decode(),
            "current_password": "administrator-reset-password",
            "new_password": "self-service-new-password",
            "confirm_password": "self-service-new-password",
        },
    )
    assert response.status_code == 302
    with mysql_app.app_context():
        stored_hash = mysql_scalar("SELECT password_hash FROM users WHERE user_id=%s", (managed_id,))
        assert check_password_hash(stored_hash, "self-service-new-password")
    assert client.get("/account/password").status_code == 200

    with client.session_transaction() as session:
        session["user_id"] = admin_id
        session["session_version"] = 0
    admin_page = client.get("/users")
    admin_token = re.search(rb'<input[^>]*name="csrf_token"[^>]*value="([^"]+)"', admin_page.data)
    assert admin_token is not None
    admin_csrf = admin_token.group(1).decode()
    response = client.post(f"/users/{managed_id}/status", data={"csrf_token": admin_csrf})
    assert response.status_code == 302
    with mysql_app.app_context():
        assert mysql_scalar("SELECT is_active FROM users WHERE user_id=%s", (managed_id,)) == 0
    response = client.post(f"/users/{managed_id}/status", data={"csrf_token": admin_csrf})
    assert response.status_code == 302
    with mysql_app.app_context():
        assert mysql_scalar("SELECT is_active FROM users WHERE user_id=%s", (managed_id,)) == 1


def test_mysql_block_edit_bed_relabel_preserves_allocation_label(mysql_app):
    suffix = uuid.uuid4().hex[:10]
    start = date.today() + timedelta(days=20)
    with mysql_app.app_context():
        with cursor(dictionary=False) as db_cursor:
            db_cursor.execute("INSERT INTO users (username,email,password_hash) VALUES (%s,%s,'test')", ("label-admin-" + suffix, suffix + "@example.com"))
            user_id = db_cursor.lastrowid
            db_cursor.execute("SELECT role_id FROM roles WHERE role_name='Super Admin'")
            role_id = db_cursor.fetchone()[0]
            db_cursor.execute("INSERT INTO user_roles (user_id,role_id) VALUES (%s,%s)", (user_id, role_id))
            db_cursor.execute("INSERT INTO hostel_blocks (block_name,address) VALUES (%s,'Old address')", ("LABEL-" + suffix,))
            block_id = db_cursor.lastrowid
            db_cursor.execute("INSERT INTO rooms (block_id,room_number) VALUES (%s,'101')", (block_id,))
            room_id = db_cursor.lastrowid
            db_cursor.execute("INSERT INTO beds (room_id,bed_label) VALUES (%s,'OLD-A')", (room_id,))
            bed_id = db_cursor.lastrowid
            db_cursor.execute("INSERT INTO students (student_number,first_name,last_name,email,admission_date) VALUES (%s,'Label','Student',%s,CURRENT_DATE)", ("LB-" + suffix, suffix + "l@example.test"))
            student_id = db_cursor.lastrowid
        get_connection().commit()
        allocation_id = create_reservation(student_id, bed_id, start, start + timedelta(days=10), None)

    client = mysql_app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = user_id
        session["session_version"] = 0
    page = client.get("/hostels")
    token = re.search(rb'<input[^>]*name="csrf_token"[^>]*value="([^"]+)"', page.data)
    assert token is not None
    csrf = token.group(1).decode()

    response = client.post(
        f"/hostels/blocks/{block_id}/update",
        data={"csrf_token": csrf, "block_name": "RENAMED-" + suffix, "address": "New address"},
    )
    assert response.status_code == 302
    response = client.post(
        f"/hostels/beds/{bed_id}/relabel",
        data={"csrf_token": csrf, "bed_label": "NEW-A"},
    )
    assert response.status_code == 302

    with mysql_app.app_context():
        assert mysql_scalar("SELECT block_name FROM hostel_blocks WHERE block_id=%s", (block_id,)) == "RENAMED-" + suffix
        assert mysql_scalar("SELECT bed_label FROM beds WHERE bed_id=%s", (bed_id,)) == "NEW-A"
        assert mysql_scalar("SELECT bed_label_snapshot FROM allocations WHERE allocation_id=%s", (allocation_id,)) == "OLD-A"

    history = client.get("/allocations")
    assert b"OLD-A" in history.data
    response = client.post(f"/hostels/blocks/{block_id}/deactivate", data={"csrf_token": csrf})
    assert response.status_code == 302
    blocked_page = client.get("/hostels")
    assert b"future reservation" in blocked_page.data
    with mysql_app.app_context():
        assert mysql_scalar("SELECT is_active FROM hostel_blocks WHERE block_id=%s", (block_id,)) == 1


def test_mysql_dashboard_filters_scheduled_counts_by_date(mysql_app):
    suffix = uuid.uuid4().hex[:10]
    selected_date = date(2045, 5, 1)
    with mysql_app.app_context():
        with cursor(dictionary=False) as db_cursor:
            db_cursor.execute("INSERT INTO users (username,email,password_hash) VALUES (%s,%s,'test')", ("dashboard-" + suffix, suffix + "@example.com"))
            user_id = db_cursor.lastrowid
            db_cursor.execute("SELECT role_id FROM roles WHERE role_name='Warden'")
            role_id = db_cursor.fetchone()[0]
            db_cursor.execute("INSERT INTO user_roles (user_id,role_id) VALUES (%s,%s)", (user_id, role_id))
            db_cursor.execute("INSERT INTO hostel_blocks (block_name) VALUES (%s)", ("DATE-" + suffix,))
            block_id = db_cursor.lastrowid
            db_cursor.execute("INSERT INTO rooms (block_id,room_number) VALUES (%s,'D-1')", (block_id,))
            room_id = db_cursor.lastrowid
            db_cursor.execute("INSERT INTO beds (room_id,bed_label) VALUES (%s,'A')", (room_id,))
            bed_id = db_cursor.lastrowid
            db_cursor.execute("INSERT INTO students (student_number,first_name,last_name,email,admission_date) VALUES (%s,'Date','Filter',%s,CURRENT_DATE)", ("DF-" + suffix, suffix + "d@example.test"))
            student_id = db_cursor.lastrowid
        get_connection().commit()
        create_reservation(student_id, bed_id, selected_date, date(2045, 5, 5), None)

    client = mysql_app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = user_id
        session["session_version"] = 0
    selected = client.get("/?date=2045-05-01")
    assert selected.status_code == 200
    assert b"Report date" in selected.data and b"2045-05-01" in selected.data
    count = re.search(rb"Scheduled on selected date</span><strong>(\d+)</strong>", selected.data)
    assert count is not None and int(count.group(1)) >= 1

    earlier = client.get("/?date=2044-05-01")
    assert earlier.status_code == 200
    earlier_count = re.search(rb"Scheduled on selected date</span><strong>(\d+)</strong>", earlier.data)
    assert earlier_count is not None and int(earlier_count.group(1)) == 0

    invalid = client.get("/?date=not-a-date")
    assert invalid.status_code == 200
    assert b"Enter a valid dashboard date" in invalid.data


def mysql_scalar(sql, params):
    with cursor(dictionary=False) as db_cursor:
        db_cursor.execute(sql, params)
        return db_cursor.fetchone()[0]