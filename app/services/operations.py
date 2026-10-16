from datetime import datetime
from decimal import Decimal

from app.db import cursor, get_connection, write_audit_record
from app.services.hostel import RuleViolation


def create_menu(service_date, meal_type: str, title: str, items: list[str], created_by: int, published: bool = False) -> int:
    connection = get_connection()
    try:
        with cursor(dictionary=False) as db_cursor:
            db_cursor.execute(
                "INSERT INTO menus (service_date,meal_type,title,published,created_by) VALUES (%s,%s,%s,%s,%s)",
                (service_date, meal_type, title, published, created_by),
            )
            menu_id = db_cursor.lastrowid
            for item in items:
                db_cursor.execute("INSERT INTO menu_items (menu_id,item_name) VALUES (%s,%s)", (menu_id, item))
            write_audit_record(db_cursor, created_by, "menu.created", "menu", menu_id)
        connection.commit()
        return menu_id
    except Exception:
        connection.rollback()
        raise


def adjust_inventory(item_id: int, quantity_change: Decimal, reason: str, recorded_by: int) -> int:
    if quantity_change == 0:
        raise RuleViolation("Stock change cannot be zero.")
    connection = get_connection()
    try:
        with cursor(dictionary=False) as db_cursor:
            db_cursor.execute("SELECT inventory_item_id FROM inventory_items WHERE inventory_item_id=%s AND is_active=TRUE FOR UPDATE", (item_id,))
            if db_cursor.fetchone() is None:
                raise RuleViolation("Inventory item does not exist or is inactive.")
            db_cursor.execute("SELECT COALESCE(SUM(quantity_change),0) FROM inventory_transactions WHERE inventory_item_id=%s", (item_id,))
            current_stock = db_cursor.fetchone()[0]
            if current_stock + quantity_change < 0:
                raise RuleViolation("This stock withdrawal would make the inventory negative.")
            db_cursor.execute("SELECT staff_id FROM staff WHERE user_id=%s", (recorded_by,))
            staff = db_cursor.fetchone()
            staff_id = staff[0] if staff else None
            db_cursor.execute(
                "INSERT INTO inventory_transactions (inventory_item_id,staff_id,quantity_change,reason,occurred_at) VALUES (%s,%s,%s,%s,%s)",
                (item_id, staff_id, quantity_change, reason, datetime.now()),
            )
            transaction_id = db_cursor.lastrowid
            write_audit_record(db_cursor, recorded_by, "inventory.stock_changed", "inventory_transaction", transaction_id)
        connection.commit()
        return transaction_id
    except Exception:
        connection.rollback()
        raise


def record_meal_attendance(menu_id: int, student_id: int, actor_user_id: int | None = None) -> int:
    connection = get_connection()
    try:
        with cursor(dictionary=False) as db_cursor:
            db_cursor.execute(
                "SELECT menu_id FROM menus WHERE menu_id=%s AND published=TRUE AND service_date=CURRENT_DATE FOR UPDATE",
                (menu_id,),
            )
            if db_cursor.fetchone() is None:
                raise RuleViolation("Only a published menu for today can be marked attended.")
            db_cursor.execute(
                "INSERT INTO meal_attendance (menu_id,student_id,attended_at) VALUES (%s,%s,NOW())",
                (menu_id, student_id),
            )
            attendance_id = db_cursor.lastrowid
            write_audit_record(db_cursor, actor_user_id, "meal_attendance.recorded", "meal_attendance", attendance_id)
        connection.commit()
        return attendance_id
    except Exception:
        connection.rollback()
        raise