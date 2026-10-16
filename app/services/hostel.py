from datetime import date, datetime

from mysql.connector import Error

from app.db import cursor, get_connection, write_audit_record


class RuleViolation(ValueError):
    pass


def create_reservation(student_id: int, bed_id: int, start_date: date, end_date: date | None, created_by: int | None) -> int:
    if end_date is not None and end_date <= start_date:
        raise RuleViolation("Planned checkout must be after check-in.")

    connection = get_connection()
    try:
        with cursor(dictionary=False) as db_cursor:
            # These parent-row locks serialize booking attempts for a student and a bed.
            db_cursor.execute("SELECT student_id FROM students WHERE student_id = %s AND is_active = TRUE FOR UPDATE", (student_id,))
            if db_cursor.fetchone() is None:
                raise RuleViolation("Student does not exist.")
            db_cursor.execute(
                "SELECT b.bed_id,b.bed_label FROM beds b JOIN rooms r ON r.room_id=b.room_id "
                "JOIN hostel_blocks h ON h.block_id=r.block_id "
                "WHERE b.bed_id=%s AND b.is_active=TRUE AND r.is_active=TRUE AND h.is_active=TRUE FOR UPDATE",
                (bed_id,),
            )
            bed = db_cursor.fetchone()
            if bed is None:
                raise RuleViolation("Bed does not exist or is inactive.")
            bed_label_snapshot = bed[1]
            db_cursor.execute("SELECT r.room_id FROM rooms r JOIN beds b ON b.room_id=r.room_id WHERE b.bed_id=%s FOR UPDATE", (bed_id,))
            db_cursor.fetchone()

            overlap_sql = (
                "SELECT allocation_id FROM allocations "
                "WHERE {owner_column} = %s AND status <> 'cancelled' "
                "AND (%s IS NULL OR planned_start_date < %s) "
                "AND (effective_end_date IS NULL OR effective_end_date > %s) LIMIT 1 FOR UPDATE"
            )
            for owner_column, owner_id in (("student_id", student_id), ("bed_id", bed_id)):
                db_cursor.execute(
                    overlap_sql.format(owner_column=owner_column),
                    (owner_id, end_date, end_date, start_date),
                )
                if db_cursor.fetchone() is not None:
                    raise RuleViolation(f"The {owner_column.replace('_id', '')} has an overlapping allocation.")

            db_cursor.execute(
                "SELECT hold_id FROM maintenance_holds "
                "WHERE status IN ('scheduled', 'active') "
                "AND (bed_id = %s OR room_id = (SELECT room_id FROM beds WHERE bed_id = %s)) "
                "AND (%s IS NULL OR start_date < %s) "
                "AND (end_date IS NULL OR end_date > %s) LIMIT 1 FOR UPDATE",
                (bed_id, bed_id, end_date, end_date, start_date),
            )
            if db_cursor.fetchone() is not None:
                raise RuleViolation("The bed has a maintenance hold during that period.")

            db_cursor.execute(
                "INSERT INTO allocations "
                "(student_id, bed_id, bed_label_snapshot, planned_start_date, planned_end_date, status, created_by) "
                "VALUES (%s, %s, %s, %s, %s, 'reserved', %s)",
                (student_id, bed_id, bed_label_snapshot, start_date, end_date, created_by),
            )
            allocation_id = db_cursor.lastrowid
            write_audit_record(db_cursor, created_by, "allocation.reserved", "allocation", allocation_id)
        connection.commit()
        return allocation_id
    except Exception:
        connection.rollback()
        raise


