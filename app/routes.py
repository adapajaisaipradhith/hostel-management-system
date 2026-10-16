import csv
import io
import secrets
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from email_validator import EmailNotValidError, validate_email
from flask import Blueprint, Response, flash, g, redirect, render_template, request, session, url_for
from mysql.connector import Error
from werkzeug.security import check_password_hash, generate_password_hash

from app.auth import login_required, roles_required
from app.db import cursor, fetch_all, fetch_one, get_connection, write_audit_record
from app.services.finance import create_fee_structure, create_invoice, record_payment
from app.services.hostel import RuleViolation, check_in, checkout, create_maintenance_hold, create_reservation
from app.services.operations import adjust_inventory, create_menu, record_meal_attendance

bp = Blueprint("records", __name__)
ADMIN = ("Super Admin", "Warden")
FINANCE = ("Super Admin", "Accountant")
ALL_STAFF = ("Super Admin", "Warden", "Accountant")


def write_audit_cursor(db_cursor, action: str, entity_type: str, entity_id: int | None = None, details: dict | None = None):
    write_audit_record(db_cursor, session.get("user_id"), action, entity_type, entity_id, details)


def execute_audited(sql: str, params: tuple, action: str, entity_type: str, entity_id: int | None = None, details: dict | None = None) -> int:
    connection = get_connection()
    try:
        with cursor(dictionary=False) as db_cursor:
            db_cursor.execute(sql, params)
            row_id = entity_id if entity_id is not None else db_cursor.lastrowid
            write_audit_cursor(db_cursor, action, entity_type, row_id, details)
        connection.commit()
        return row_id or 0
    except Exception:
        connection.rollback()
        raise


def safe_error(error: Exception) -> str:
    if isinstance(error, RuleViolation):
        return str(error)
    if isinstance(error, Error) and error.errno in (1062, 1452, 3819):
        return "The record conflicts with an existing value or database rule. Check unique values and required references."
    return "The operation could not be completed. Check the submitted values and database connection."


def csv_safe_rows(rows: list[dict]) -> list[dict]:
    return [
        {
            key: "'" + value if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")) else value
            for key, value in row.items()
        }
        for row in rows
    ]


@bp.get("/students")
@roles_required(*ALL_STAFF)
def students():
    term = request.args.get("q", "").strip()
    page = max(1, request.args.get("page", 1, type=int))
    page_size = 20
    search = f"%{term}%"
    total = fetch_one(
        "SELECT COUNT(*) AS total FROM students WHERE is_active = TRUE "
        "AND (%s = '' OR student_number LIKE %s OR first_name LIKE %s OR last_name LIKE %s OR email LIKE %s)",
        (term, search, search, search, search),
    )["total"]
    rows = fetch_all(
        "SELECT student_id, student_number, first_name, last_name, email, phone, guardian_name, guardian_phone, emergency_contact_name, emergency_contact_phone, admission_date "
        "FROM students WHERE is_active = TRUE "
        "AND (%s = '' OR student_number LIKE %s OR first_name LIKE %s OR last_name LIKE %s OR email LIKE %s) "
        "ORDER BY last_name, first_name LIMIT %s OFFSET %s",
        (term, search, search, search, search, page_size, (page - 1) * page_size),
    )
    return render_template("records/students.html", students=rows, term=term, page=page, total=total, page_size=page_size)


@bp.post("/students/<int:student_id>/update")
@roles_required(*ADMIN)
def update_student(student_id: int):
    try:
        email = validate_email(request.form["email"].strip(), check_deliverability=False).normalized
        execute_audited(
            "UPDATE students SET first_name=%s,last_name=%s,email=%s,phone=%s,guardian_name=%s,guardian_phone=%s, "
            "emergency_contact_name=%s,emergency_contact_phone=%s WHERE student_id=%s AND is_active=TRUE",
            (request.form["first_name"].strip(), request.form["last_name"].strip(), email,
             request.form.get("phone") or None, request.form.get("guardian_name") or None,
             request.form.get("guardian_phone") or None, request.form.get("emergency_contact_name") or None,
             request.form.get("emergency_contact_phone") or None, student_id),
            "student.updated", "student", student_id,
        )
        flash("Student record updated.", "success")
    except Exception as error:
        flash(safe_error(error), "error")
    return redirect(url_for("records.students"))


@bp.post("/students")
@roles_required(*ADMIN)
def create_student_route():
    fields = [request.form.get(key, "").strip() for key in ("student_number", "first_name", "last_name", "email")]
    if not all(fields):
        flash("Student number, first name, last name, and email are required.", "error")
        return redirect(url_for("records.students"))
    try:
        fields[3] = validate_email(fields[3], check_deliverability=False).normalized
    except EmailNotValidError:
        flash("Enter a valid student email address.", "error")
        return redirect(url_for("records.students"))
    try:
        student_id = execute_audited(
            "INSERT INTO students (student_number, first_name, last_name, email, phone, guardian_name, guardian_phone, "
            "emergency_contact_name, emergency_contact_phone, admission_date) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (*fields, request.form.get("phone") or None, request.form.get("guardian_name") or None,
             request.form.get("guardian_phone") or None, request.form.get("emergency_contact_name") or None,
             request.form.get("emergency_contact_phone") or None, request.form.get("admission_date") or date.today()),
              "student.created", "student",
        )
        flash("Student record created.", "success")
    except Exception as error:
        flash(safe_error(error), "error")
    return redirect(url_for("records.students"))


@bp.post("/students/<int:student_id>/deactivate")
@roles_required(*ADMIN)
def deactivate_student(student_id: int):
    try:
        execute_audited("UPDATE students SET is_active = FALSE WHERE student_id = %s", (student_id,), "student.deactivated", "student", student_id)
        flash("Student deactivated; historical allocations and financial records were retained.", "success")
    except Exception as error:
        flash(safe_error(error), "error")
    return redirect(url_for("records.students"))


@bp.get("/hostels")
@roles_required(*ADMIN)
def hostels():
    blocks = fetch_all("SELECT block_id, block_name, address FROM hostel_blocks WHERE is_active = TRUE ORDER BY block_name")
    block_directory = fetch_all("SELECT block_id,block_name,address,is_active FROM hostel_blocks ORDER BY block_name")
    rooms = fetch_all(
        "SELECT r.room_id, hb.block_name, r.floor_number, r.room_number, r.room_type, COUNT(b.bed_id) AS bed_count "
        "FROM rooms r JOIN hostel_blocks hb ON hb.block_id = r.block_id LEFT JOIN beds b ON b.room_id = r.room_id AND b.is_active = TRUE "
        "WHERE r.is_active = TRUE GROUP BY r.room_id, hb.block_name ORDER BY hb.block_name, r.floor_number, r.room_number"
    )
    beds = fetch_all(
        "SELECT b.bed_id,b.bed_label,b.is_active,r.room_id,r.room_number,hb.block_name "
        "FROM beds b JOIN rooms r ON r.room_id=b.room_id JOIN hostel_blocks hb ON hb.block_id=r.block_id "
        "ORDER BY hb.block_name,r.room_number,b.bed_label"
    )
    return render_template("records/hostels.html", blocks=blocks, block_directory=block_directory, rooms=rooms, beds=beds)


