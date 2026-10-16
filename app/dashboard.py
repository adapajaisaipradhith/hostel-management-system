from datetime import date

from flask import Blueprint, flash, g, render_template, request, session

from app.auth import login_required
from app.db import fetch_all, fetch_one

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.get("/")
@login_required
def index():
    selected_date = date.today()
    requested_date = request.args.get("date")
    if requested_date:
        try:
            selected_date = date.fromisoformat(requested_date)
        except ValueError:
            flash("Enter a valid dashboard date.", "error")
    if "Student" in g.current_roles and not g.current_roles.intersection({"Super Admin", "Warden", "Accountant", "Mess Manager"}):
        student = fetch_one("SELECT student_id,student_number,first_name,last_name FROM students WHERE user_id=%s AND is_active=TRUE", (session["user_id"],))
        personal = None
        if student:
            personal = {
                "allocation": fetch_one(
                    "SELECT hb.block_name,r.room_number,COALESCE(a.bed_label_snapshot,b.bed_label) AS bed_label,a.planned_start_date,a.planned_end_date,a.checked_in_at "
                    "FROM allocations a JOIN beds b ON b.bed_id=a.bed_id JOIN rooms r ON r.room_id=b.room_id "
                    "JOIN hostel_blocks hb ON hb.block_id=r.block_id WHERE a.student_id=%s AND a.status IN ('reserved','checked_in') "
                    "ORDER BY a.planned_start_date LIMIT 1", (student["student_id"],)),
                "balance": fetch_one("SELECT COALESCE(SUM(outstanding_amount),0.00) AS amount FROM v_invoice_balances WHERE student_id=%s AND status='posted'", (student["student_id"],))["amount"],
                "notifications": fetch_all("SELECT title,body,created_at FROM notifications WHERE user_id=%s ORDER BY created_at DESC LIMIT 6", (session["user_id"],)),
                "leave_requests": fetch_all("SELECT start_date,end_date,reason,status FROM leave_requests WHERE student_id=%s ORDER BY created_at DESC LIMIT 10", (student["student_id"],)),
                "attendance": fetch_all("SELECT attendance_date,session_name,status FROM attendance WHERE student_id=%s ORDER BY attendance_date DESC LIMIT 10", (student["student_id"],)),
                "complaints": fetch_all("SELECT category,description,status,created_at FROM complaints WHERE student_id=%s ORDER BY created_at DESC LIMIT 10", (student["student_id"],)),
                "menus": fetch_all("SELECT menu_id,service_date,meal_type,title FROM menus WHERE published=TRUE AND service_date>=CURRENT_DATE ORDER BY service_date,meal_type LIMIT 12"),
            }
        return render_template("dashboard/student.html", student=student, personal=personal)

    summary = fetch_one(
        "SELECT COUNT(*) AS total_beds, "
        "COALESCE(SUM(a.physical_occupied),0) AS physical_occupied, "
        "COALESCE(SUM(a.scheduled_today),0) AS scheduled_today, "
        "COALESCE(SUM(a.maintenance_today),0) AS maintenance_today, "
        "COALESCE(SUM(CASE WHEN a.physical_occupied=0 AND a.scheduled_today=0 AND a.maintenance_today=0 THEN 1 ELSE 0 END),0) AS available_today "
        "FROM beds b JOIN rooms r ON r.room_id=b.room_id JOIN hostel_blocks h ON h.block_id=r.block_id "
        "LEFT JOIN (SELECT b2.bed_id, "
        "EXISTS(SELECT 1 FROM allocations x WHERE x.bed_id=b2.bed_id AND x.checked_in_at IS NOT NULL AND x.checked_out_at IS NULL) AS physical_occupied, "
        "EXISTS(SELECT 1 FROM allocations x WHERE x.bed_id=b2.bed_id AND x.status='reserved' AND x.planned_start_date<=%s AND (x.effective_end_date IS NULL OR x.effective_end_date>%s)) AS scheduled_today, "
        "EXISTS(SELECT 1 FROM maintenance_holds m WHERE (m.bed_id=b2.bed_id OR m.room_id=b2.room_id) AND m.status IN ('scheduled','active') AND m.start_date<=%s AND (m.end_date IS NULL OR m.end_date>%s)) AS maintenance_today "
        "FROM beds b2) a ON a.bed_id=b.bed_id "
        "WHERE b.is_active=TRUE AND r.is_active=TRUE AND h.is_active=TRUE",
        (selected_date, selected_date, selected_date, selected_date),
    )
    summary["reserved_future"] = fetch_one("SELECT COUNT(DISTINCT bed_id) AS amount FROM allocations WHERE status='reserved' AND planned_start_date>%s", (selected_date,))["amount"]
    finance_summary = fetch_one("SELECT COALESCE(SUM(outstanding_amount),0.00) AS outstanding, COUNT(*) AS invoices FROM v_invoice_balances WHERE status='posted'") if g.current_roles.intersection({"Super Admin", "Accountant"}) else None
    attendance_summary = fetch_one("SELECT COUNT(*) AS marked, COALESCE(SUM(status='present'),0) AS present FROM attendance WHERE attendance_date=%s", (selected_date,)) if g.current_roles.intersection({"Super Admin", "Warden"}) else None
    pending_leave = fetch_one("SELECT COUNT(*) AS total FROM leave_requests WHERE status='pending'")["total"] if g.current_roles.intersection({"Super Admin", "Warden"}) else 0
    open_complaints = fetch_one("SELECT COUNT(*) AS total FROM complaints WHERE status IN ('open','in_progress')")["total"] if g.current_roles.intersection({"Super Admin", "Warden"}) else 0
    block_rows = fetch_all(
        "SELECT hb.block_name,COUNT(DISTINCT b.bed_id) AS total_beds, "
        "COUNT(DISTINCT CASE WHEN a.checked_in_at IS NOT NULL AND a.checked_out_at IS NULL THEN b.bed_id END) AS occupied "
        "FROM hostel_blocks hb JOIN rooms r ON r.block_id=hb.block_id JOIN beds b ON b.room_id=r.room_id "
        "LEFT JOIN allocations a ON a.bed_id=b.bed_id WHERE hb.is_active=TRUE AND r.is_active=TRUE AND b.is_active=TRUE "
        "GROUP BY hb.block_id,hb.block_name ORDER BY hb.block_name"
    ) if g.current_roles.intersection({"Super Admin", "Warden"}) else []
    recent_audit = fetch_all(
        "SELECT al.action, al.entity_type, al.occurred_at, u.username FROM audit_logs al "
        "LEFT JOIN users u ON u.user_id=al.user_id ORDER BY al.occurred_at DESC LIMIT 8"
    ) if g.current_roles.intersection({"Super Admin", "Warden"}) else []
    stock_alerts = fetch_all(
        "SELECT i.item_name,i.unit_name,i.reorder_level,COALESCE(SUM(t.quantity_change),0) AS stock "
        "FROM inventory_items i LEFT JOIN inventory_transactions t ON t.inventory_item_id=i.inventory_item_id "
        "WHERE i.is_active=TRUE GROUP BY i.inventory_item_id HAVING stock<=i.reorder_level ORDER BY i.item_name LIMIT 8"
    ) if "Mess Manager" in g.current_roles or "Super Admin" in g.current_roles else []
    chart = {"labels": [row["block_name"] for row in block_rows], "occupied": [int(row["occupied"]) for row in block_rows], "beds": [int(row["total_beds"]) for row in block_rows]}
    return render_template("dashboard/index.html", summary=summary, recent_audit=recent_audit, finance_summary=finance_summary, attendance_summary=attendance_summary, pending_leave=pending_leave, open_complaints=open_complaints, stock_alerts=stock_alerts, chart=chart, selected_date=selected_date.isoformat())