def check_in(allocation_id: int, actor_user_id: int | None = None) -> None:
    connection = get_connection()
    try:
        with cursor(dictionary=False) as db_cursor:
            db_cursor.execute(
                "SELECT bed_id FROM allocations WHERE allocation_id = %s",
                (allocation_id,),
            )
            allocation_ref = db_cursor.fetchone()
            if allocation_ref is None:
                raise RuleViolation("Reservation does not exist.")
            bed_id = allocation_ref[0]
            db_cursor.execute(
                "SELECT r.room_id FROM rooms r JOIN beds b ON b.room_id=r.room_id "
                "WHERE b.bed_id=%s FOR UPDATE",
                (bed_id,),
            )
            if db_cursor.fetchone() is None:
                raise RuleViolation("The reserved bed no longer exists.")
            db_cursor.execute(
                "SELECT student_id, bed_id, planned_start_date, planned_end_date, status "
                "FROM allocations WHERE allocation_id = %s FOR UPDATE",
                (allocation_id,),
            )
            allocation = db_cursor.fetchone()
            if allocation is None or allocation[4] != "reserved":
                raise RuleViolation("Only a reservation can be checked in.")
            student_id, bed_id, start_date, end_date, _status = allocation
            today = date.today()
            if today < start_date or (end_date is not None and today >= end_date):
                raise RuleViolation("Today is outside the reservation period.")
            db_cursor.execute(
                "SELECT allocation_id FROM allocations WHERE bed_id = %s "
                "AND checked_in_at IS NOT NULL AND checked_out_at IS NULL "
                "AND allocation_id <> %s FOR UPDATE",
                (bed_id, allocation_id),
            )
            if db_cursor.fetchone() is not None:
                raise RuleViolation("The bed is still physically occupied; actual checkout is required first.")
            db_cursor.execute(
                "SELECT allocation_id FROM allocations WHERE student_id = %s "
                "AND checked_in_at IS NOT NULL AND checked_out_at IS NULL "
                "AND allocation_id <> %s FOR UPDATE",
                (student_id, allocation_id),
            )
            if db_cursor.fetchone() is not None:
                raise RuleViolation("The student already has an active physical allocation.")
            db_cursor.execute(
                "SELECT hold_id FROM maintenance_holds "
                "WHERE status IN ('scheduled', 'active') "
                "AND (bed_id = %s OR room_id = (SELECT room_id FROM beds WHERE bed_id = %s)) "
                "AND start_date <= CURRENT_DATE AND (end_date IS NULL OR end_date > CURRENT_DATE) "
                "LIMIT 1 FOR UPDATE",
                (bed_id, bed_id),
            )
            if db_cursor.fetchone() is not None:
                raise RuleViolation("The bed is under an active maintenance hold.")
            db_cursor.execute(
                "UPDATE allocations SET checked_in_at = %s, status = 'checked_in' WHERE allocation_id = %s",
                (datetime.now(), allocation_id),
            )
            write_audit_record(db_cursor, actor_user_id, "allocation.checked_in", "allocation", allocation_id)
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def create_maintenance_hold(room_id: int | None, bed_id: int | None, start_date: date, end_date: date | None, reason: str, created_by: int) -> int:
    if (room_id is None) == (bed_id is None):
        raise RuleViolation("Choose exactly one maintenance scope: a room or a bed.")
    if end_date is not None and end_date <= start_date:
        raise RuleViolation("Maintenance hold end date must be after its start date.")
    connection = get_connection()
    try:
        with cursor(dictionary=False) as db_cursor:
            if room_id is not None:
                db_cursor.execute("SELECT room_id FROM rooms WHERE room_id=%s AND is_active=TRUE FOR UPDATE", (room_id,))
            else:
                db_cursor.execute("SELECT bed_id FROM beds WHERE bed_id=%s AND is_active=TRUE FOR UPDATE", (bed_id,))
            if db_cursor.fetchone() is None:
                raise RuleViolation("Selected maintenance location does not exist or is inactive.")
            db_cursor.execute(
                "SELECT a.allocation_id FROM allocations a JOIN beds b ON b.bed_id=a.bed_id "
                "WHERE (a.bed_id=%s OR (%s IS NOT NULL AND b.room_id=%s)) "
                "AND a.status <> 'cancelled' AND (%s IS NULL OR a.planned_start_date < %s) "
                "AND (a.effective_end_date IS NULL OR a.effective_end_date > %s) LIMIT 1 FOR UPDATE",
                (bed_id, room_id, room_id, end_date, end_date, start_date),
            )
            if db_cursor.fetchone() is not None:
                raise RuleViolation("The maintenance period conflicts with an existing reservation or allocation.")
            db_cursor.execute(
                "INSERT INTO maintenance_holds (room_id,bed_id,start_date,end_date,reason,status,created_by) "
                "VALUES (%s,%s,%s,%s,%s,'scheduled',%s)",
                (room_id, bed_id, start_date, end_date, reason, created_by),
            )
            hold_id = db_cursor.lastrowid
            write_audit_record(db_cursor, created_by, "maintenance_hold.created", "maintenance_hold", hold_id)
        connection.commit()
        return hold_id
    except Exception:
        connection.rollback()
        raise


def checkout(allocation_id: int, decided_by: int, note: str | None = None) -> bool:
    connection = get_connection()
    try:
        with cursor(dictionary=False) as db_cursor:
            db_cursor.execute(
                "SELECT student_id FROM allocations WHERE allocation_id = %s",
                (allocation_id,),
            )
            allocation_ref = db_cursor.fetchone()
            if allocation_ref is None:
                raise RuleViolation("Allocation does not exist.")
            student_id = allocation_ref[0]
            db_cursor.execute("SELECT student_id FROM students WHERE student_id=%s FOR UPDATE", (student_id,))
            if db_cursor.fetchone() is None:
                raise RuleViolation("Student record no longer exists.")
            db_cursor.execute(
                "SELECT student_id, checked_in_at, checked_out_at, status "
                "FROM allocations WHERE allocation_id = %s FOR UPDATE",
                (allocation_id,),
            )
            allocation = db_cursor.fetchone()
            if allocation is None or allocation[0] != student_id or allocation[1] is None or allocation[2] is not None:
                raise RuleViolation("Only a currently checked-in allocation can be checked out.")

            # Lock the student's posted invoices before calculating clearance, matching payment workflow locks.
            db_cursor.execute(
                "SELECT invoice_id FROM invoices WHERE student_id = %s AND status = 'posted' FOR UPDATE",
                (student_id,),
            )
            db_cursor.fetchall()
            db_cursor.execute(
                "SELECT COALESCE(SUM(outstanding_amount), 0.00) FROM v_invoice_balances "
                "WHERE student_id = %s AND status = 'posted'",
                (student_id,),
            )
            balance = db_cursor.fetchone()[0]
            decision = "cleared" if balance <= 0 else "blocked"
            db_cursor.execute(
                "INSERT INTO checkout_clearances "
                "(allocation_id, balance_at_decision, decision, decided_by, decided_at, note) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (allocation_id, balance, decision, decided_by, datetime.now(), note),
            )
            if decision == "cleared":
                db_cursor.execute(
                    "UPDATE allocations SET checked_out_at = %s, status = 'completed' "
                    "WHERE allocation_id = %s",
                    (datetime.now(), allocation_id),
                )
            write_audit_record(
                db_cursor,
                decided_by,
                "allocation.checked_out" if decision == "cleared" else "allocation.checkout_blocked",
                "allocation",
                allocation_id,
                {"balance": str(balance), "decision": decision},
            )
        connection.commit()
        return decision == "cleared"
    except Exception:
        connection.rollback()
        raise