@bp.post("/hostels/blocks")
@roles_required(*ADMIN)
def create_block():
    name = request.form.get("block_name", "").strip()
    if not name:
        flash("Block name is required.", "error")
    else:
        try:
            execute_audited("INSERT INTO hostel_blocks (block_name, address) VALUES (%s, %s)", (name, request.form.get("address") or None), "hostel_block.created", "hostel_block")
            flash("Hostel block added.", "success")
        except Exception as error:
            flash(safe_error(error), "error")
    return redirect(url_for("records.hostels"))


@bp.post("/hostels/blocks/<int:block_id>/update")
@roles_required(*ADMIN)
def update_block(block_id: int):
    name = request.form.get("block_name", "").strip()
    if not name:
        flash("Block name is required.", "error")
        return redirect(url_for("records.hostels"))
    connection = get_connection()
    try:
        with cursor(dictionary=False) as db_cursor:
            db_cursor.execute("SELECT block_id FROM hostel_blocks WHERE block_id=%s AND is_active=TRUE FOR UPDATE", (block_id,))
            if db_cursor.fetchone() is None:
                raise RuleViolation("Block does not exist or is inactive.")
            db_cursor.execute("UPDATE hostel_blocks SET block_name=%s,address=%s WHERE block_id=%s", (name, request.form.get("address") or None, block_id))
            write_audit_cursor(db_cursor, "hostel_block.updated", "hostel_block", block_id)
        connection.commit()
        flash("Hostel block updated.", "success")
    except Exception as error:
        connection.rollback()
        flash(safe_error(error), "error")
    return redirect(url_for("records.hostels"))


@bp.post("/hostels/blocks/<int:block_id>/deactivate")
@roles_required(*ADMIN)
def deactivate_block(block_id: int):
    connection = get_connection()
    try:
        with cursor(dictionary=False) as db_cursor:
            db_cursor.execute("SELECT block_id FROM hostel_blocks WHERE block_id=%s AND is_active=TRUE FOR UPDATE", (block_id,))
            if db_cursor.fetchone() is None:
                raise RuleViolation("Block does not exist or is already inactive.")
            db_cursor.execute(
                "SELECT a.allocation_id FROM allocations a JOIN beds b ON b.bed_id=a.bed_id "
                "JOIN rooms r ON r.room_id=b.room_id WHERE r.block_id=%s AND a.status<>'cancelled' AND "
                "((a.checked_in_at IS NOT NULL AND a.checked_out_at IS NULL) OR a.planned_start_date>=CURRENT_DATE "
                "OR a.effective_end_date IS NULL OR a.effective_end_date>CURRENT_DATE) LIMIT 1 FOR UPDATE",
                (block_id,),
            )
            if db_cursor.fetchone() is not None:
                raise RuleViolation("Block has a current stay or future reservation; complete or reassign it first.")
            db_cursor.execute("UPDATE hostel_blocks SET is_active=FALSE WHERE block_id=%s", (block_id,))
            write_audit_cursor(db_cursor, "hostel_block.deactivated", "hostel_block", block_id)
        connection.commit()
        flash("Block deactivated; its rooms and historical records were retained.", "success")
    except Exception as error:
        connection.rollback()
        flash(safe_error(error), "error")
    return redirect(url_for("records.hostels"))


@bp.post("/hostels/rooms")
@roles_required(*ADMIN)
def create_room():
    try:
        execute_audited(
            "INSERT INTO rooms (block_id, floor_number, room_number, room_type) VALUES (%s, %s, %s, %s)",
            (int(request.form["block_id"]), int(request.form.get("floor_number", 0)), request.form["room_number"].strip(), request.form.get("room_type", "standard").strip()),
            "room.created", "room",
        )
        flash("Room added.", "success")
    except Exception as error:
        flash(safe_error(error), "error")
    return redirect(url_for("records.hostels"))


@bp.post("/hostels/rooms/<int:room_id>/update")
@roles_required(*ADMIN)
def update_room(room_id: int):
    try:
        room_number = request.form["room_number"].strip()
        room_type = request.form["room_type"].strip()
        if not room_number or not room_type:
            raise RuleViolation("Room number and type are required.")
        execute_audited(
            "UPDATE rooms SET floor_number=%s,room_number=%s,room_type=%s WHERE room_id=%s AND is_active=TRUE",
            (int(request.form["floor_number"]), room_number, room_type, room_id),
            "room.updated", "room", room_id,
        )
        flash("Room details updated.", "success")
    except Exception as error:
        flash(safe_error(error), "error")
    return redirect(url_for("records.hostels"))


def _space_has_future_or_physical_allocations(db_cursor, bed_id: int | None = None, room_id: int | None = None) -> bool:
    if bed_id is not None:
        db_cursor.execute(
            "SELECT allocation_id FROM allocations WHERE bed_id=%s AND status<>'cancelled' AND "
            "((checked_in_at IS NOT NULL AND checked_out_at IS NULL) OR planned_start_date>=CURRENT_DATE "
            "OR effective_end_date IS NULL OR effective_end_date>CURRENT_DATE) LIMIT 1 FOR UPDATE",
            (bed_id,),
        )
    else:
        db_cursor.execute(
            "SELECT a.allocation_id FROM allocations a JOIN beds b ON b.bed_id=a.bed_id "
            "WHERE b.room_id=%s AND a.status<>'cancelled' AND "
            "((a.checked_in_at IS NOT NULL AND a.checked_out_at IS NULL) OR a.planned_start_date>=CURRENT_DATE "
            "OR a.effective_end_date IS NULL OR a.effective_end_date>CURRENT_DATE) LIMIT 1 FOR UPDATE",
            (room_id,),
        )
    return db_cursor.fetchone() is not None


@bp.post("/hostels/rooms/<int:room_id>/deactivate")
@roles_required(*ADMIN)
def deactivate_room(room_id: int):
    connection = get_connection()
    try:
        with cursor(dictionary=False) as db_cursor:
            db_cursor.execute("SELECT room_id FROM rooms WHERE room_id=%s AND is_active=TRUE FOR UPDATE", (room_id,))
            if db_cursor.fetchone() is None:
                raise RuleViolation("Room does not exist or is already inactive.")
            if _space_has_future_or_physical_allocations(db_cursor, room_id=room_id):
                raise RuleViolation("Room has a current stay or future reservation; reassign or complete it before deactivation.")
            db_cursor.execute("UPDATE rooms SET is_active=FALSE WHERE room_id=%s", (room_id,))
            write_audit_cursor(db_cursor, "room.deactivated", "room", room_id)
        connection.commit()
        flash("Room deactivated; its beds and allocation history were retained.", "success")
    except Exception as error:
        connection.rollback()
        flash(safe_error(error), "error")
    return redirect(url_for("records.hostels"))


@bp.post("/hostels/rooms/<int:room_id>/beds")
@roles_required(*ADMIN)
def create_bed(room_id: int):
    label = request.form.get("bed_label", "").strip()
    try:
        execute_audited("INSERT INTO beds (room_id, bed_label) VALUES (%s, %s)", (room_id, label), "bed.created", "bed")
        flash("Bed added.", "success")
    except Exception as error:
        flash(safe_error(error), "error")
    return redirect(url_for("records.hostels"))


