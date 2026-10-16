from datetime import datetime
from decimal import Decimal

from app.db import cursor, get_connection, write_audit_record
from app.services.hostel import RuleViolation


def create_invoice(student_id: int, invoice_number: str, issue_date, due_date, fee_structure_id: int, description: str, allocation_id: int | None, created_by: int) -> int:
    if due_date < issue_date:
        raise RuleViolation("Invoice due date cannot precede its issue date.")
    connection = get_connection()
    try:
        with cursor(dictionary=False) as db_cursor:
            db_cursor.execute("SELECT student_id FROM students WHERE student_id = %s FOR UPDATE", (student_id,))
            if db_cursor.fetchone() is None:
                raise RuleViolation("Student does not exist.")
            if allocation_id is not None:
                db_cursor.execute(
                    "SELECT allocation_id FROM allocations WHERE allocation_id=%s AND student_id=%s FOR UPDATE",
                    (allocation_id, student_id),
                )
                if db_cursor.fetchone() is None:
                    raise RuleViolation("The allocation does not belong to this student.")
            db_cursor.execute(
                "SELECT fs.fee_category_id,fs.amount,fc.category_name FROM fee_structures fs "
                "JOIN fee_categories fc ON fc.fee_category_id=fs.fee_category_id "
                "WHERE fs.fee_structure_id=%s AND fs.is_active=TRUE AND fc.is_active=TRUE "
                "AND fs.effective_start<=%s AND (fs.effective_end IS NULL OR fs.effective_end>=%s) FOR UPDATE",
                (fee_structure_id, issue_date, issue_date),
            )
            fee = db_cursor.fetchone()
            if fee is None:
                raise RuleViolation("Fee structure is inactive or not effective on the invoice issue date.")
            db_cursor.execute(
                "INSERT INTO invoices (invoice_number, student_id, allocation_id, issue_date, due_date, status, created_by) "
                "VALUES (%s, %s, %s, %s, %s, 'draft', %s)",
                (invoice_number, student_id, allocation_id, issue_date, due_date, created_by),
            )
            invoice_id = db_cursor.lastrowid
            db_cursor.execute(
                "INSERT INTO invoice_items (invoice_id,fee_category_id,category,description,quantity,unit_amount) "
                "VALUES (%s,%s,%s,%s,1,%s)",
                (invoice_id, fee[0], fee[2], description, fee[1]),
            )
            db_cursor.execute("UPDATE invoices SET status = 'posted' WHERE invoice_id = %s", (invoice_id,))
            write_audit_record(db_cursor, created_by, "invoice.posted", "invoice", invoice_id)
        connection.commit()
        return invoice_id
    except Exception:
        connection.rollback()
        raise


def record_payment(student_id: int, invoice_id: int, receipt_number: str, amount: Decimal, method: str, reference: str | None, recorded_by: int) -> int:
    if amount <= 0:
        raise RuleViolation("Payment amount must be greater than zero.")
    connection = get_connection()
    try:
        with cursor(dictionary=False) as db_cursor:
            db_cursor.execute("SELECT student_id FROM students WHERE student_id = %s FOR UPDATE", (student_id,))
            if db_cursor.fetchone() is None:
                raise RuleViolation("Student does not exist.")
            db_cursor.execute(
                "SELECT invoice_id FROM invoices WHERE invoice_id = %s AND student_id = %s "
                "AND status = 'posted' FOR UPDATE",
                (invoice_id, student_id),
            )
            if db_cursor.fetchone() is None:
                raise RuleViolation("That posted invoice does not belong to this student.")
            db_cursor.execute(
                "SELECT outstanding_amount FROM v_invoice_balances WHERE invoice_id = %s",
                (invoice_id,),
            )
            balance = db_cursor.fetchone()[0]
            if amount > balance:
                raise RuleViolation("Payment cannot exceed the invoice's outstanding balance.")

            db_cursor.execute(
                "INSERT INTO payments (receipt_number, student_id, amount, received_at, method, reference, status, recorded_by) "
                "VALUES (%s, %s, %s, %s, %s, %s, 'successful', %s)",
                (receipt_number, student_id, amount, datetime.now(), method, reference, recorded_by),
            )
            payment_id = db_cursor.lastrowid
            db_cursor.execute(
                "INSERT INTO payment_applications (payment_id, invoice_id, amount_applied) VALUES (%s, %s, %s)",
                (payment_id, invoice_id, amount),
            )
            write_audit_record(db_cursor, recorded_by, "payment.recorded", "payment", payment_id, {"receipt": receipt_number})
        connection.commit()
        return payment_id
    except Exception:
        connection.rollback()
        raise


def create_fee_structure(category_id: int, amount: Decimal, frequency: str, effective_start, effective_end, actor_user_id: int | None = None) -> int:
    if amount < 0 or (effective_end is not None and effective_end < effective_start):
        raise RuleViolation("Fee amount or effective dates are invalid.")
    connection = get_connection()
    try:
        with cursor(dictionary=False) as db_cursor:
            db_cursor.execute("SELECT fee_category_id FROM fee_categories WHERE fee_category_id=%s AND is_active=TRUE FOR UPDATE", (category_id,))
            if db_cursor.fetchone() is None:
                raise RuleViolation("Fee category does not exist or is inactive.")
            db_cursor.execute(
                "SELECT fee_structure_id FROM fee_structures WHERE fee_category_id=%s AND frequency=%s AND is_active=TRUE "
                "AND (%s IS NULL OR effective_start<=%s) AND (effective_end IS NULL OR effective_end>=%s) LIMIT 1 FOR UPDATE",
                (category_id, frequency, effective_end, effective_end, effective_start),
            )
            if db_cursor.fetchone() is not None:
                raise RuleViolation("That fee category already has an active structure for an overlapping effective period.")
            db_cursor.execute(
                "INSERT INTO fee_structures (fee_category_id,amount,frequency,effective_start,effective_end) VALUES (%s,%s,%s,%s,%s)",
                (category_id, amount, frequency, effective_start, effective_end),
            )
            fee_structure_id = db_cursor.lastrowid
            write_audit_record(db_cursor, actor_user_id, "fee_structure.created", "fee_structure", fee_structure_id)
        connection.commit()
        return fee_structure_id
    except Exception:
        connection.rollback()
        raise