@bp.post("/hostels/beds/<int:bed_id>/relabel")
@roles_required(*ADMIN)
def relabel_bed(bed_id: int):
    label = request.form.get("bed_label", "").strip()
    if not label or len(label) > 30:
        flash("Bed label is required and must be at most 30 characters.", "error")
        return redirect(url_for("records.hostels"))
    connection = get_connection()
    try:
        with cursor(dictionary=False) as db_cursor:
            db_cursor.execute("SELECT bed_id FROM beds WHERE bed_id=%s AND is_active=TRUE FOR UPDATE", (bed_id,))
            if db_cursor.fetchone() is None:
                raise RuleViolation("Bed does not exist or is inactive.")
            db_cursor.execute("UPDATE beds SET bed_label=%s WHERE bed_id=%s", (label, bed_id))
            write_audit_cursor(db_cursor, "bed.relabeled", "bed", bed_id, {"label": label})
        connection.commit()
        flash("Current bed label updated; allocation history keeps its original label snapshot.", "success")
    except Exception as error:
        connection.rollback()
        flash(safe_error(error), "error")
    return redirect(url_for("records.hostels"))


@bp.post("/hostels/beds/<int:bed_id>/deactivate")
@roles_required(*ADMIN)
def deactivate_bed(bed_id: int):
    connection = get_connection()
    try:
        with cursor(dictionary=False) as db_cursor:
            db_cursor.execute("SELECT bed_id FROM beds WHERE bed_id=%s AND is_active=TRUE FOR UPDATE", (bed_id,))
            if db_cursor.fetchone() is None:
                raise RuleViolation("Bed does not exist or is already inactive.")
            if _space_has_future_or_physical_allocations(db_cursor, bed_id=bed_id):
                raise RuleViolation("Bed has a current stay or future reservation; reassign or complete it before deactivation.")
            db_cursor.execute("UPDATE beds SET is_active=FALSE WHERE bed_id=%s", (bed_id,))
            write_audit_cursor(db_cursor, "bed.deactivated", "bed", bed_id)
        connection.commit()
        flash("Bed deactivated; allocation history was retained.", "success")
    except Exception as error:
        connection.rollback()
        flash(safe_error(error), "error")
    return redirect(url_for("records.hostels"))


@bp.get("/allocations")
@roles_required(*ADMIN)
def allocations():
    rows = fetch_all(
        "SELECT a.allocation_id, a.planned_start_date, a.planned_end_date, a.checked_in_at, a.checked_out_at, a.status, "
        "s.student_number, s.first_name, s.last_name, hb.block_name, r.room_number, COALESCE(a.bed_label_snapshot,b.bed_label) AS bed_label "
        "FROM allocations a JOIN students s ON s.student_id = a.student_id "
        "JOIN beds b ON b.bed_id = a.bed_id JOIN rooms r ON r.room_id = b.room_id "
        "JOIN hostel_blocks hb ON hb.block_id = r.block_id ORDER BY a.created_at DESC LIMIT 100"
    )
    students_list = fetch_all("SELECT student_id, student_number, first_name, last_name FROM students WHERE is_active = TRUE ORDER BY last_name")
    beds = fetch_all("SELECT b.bed_id, hb.block_name, r.room_number, b.bed_label FROM beds b JOIN rooms r ON r.room_id=b.room_id JOIN hostel_blocks hb ON hb.block_id=r.block_id WHERE b.is_active=TRUE AND r.is_active=TRUE AND hb.is_active=TRUE ORDER BY hb.block_name,r.room_number,b.bed_label")
    return render_template("records/allocations.html", allocations=rows, students=students_list, beds=beds)


@bp.post("/allocations")
@roles_required(*ADMIN)
def reserve_bed():
    try:
        end = request.form.get("planned_end_date") or None
        allocation_id = create_reservation(int(request.form["student_id"]), int(request.form["bed_id"]), date.fromisoformat(request.form["planned_start_date"]), date.fromisoformat(end) if end else None, request_user_id())
        flash("Reservation created after checking student, bed, and maintenance conflicts.", "success")
    except Exception as error:
        flash(safe_error(error), "error")
    return redirect(url_for("records.allocations"))


def request_user_id() -> int:
    from flask import session
    return int(session["user_id"])


def student_for_current_user() -> dict:
    from flask import session
    student = fetch_one("SELECT student_id FROM students WHERE user_id=%s AND is_active=TRUE", (session["user_id"],))
    if student is None:
        raise RuleViolation("This account is not linked to an active student record.")
    return student


@bp.post("/my-hostel/leave")
@roles_required("Student")
def student_leave_request():
    try:
        student = student_for_current_user()
        execute_audited(
            "INSERT INTO leave_requests (student_id,start_date,end_date,reason) VALUES (%s,%s,%s,%s)",
            (student["student_id"], request.form["start_date"], request.form["end_date"], request.form["reason"].strip()),
            "leave_request.created", "leave_request",
        )
        flash("Leave request submitted for review.", "success")
    except Exception as error:
        flash(safe_error(error), "error")
    return redirect(url_for("dashboard.index"))


@bp.post("/my-hostel/complaints")
@roles_required("Student")
def student_complaint():
    try:
        student = student_for_current_user()
        execute_audited(
            "INSERT INTO complaints (student_id,category,description,priority) VALUES (%s,%s,%s,'normal')",
            (student["student_id"], request.form["category"].strip(), request.form["description"].strip()),
            "complaint.created", "complaint",
        )
        flash("Complaint submitted.", "success")
    except Exception as error:
        flash(safe_error(error), "error")
    return redirect(url_for("dashboard.index"))


@bp.post("/my-hostel/meals/<int:menu_id>/attendance")
@roles_required("Student")
def student_meal_attendance(menu_id: int):
    try:
        student = student_for_current_user()
        record_meal_attendance(menu_id, student["student_id"], request_user_id())
        flash("Meal attendance recorded.", "success")
    except Exception as error:
        flash(safe_error(error), "error")
    return redirect(url_for("dashboard.index"))


@bp.post("/allocations/<int:allocation_id>/check-in")
@roles_required(*ADMIN)
def allocation_check_in(allocation_id: int):
    try:
        check_in(allocation_id, request_user_id())
        flash("Student checked in; physical occupancy is now active.", "success")
    except Exception as error:
        flash(safe_error(error), "error")
    return redirect(url_for("records.allocations"))


@bp.post("/allocations/<int:allocation_id>/checkout")
@roles_required(*ADMIN)
def allocation_checkout(allocation_id: int):
    try:
        cleared = checkout(allocation_id, request_user_id(), request.form.get("note") or None)
        flash("Checkout completed and recorded." if cleared else "Checkout blocked: posted hostel fees remain outstanding.", "success" if cleared else "error")
    except Exception as error:
        flash(safe_error(error), "error")
    return redirect(url_for("records.allocations"))


@bp.get("/finance")
@roles_required(*FINANCE)
def finance():
    balances = fetch_all("SELECT v.*, s.student_number, s.first_name, s.last_name FROM v_invoice_balances v JOIN students s ON s.student_id=v.student_id WHERE v.status <> 'void' ORDER BY v.invoice_id DESC LIMIT 200")
    students_list = fetch_all("SELECT student_id, student_number, first_name, last_name FROM students WHERE is_active=TRUE ORDER BY last_name")
    payments = fetch_all("SELECT p.payment_id,p.receipt_number,p.amount,p.received_at,p.method,p.status,s.student_number,s.first_name,s.last_name FROM payments p JOIN students s ON s.student_id=p.student_id ORDER BY p.received_at DESC LIMIT 100")
    categories = fetch_all("SELECT fee_category_id,category_name FROM fee_categories WHERE is_active=TRUE ORDER BY category_name")
    fee_structures = fetch_all("SELECT fs.fee_structure_id,fc.category_name,fs.amount,fs.frequency,fs.effective_start,fs.effective_end FROM fee_structures fs JOIN fee_categories fc ON fc.fee_category_id=fs.fee_category_id WHERE fs.is_active=TRUE AND fc.is_active=TRUE ORDER BY fc.category_name,fs.effective_start DESC")
    return render_template("records/finance.html", balances=balances, students=students_list, payments=payments, categories=categories, fee_structures=fee_structures)


@bp.post("/finance/invoices")
@roles_required(*FINANCE)
def add_invoice():
    try:
        today = date.fromisoformat(request.form.get("issue_date") or date.today().isoformat())
        due = date.fromisoformat(request.form["due_date"])
        number = "INV-" + datetime.now().strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(2).upper()
        invoice_id = create_invoice(int(request.form["student_id"]), number, today, due, int(request.form["fee_structure_id"]), request.form["description"].strip(), None, request_user_id())
        flash(f"Invoice {number} posted.", "success")
    except (ValueError, InvalidOperation, Error, RuleViolation, KeyError) as error:
        flash(safe_error(error), "error")
    return redirect(url_for("records.finance"))


@bp.post("/finance/categories")
@roles_required("Super Admin")
def create_fee_category():
    try:
        execute_audited("INSERT INTO fee_categories (category_name,description) VALUES (%s,%s)", (request.form["category_name"].strip(), request.form.get("description", "").strip()), "fee_category.created", "fee_category")
        flash("Fee category added.", "success")
    except Exception as error:
        flash(safe_error(error), "error")
    return redirect(url_for("records.finance"))


@bp.post("/finance/fee-structures")
@roles_required("Super Admin", "Accountant")
def create_fee_structure():
    try:
        amount = Decimal(request.form["amount"])
        if amount < 0:
            raise RuleViolation("Fee amount cannot be negative.")
        create_fee_structure(int(request.form["fee_category_id"]), amount, request.form["frequency"], date.fromisoformat(request.form["effective_start"]), date.fromisoformat(request.form["effective_end"]) if request.form.get("effective_end") else None, request_user_id())
        flash("Fee structure configured.", "success")
    except Exception as error:
        flash(safe_error(error), "error")
    return redirect(url_for("records.finance"))


@bp.post("/finance/fee-structures/<int:fee_structure_id>/deactivate")
@roles_required("Super Admin", "Accountant")
def deactivate_fee_structure(fee_structure_id: int):
    try:
        execute_audited("UPDATE fee_structures SET is_active=FALSE WHERE fee_structure_id=%s AND is_active=TRUE", (fee_structure_id,), "fee_structure.deactivated", "fee_structure", fee_structure_id)
        flash("Fee structure retired. Posted invoices and historical line amounts were unchanged.", "success")
    except Exception as error:
        flash(safe_error(error), "error")
    return redirect(url_for("records.finance"))


@bp.post("/finance/payments")
@roles_required(*FINANCE)
def add_payment():
    try:
        amount = Decimal(request.form["amount"])
        student_id = int(request.form["student_id"])
        receipt = "RCT-" + datetime.now().strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(2).upper()
        payment_id = record_payment(student_id, int(request.form["invoice_id"]), receipt, amount, request.form["method"], request.form.get("reference") or None, request_user_id())
        flash(f"Manual payment recorded. Receipt: {receipt}", "success")
    except (ValueError, InvalidOperation, Error, RuleViolation, KeyError) as error:
        flash(safe_error(error), "error")
    return redirect(url_for("records.finance"))


@bp.get("/finance/receipts/<int:payment_id>")
@roles_required(*FINANCE)
def payment_receipt(payment_id: int):
    payment = fetch_one(
        "SELECT p.receipt_number,p.amount,p.received_at,p.method,p.reference,p.status, "
        "s.student_number,s.first_name,s.last_name, GROUP_CONCAT(i.invoice_number ORDER BY i.invoice_number SEPARATOR ', ') AS invoices "
        "FROM payments p JOIN students s ON s.student_id=p.student_id "
        "LEFT JOIN payment_applications pa ON pa.payment_id=p.payment_id "
        "LEFT JOIN invoices i ON i.invoice_id=pa.invoice_id WHERE p.payment_id=%s "
        "GROUP BY p.payment_id,s.student_id",
        (payment_id,),
    )
    if payment is None:
        flash("Receipt not found.", "error")
        return redirect(url_for("records.finance"))
    return render_template("records/receipt.html", payment=payment)


@bp.get("/reports/occupancy.csv")
@roles_required(*ADMIN)
def occupancy_csv():
    rows = fetch_all(
        "SELECT hb.block_name, r.floor_number, r.room_number, b.bed_label, "
        "CASE WHEN a.checked_in_at IS NOT NULL AND a.checked_out_at IS NULL THEN 'occupied' "
        "WHEN h.hold_id IS NOT NULL THEN 'maintenance' ELSE 'not physically occupied' END AS physical_state "
        "FROM beds b JOIN rooms r ON r.room_id=b.room_id JOIN hostel_blocks hb ON hb.block_id=r.block_id "
        "LEFT JOIN allocations a ON a.bed_id=b.bed_id AND a.checked_in_at IS NOT NULL AND a.checked_out_at IS NULL "
        "LEFT JOIN maintenance_holds h ON (h.bed_id=b.bed_id OR h.room_id=r.room_id) AND h.status IN ('scheduled','active') "
        "AND h.start_date <= CURRENT_DATE AND (h.end_date IS NULL OR h.end_date > CURRENT_DATE) "
        "ORDER BY hb.block_name,r.room_number,b.bed_label"
    )
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(rows[0].keys()) if rows else ["block_name", "floor_number", "room_number", "bed_label", "physical_state"])
    writer.writeheader()
    writer.writerows(csv_safe_rows(rows))
    return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=hostel-occupancy.csv"})


@bp.get("/operations")
@roles_required("Super Admin", "Warden", "Mess Manager")
def operations():
    can_manage_students = bool(g.current_roles.intersection({"Super Admin", "Warden"}))
    attendance = fetch_all("SELECT a.attendance_date, a.session_name, a.status, s.student_number, s.first_name, s.last_name FROM attendance a JOIN students s ON s.student_id=a.student_id ORDER BY a.attendance_date DESC, a.attendance_id DESC LIMIT 100") if can_manage_students else []
    leave_requests = fetch_all("SELECT lr.leave_request_id, lr.start_date, lr.end_date, lr.status, lr.reason, s.student_number, s.first_name, s.last_name FROM leave_requests lr JOIN students s ON s.student_id=lr.student_id ORDER BY lr.created_at DESC LIMIT 100") if can_manage_students else []
    complaints = fetch_all("SELECT c.complaint_id,c.category,c.priority,c.status,c.description,c.created_at,s.student_number,s.first_name,s.last_name FROM complaints c JOIN students s ON s.student_id=c.student_id ORDER BY c.created_at DESC LIMIT 100") if can_manage_students else []
    visitors = fetch_all("SELECT v.visitor_id,v.visitor_name,v.purpose,v.checked_in_at,v.checked_out_at,s.first_name,s.last_name FROM visitors v JOIN students s ON s.student_id=v.student_id ORDER BY v.checked_in_at DESC LIMIT 100") if can_manage_students else []
    holds = fetch_all("SELECT hold_id,room_id,bed_id,start_date,end_date,reason,status FROM maintenance_holds WHERE status IN ('scheduled','active') ORDER BY start_date DESC LIMIT 100") if can_manage_students else []
    work_orders = fetch_all("SELECT wo.work_order_id,wo.description,wo.status,wo.opened_at,wo.completed_at,wo.complaint_id,s.first_name,s.last_name,st.staff_id AS assigned_staff_id,st.first_name AS staff_first_name,st.last_name AS staff_last_name FROM maintenance_work_orders wo LEFT JOIN complaints c ON c.complaint_id=wo.complaint_id LEFT JOIN students s ON s.student_id=c.student_id LEFT JOIN staff st ON st.staff_id=wo.assigned_staff_id ORDER BY wo.opened_at DESC LIMIT 100") if can_manage_students else []
    menus = fetch_all("SELECT menu_id,service_date,meal_type,title,published FROM menus ORDER BY service_date DESC LIMIT 50")
    inventory = fetch_all("SELECT i.inventory_item_id,i.item_name,i.unit_name,i.reorder_level,COALESCE(SUM(t.quantity_change),0) AS current_stock FROM inventory_items i LEFT JOIN inventory_transactions t ON t.inventory_item_id=i.inventory_item_id WHERE i.is_active=TRUE GROUP BY i.inventory_item_id ORDER BY i.item_name")
    students_list = fetch_all("SELECT student_id,student_number,first_name,last_name FROM students WHERE is_active=TRUE ORDER BY last_name") if can_manage_students else []
    rooms_list = fetch_all("SELECT r.room_id,hb.block_name,r.room_number FROM rooms r JOIN hostel_blocks hb ON hb.block_id=r.block_id WHERE r.is_active=TRUE ORDER BY hb.block_name,r.room_number") if can_manage_students else []
    staff_list = fetch_all("SELECT staff_id,staff_number,first_name,last_name,job_title FROM staff WHERE is_active=TRUE ORDER BY last_name,first_name") if can_manage_students else []
    return render_template("records/operations.html", attendance=attendance, leave_requests=leave_requests, complaints=complaints, visitors=visitors, holds=holds, work_orders=work_orders, menus=menus, inventory=inventory, students=students_list, rooms=rooms_list, staff=staff_list, can_manage_students=can_manage_students)


@bp.get("/staff")
@roles_required("Super Admin", "Warden")
def staff_directory():
    rows = fetch_all("SELECT staff_id,staff_number,first_name,last_name,job_title,phone,is_active FROM staff ORDER BY last_name,first_name")
    return render_template("records/staff.html", staff=rows)


@bp.post("/staff")
@roles_required("Super Admin")
def create_staff():
    try:
        execute_audited(
            "INSERT INTO staff (staff_number,first_name,last_name,job_title,phone) VALUES (%s,%s,%s,%s,%s)",
            (request.form["staff_number"].strip(), request.form["first_name"].strip(), request.form["last_name"].strip(), request.form["job_title"].strip(), request.form.get("phone") or None),
            "staff.created", "staff",
        )
        flash("Staff record created.", "success")
    except Exception as error:
        flash(safe_error(error), "error")
    return redirect(url_for("records.staff_directory"))


@bp.post("/staff/<int:staff_id>/deactivate")
@roles_required("Super Admin")
def deactivate_staff(staff_id: int):
    try:
        execute_audited("UPDATE staff SET is_active=FALSE WHERE staff_id=%s", (staff_id,), "staff.deactivated", "staff", staff_id)
        flash("Staff record deactivated; history retained.", "success")
    except Exception as error:
        flash(safe_error(error), "error")
    return redirect(url_for("records.staff_directory"))


@bp.get("/users")
@roles_required("Super Admin")
def user_management():
    users = fetch_all(
        "SELECT u.user_id,u.username,u.email,u.is_active,u.created_at, "
        "COALESCE(GROUP_CONCAT(DISTINCT r.role_name ORDER BY r.role_name SEPARATOR ', '),'') AS role_names, "
        "MAX(s.student_number) AS student_number,MAX(st.staff_number) AS staff_number "
        "FROM users u LEFT JOIN user_roles ur ON ur.user_id=u.user_id "
        "LEFT JOIN roles r ON r.role_id=ur.role_id "
        "LEFT JOIN students s ON s.user_id=u.user_id "
        "LEFT JOIN staff st ON st.user_id=u.user_id "
        "GROUP BY u.user_id ORDER BY u.username"
    )
    roles = fetch_all("SELECT role_name FROM roles ORDER BY role_name")
    return render_template("records/users.html", users=users, roles=roles)


@bp.post("/users")
@roles_required("Super Admin")
def create_user_account():
    username = request.form.get("username", "").strip()
    email_raw = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    confirmation = request.form.get("confirm_password", "")
    role_name = request.form.get("role_name", "")
    student_number = request.form.get("student_number", "").strip()
    staff_number = request.form.get("staff_number", "").strip()
    try:
        email = validate_email(email_raw, check_deliverability=False).normalized
        if not username or len(username) > 80:
            raise RuleViolation("Username is required and must be at most 80 characters.")
        if len(password) < 12 or password != confirmation:
            raise RuleViolation("Passwords must match and be at least 12 characters.")
        if student_number and staff_number:
            raise RuleViolation("Link an account to a student or staff record, not both.")
        if role_name == "Student" and not student_number:
            raise RuleViolation("A Student login must be linked to an existing student number.")

        connection = get_connection()
        with cursor(dictionary=False) as db_cursor:
            db_cursor.execute("SELECT role_id FROM roles WHERE role_name=%s", (role_name,))
            role = db_cursor.fetchone()
            if role is None:
                raise RuleViolation("Choose one of the configured roles.")
            db_cursor.execute(
                "INSERT INTO users (username,email,password_hash) VALUES (%s,%s,%s)",
                (username, email, generate_password_hash(password)),
            )
            user_id = db_cursor.lastrowid
            db_cursor.execute("INSERT INTO user_roles (user_id,role_id) VALUES (%s,%s)", (user_id, role[0]))
            if student_number:
                db_cursor.execute("UPDATE students SET user_id=%s WHERE student_number=%s AND is_active=TRUE AND user_id IS NULL", (user_id, student_number))
                if db_cursor.rowcount != 1:
                    raise RuleViolation("Student number was not found, is inactive, or already has a login.")
            if staff_number:
                db_cursor.execute("UPDATE staff SET user_id=%s WHERE staff_number=%s AND is_active=TRUE AND user_id IS NULL", (user_id, staff_number))
                if db_cursor.rowcount != 1:
                    raise RuleViolation("Staff number was not found, is inactive, or already has a login.")
            write_audit_cursor(db_cursor, "user.created", "user", user_id, {"role": role_name})
        connection.commit()
        flash("User account created.", "success")
    except (EmailNotValidError, RuleViolation, Error, ValueError) as error:
        if "connection" in locals():
            connection.rollback()
        flash("Enter a valid email address." if isinstance(error, EmailNotValidError) else safe_error(error), "error")
    return redirect(url_for("records.user_management"))


@bp.post("/users/<int:user_id>/roles")
@roles_required("Super Admin")
def update_user_roles(user_id: int):
    selected_names = set(request.form.getlist("role_names"))
    connection = get_connection()
    try:
        with cursor(dictionary=False) as db_cursor:
            db_cursor.execute("SELECT role_id,role_name FROM roles")
            role_map = {row[1]: row[0] for row in db_cursor.fetchall()}
            if not selected_names or not selected_names.issubset(role_map):
                raise RuleViolation("Choose one or more valid roles.")
            db_cursor.execute("SELECT is_active FROM users WHERE user_id=%s FOR UPDATE", (user_id,))
            user = db_cursor.fetchone()
            if user is None:
                raise RuleViolation("User does not exist.")
            db_cursor.execute("SELECT r.role_name FROM user_roles ur JOIN roles r ON r.role_id=ur.role_id WHERE ur.user_id=%s", (user_id,))
            previous_names = {row[0] for row in db_cursor.fetchall()}
            if user[0] and "Super Admin" in previous_names and "Super Admin" not in selected_names:
                db_cursor.execute("SELECT COUNT(*) FROM users u JOIN user_roles ur ON ur.user_id=u.user_id JOIN roles r ON r.role_id=ur.role_id WHERE u.is_active=TRUE AND r.role_name='Super Admin'")
                if db_cursor.fetchone()[0] <= 1:
                    raise RuleViolation("The last active Super Admin cannot lose that role.")
            db_cursor.execute("DELETE FROM user_roles WHERE user_id=%s", (user_id,))
            db_cursor.executemany(
                "INSERT INTO user_roles (user_id,role_id) VALUES (%s,%s)",
                [(user_id, role_map[name]) for name in sorted(selected_names)],
            )
            write_audit_cursor(db_cursor, "user.roles_changed", "user", user_id, {"roles": sorted(selected_names)})
        connection.commit()
        flash("User roles updated.", "success")
    except Exception as error:
        connection.rollback()
        flash(safe_error(error), "error")
    return redirect(url_for("records.user_management"))


@bp.post("/users/<int:user_id>/status")
@roles_required("Super Admin")
def toggle_user_status(user_id: int):
    connection = get_connection()
    try:
        with cursor(dictionary=False) as db_cursor:
            db_cursor.execute("SELECT is_active FROM users WHERE user_id=%s FOR UPDATE", (user_id,))
            user = db_cursor.fetchone()
            if user is None:
                raise RuleViolation("User does not exist.")
            active = bool(user[0])
            if active and user_id == session.get("user_id"):
                raise RuleViolation("You cannot deactivate your own account.")
            if active:
                db_cursor.execute("SELECT COUNT(*) FROM user_roles ur JOIN roles r ON r.role_id=ur.role_id WHERE ur.user_id=%s AND r.role_name='Super Admin'", (user_id,))
                is_super_admin = db_cursor.fetchone()[0] > 0
                if is_super_admin:
                    db_cursor.execute("SELECT COUNT(*) FROM users u JOIN user_roles ur ON ur.user_id=u.user_id JOIN roles r ON r.role_id=ur.role_id WHERE u.is_active=TRUE AND r.role_name='Super Admin'")
                    if db_cursor.fetchone()[0] <= 1:
                        raise RuleViolation("The last active Super Admin cannot be deactivated.")
            new_active = not active
            db_cursor.execute("UPDATE users SET is_active=%s WHERE user_id=%s", (new_active, user_id))
            write_audit_cursor(db_cursor, "user.activated" if new_active else "user.deactivated", "user", user_id)
        connection.commit()
        flash("Account activated." if new_active else "Account deactivated; historical records were retained.", "success")
    except Exception as error:
        connection.rollback()
        flash(safe_error(error), "error")
    return redirect(url_for("records.user_management"))


@bp.post("/users/<int:user_id>/password")
@roles_required("Super Admin")
def admin_reset_password(user_id: int):
    password = request.form.get("new_password", "")
    confirmation = request.form.get("confirm_password", "")
    if len(password) < 12 or password != confirmation:
        flash("Passwords must match and be at least 12 characters.", "error")
        return redirect(url_for("records.user_management"))
    connection = get_connection()
    try:
        with cursor(dictionary=False) as db_cursor:
            db_cursor.execute("SELECT user_id FROM users WHERE user_id=%s AND is_active=TRUE FOR UPDATE", (user_id,))
            if db_cursor.fetchone() is None:
                raise RuleViolation("Active user does not exist.")
            db_cursor.execute("UPDATE users SET password_hash=%s,session_version=session_version+1 WHERE user_id=%s", (generate_password_hash(password), user_id))
            db_cursor.execute("SELECT session_version FROM users WHERE user_id=%s", (user_id,))
            new_session_version = db_cursor.fetchone()[0]
            write_audit_cursor(db_cursor, "user.password_reset", "user", user_id)
        connection.commit()
        if user_id == session.get("user_id"):
            session["session_version"] = new_session_version
        flash("Password reset; the user must sign in with the new password.", "success")
    except Exception as error:
        connection.rollback()
        flash(safe_error(error), "error")
    return redirect(url_for("records.user_management"))


@bp.route("/account/password", methods=["GET", "POST"])
@login_required
def change_own_password():
    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirmation = request.form.get("confirm_password", "")
        if len(new_password) < 12 or new_password != confirmation:
            flash("New passwords must match and be at least 12 characters.", "error")
            return redirect(url_for("records.change_own_password"))
        connection = get_connection()
        try:
            with cursor(dictionary=False) as db_cursor:
                db_cursor.execute("SELECT password_hash FROM users WHERE user_id=%s AND is_active=TRUE FOR UPDATE", (session["user_id"],))
                user = db_cursor.fetchone()
                if user is None or not check_password_hash(user[0], current_password):
                    raise RuleViolation("Current password is incorrect.")
                db_cursor.execute("UPDATE users SET password_hash=%s,session_version=session_version+1 WHERE user_id=%s", (generate_password_hash(new_password), session["user_id"]))
                write_audit_cursor(db_cursor, "user.password_changed", "user", session["user_id"])
            connection.commit()
            session["session_version"] = g.current_user["session_version"] + 1
            flash("Password changed successfully.", "success")
            return redirect(url_for("dashboard.index"))
        except Exception as error:
            connection.rollback()
            flash(safe_error(error), "error")
    return render_template("auth/change_password.html")


@bp.post("/operations/attendance")
@roles_required(*ADMIN)
def record_attendance():
    try:
        execute_audited("INSERT INTO attendance (student_id, attendance_date, session_name, status, recorded_by) VALUES (%s,%s,%s,%s,%s)", (int(request.form["student_id"]), request.form["attendance_date"], request.form.get("session_name", "daily"), request.form["status"], request_user_id()), "attendance.recorded", "attendance")
        flash("Attendance recorded.", "success")
    except Exception as error:
        flash(safe_error(error), "error")
    return redirect(url_for("records.operations"))


@bp.post("/operations/leave")
@roles_required(*ALL_STAFF)
def create_leave_request():
    try:
        execute_audited("INSERT INTO leave_requests (student_id,start_date,end_date,reason) VALUES (%s,%s,%s,%s)", (int(request.form["student_id"]), request.form["start_date"], request.form["end_date"], request.form["reason"].strip()), "leave_request.created", "leave_request")
        flash("Leave request submitted.", "success")
    except Exception as error:
        flash(safe_error(error), "error")
    return redirect(url_for("records.operations"))


@bp.post("/operations/leave/<int:leave_id>/review")
@roles_required(*ADMIN)
def review_leave(leave_id: int):
    status = request.form.get("status")
    if status not in {"approved", "rejected"}:
        flash("Choose approved or rejected.", "error")
    else:
        connection = get_connection()
        try:
            with cursor(dictionary=False) as db_cursor:
                db_cursor.execute("UPDATE leave_requests SET status=%s, reviewed_by=%s, reviewed_at=%s WHERE leave_request_id=%s AND status='pending'", (status, request_user_id(), datetime.now(), leave_id))
                if db_cursor.rowcount != 1:
                    raise RuleViolation("Leave request does not exist or is no longer pending.")
                db_cursor.execute("SELECT s.user_id FROM leave_requests lr JOIN students s ON s.student_id=lr.student_id WHERE lr.leave_request_id=%s", (leave_id,))
                student_user = db_cursor.fetchone()
                if student_user and student_user[0]:
                    db_cursor.execute(
                        "INSERT INTO notifications (user_id,title,body) VALUES (%s,%s,%s)",
                        (student_user[0], "Leave request " + status, "Your leave request has been " + status + "."),
                    )
                write_audit_cursor(db_cursor, "leave_request.reviewed", "leave_request", leave_id, {"status": status})
            connection.commit()
            flash(f"Leave request {status}.", "success")
        except Exception as error:
            connection.rollback()
            flash(safe_error(error), "error")
    return redirect(url_for("records.operations"))


@bp.post("/operations/complaints")
@roles_required(*ALL_STAFF)
def create_complaint():
    try:
        execute_audited("INSERT INTO complaints (student_id,room_id,category,description,priority) VALUES (%s,%s,%s,%s,%s)", (int(request.form["student_id"]), int(request.form["room_id"]) if request.form.get("room_id") else None, request.form["category"].strip(), request.form["description"].strip(), request.form.get("priority", "normal")), "complaint.created", "complaint")
        flash("Complaint recorded.", "success")
    except Exception as error:
        flash(safe_error(error), "error")
    return redirect(url_for("records.operations"))


@bp.post("/operations/maintenance-holds")
@roles_required(*ADMIN)
def create_maintenance_hold():
    try:
        room_id = int(request.form["room_id"]) if request.form.get("room_id") else None
        bed_id = int(request.form["bed_id"]) if request.form.get("bed_id") else None
        row_id = create_maintenance_hold(room_id, bed_id, date.fromisoformat(request.form["start_date"]), date.fromisoformat(request.form["end_date"]) if request.form.get("end_date") else None, request.form["reason"].strip(), request_user_id())
        flash("Maintenance hold scheduled.", "success")
    except Exception as error:
        flash(safe_error(error), "error")
    return redirect(url_for("records.operations"))


@bp.post("/operations/visitors")
@roles_required(*ADMIN)
def register_visitor():
    try:
        execute_audited("INSERT INTO visitors (student_id,visitor_name,visitor_phone,purpose,checked_in_at,recorded_by) VALUES (%s,%s,%s,%s,%s,%s)", (int(request.form["student_id"]), request.form["visitor_name"].strip(), request.form.get("visitor_phone") or None, request.form["purpose"].strip(), datetime.now(), request_user_id()), "visitor.checked_in", "visitor")
        flash("Visitor check-in recorded.", "success")
    except Exception as error:
        flash(safe_error(error), "error")
    return redirect(url_for("records.operations"))


@bp.post("/operations/visitors/<int:visitor_id>/checkout")
@roles_required(*ADMIN)
def visitor_checkout(visitor_id: int):
    try:
        execute_audited("UPDATE visitors SET checked_out_at=%s WHERE visitor_id=%s AND checked_out_at IS NULL", (datetime.now(), visitor_id), "visitor.checked_out", "visitor", visitor_id)
        flash("Visitor checkout recorded.", "success")
    except Exception as error:
        flash(safe_error(error), "error")
    return redirect(url_for("records.operations"))


@bp.post("/operations/menus")
@roles_required("Super Admin", "Mess Manager")
def create_menu():
    try:
        items = list(filter(None, (part.strip() for part in request.form.get("items", "").splitlines())))
        menu_id = create_menu(date.fromisoformat(request.form["service_date"]), request.form["meal_type"], request.form["title"].strip(), items, request_user_id(), bool(request.form.get("published")))
        flash("Menu saved.", "success")
    except Exception as error:
        flash(safe_error(error), "error")
    return redirect(url_for("records.operations"))


@bp.post("/operations/inventory/items")
@roles_required("Super Admin", "Mess Manager")
def create_inventory_item():
    try:
        execute_audited("INSERT INTO inventory_items (item_name,unit_name,reorder_level) VALUES (%s,%s,%s)", (request.form["item_name"].strip(), request.form["unit_name"].strip(), Decimal(request.form.get("reorder_level", "0"))), "inventory_item.created", "inventory_item")
        flash("Inventory item added.", "success")
    except Exception as error:
        flash(safe_error(error), "error")
    return redirect(url_for("records.operations"))


@bp.post("/operations/inventory/transactions")
@roles_required("Super Admin", "Mess Manager")
def inventory_transaction():
    try:
        quantity = Decimal(request.form["quantity_change"])
        row_id = adjust_inventory(int(request.form["inventory_item_id"]), quantity, request.form["reason"].strip(), request_user_id())
        flash("Stock movement recorded.", "success")
    except Exception as error:
        flash(safe_error(error), "error")
    return redirect(url_for("records.operations"))


@bp.post("/operations/complaints/<int:complaint_id>/status")
@roles_required(*ADMIN)
def update_complaint_status(complaint_id: int):
    status = request.form.get("status")
    if status not in {"open", "in_progress", "resolved", "closed"}:
        flash("Choose a valid complaint status.", "error")
    else:
        try:
            execute_audited("UPDATE complaints SET status=%s WHERE complaint_id=%s", (status, complaint_id), "complaint.status_changed", "complaint", complaint_id, {"status": status})
            flash("Complaint status updated.", "success")
        except Exception as error:
            flash(safe_error(error), "error")
    return redirect(url_for("records.operations"))


@bp.post("/operations/complaints/<int:complaint_id>/work-order")
@roles_required(*ADMIN)
def create_work_order(complaint_id: int):
    try:
        connection = get_connection()
        with cursor(dictionary=False) as db_cursor:
            db_cursor.execute("SELECT complaint_id,room_id,status FROM complaints WHERE complaint_id=%s FOR UPDATE", (complaint_id,))
            complaint = db_cursor.fetchone()
            if complaint is None:
                raise RuleViolation("Complaint does not exist.")
            assigned_staff_id = int(request.form["assigned_staff_id"]) if request.form.get("assigned_staff_id") else None
            if assigned_staff_id is not None:
                db_cursor.execute("SELECT staff_id FROM staff WHERE staff_id=%s AND is_active=TRUE FOR UPDATE", (assigned_staff_id,))
                if db_cursor.fetchone() is None:
                    raise RuleViolation("Selected staff member does not exist or is inactive.")
            db_cursor.execute(
                "INSERT INTO maintenance_work_orders (complaint_id,room_id,assigned_staff_id,description,status,opened_at) VALUES (%s,%s,%s,%s,'open',%s)",
                (complaint_id, complaint[1], assigned_staff_id, request.form.get("description", "").strip() or "Follow-up maintenance for complaint " + str(complaint_id), datetime.now()),
            )
            work_order_id = db_cursor.lastrowid
            db_cursor.execute("UPDATE complaints SET status='in_progress' WHERE complaint_id=%s", (complaint_id,))
            write_audit_cursor(db_cursor, "maintenance_work_order.created", "maintenance_work_order", work_order_id)
        connection.commit()
        flash("Maintenance work order created.", "success")
    except Exception as error:
        if "connection" in locals():
            connection.rollback()
        flash(safe_error(error), "error")
    return redirect(url_for("records.operations"))


@bp.post("/operations/work-orders/<int:work_order_id>/assign")
@roles_required(*ADMIN)
def assign_work_order(work_order_id: int):
    try:
        staff_id = int(request.form["staff_id"]) if request.form.get("staff_id") else None
        connection = get_connection()
        with cursor(dictionary=False) as db_cursor:
            db_cursor.execute("SELECT work_order_id FROM maintenance_work_orders WHERE work_order_id=%s FOR UPDATE", (work_order_id,))
            if db_cursor.fetchone() is None:
                raise RuleViolation("Work order does not exist.")
            if staff_id is not None:
                db_cursor.execute("SELECT staff_id FROM staff WHERE staff_id=%s AND is_active=TRUE FOR UPDATE", (staff_id,))
                if db_cursor.fetchone() is None:
                    raise RuleViolation("Selected staff member does not exist or is inactive.")
            db_cursor.execute("UPDATE maintenance_work_orders SET assigned_staff_id=%s WHERE work_order_id=%s", (staff_id, work_order_id))
            write_audit_cursor(db_cursor, "maintenance_work_order.assigned", "maintenance_work_order", work_order_id, {"staff_id": staff_id})
        connection.commit()
        flash("Work order assignment updated.", "success")
    except Exception as error:
        if "connection" in locals():
            connection.rollback()
        flash(safe_error(error), "error")
    return redirect(url_for("records.operations"))


@bp.post("/operations/work-orders/<int:work_order_id>/complete")
@roles_required(*ADMIN)
def complete_work_order(work_order_id: int):
    try:
        execute_audited("UPDATE maintenance_work_orders SET status='completed',completed_at=%s WHERE work_order_id=%s AND status IN ('open','in_progress')", (datetime.now(), work_order_id), "maintenance_work_order.completed", "maintenance_work_order", work_order_id)
        flash("Work order completed.", "success")
    except Exception as error:
        flash(safe_error(error), "error")
    return redirect(url_for("records.operations"))


@bp.post("/operations/maintenance-holds/<int:hold_id>/release")
@roles_required(*ADMIN)
def release_maintenance_hold(hold_id: int):
    try:
        execute_audited("UPDATE maintenance_holds SET status='released' WHERE hold_id=%s AND status IN ('scheduled','active')", (hold_id,), "maintenance_hold.released", "maintenance_hold", hold_id)
        flash("Maintenance hold released.", "success")
    except Exception as error:
        flash(safe_error(error), "error")
    return redirect(url_for("records.operations"))


@bp.get("/notifications")
@roles_required("Super Admin", "Warden", "Accountant", "Student", "Mess Manager")
def notifications():
    from flask import session
    rows = fetch_all("SELECT notification_id,title,body,created_at,read_at FROM notifications WHERE user_id=%s ORDER BY created_at DESC LIMIT 100", (session["user_id"],))
    return render_template("records/notifications.html", notifications=rows)


@bp.post("/notifications/<int:notification_id>/read")
@roles_required("Super Admin", "Warden", "Accountant", "Student", "Mess Manager")
def mark_notification_read(notification_id: int):
    from flask import session
    try:
        execute_audited(
            "UPDATE notifications SET read_at=%s WHERE notification_id=%s AND user_id=%s AND read_at IS NULL",
            (datetime.now(), notification_id, session["user_id"]),
            "notification.read",
            "notification",
            notification_id,
        )
        flash("Notification marked as read.", "success")
    except Exception as error:
        flash(safe_error(error), "error")
    return redirect(url_for("records.notifications"))


@bp.get("/audit")
@roles_required("Super Admin")
def audit_log():
    page = max(1, request.args.get("page", 1, type=int))
    rows = fetch_all(
        "SELECT a.action,a.entity_type,a.entity_id,a.details,a.occurred_at,u.username "
        "FROM audit_logs a LEFT JOIN users u ON u.user_id=a.user_id "
        "ORDER BY a.occurred_at DESC LIMIT 100 OFFSET %s", ((page - 1) * 100,),
    )
    return render_template("records/audit.html", logs=rows, page=page)


@bp.get("/reports/finance.csv")
@roles_required(*FINANCE)
def finance_csv():
    start = request.args.get("start", "")
    end = request.args.get("end", "")
    conditions = []
    params: list[str] = []
    try:
        if start:
            date.fromisoformat(start)
            conditions.append("p.received_at >= %s")
            params.append(start)
        if end:
            date.fromisoformat(end)
            conditions.append("p.received_at < DATE_ADD(%s, INTERVAL 1 DAY)")
            params.append(end)
        if start and end and end < start:
            raise ValueError("End date precedes start date.")
    except ValueError:
        flash("Enter valid report dates, with end on or after start.", "error")
        return redirect(url_for("records.finance"))
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    rows = fetch_all(
        "SELECT p.receipt_number,p.received_at,s.student_number,p.amount,p.method,p.reference,p.status "
        "FROM payments p JOIN students s ON s.student_id=p.student_id " + where + " ORDER BY p.received_at DESC",
        tuple(params),
    )
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(rows[0].keys()) if rows else ["receipt_number", "received_at", "student_number", "amount", "method", "reference", "status"])
    writer.writeheader()
    writer.writerows(csv_safe_rows(rows))
    return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=hostel-payments.csv"})