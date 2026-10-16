CREATE TABLE roles (
    role_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    role_name VARCHAR(40) NOT NULL UNIQUE,
    description VARCHAR(255) NOT NULL DEFAULT ''
) ENGINE=InnoDB;

INSERT INTO roles (role_name, description) VALUES
    ('Super Admin', 'Full system configuration and access'),
    ('Warden', 'Hostel occupancy, student welfare, and approvals'),
    ('Accountant', 'Invoices, payments, balances, and financial reports'),
    ('Student', 'Personal profile, requests, and hostel information'),
    ('Mess Manager', 'Menus, meal records, and mess inventory');

CREATE TABLE users (
    user_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    username VARCHAR(80) NOT NULL UNIQUE,
    email VARCHAR(254) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB;

CREATE TABLE user_roles (
    user_id BIGINT UNSIGNED NOT NULL,
    role_id BIGINT UNSIGNED NOT NULL,
    assigned_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, role_id),
    CONSTRAINT fk_user_roles_user FOREIGN KEY (user_id) REFERENCES users(user_id),
    CONSTRAINT fk_user_roles_role FOREIGN KEY (role_id) REFERENCES roles(role_id)
) ENGINE=InnoDB;

CREATE TABLE students (
    student_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    user_id BIGINT UNSIGNED NULL UNIQUE,
    student_number VARCHAR(40) NOT NULL UNIQUE,
    first_name VARCHAR(80) NOT NULL,
    last_name VARCHAR(80) NOT NULL,
    email VARCHAR(254) NOT NULL UNIQUE,
    phone VARCHAR(30) NULL,
    guardian_name VARCHAR(160) NULL,
    guardian_phone VARCHAR(30) NULL,
    emergency_contact_name VARCHAR(160) NULL,
    emergency_contact_phone VARCHAR(30) NULL,
    date_of_birth DATE NULL,
    admission_date DATE NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_students_name (last_name, first_name),
    CONSTRAINT fk_students_user FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE SET NULL
) ENGINE=InnoDB;

CREATE TABLE staff (
    staff_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    user_id BIGINT UNSIGNED NULL UNIQUE,
    staff_number VARCHAR(40) NOT NULL UNIQUE,
    first_name VARCHAR(80) NOT NULL,
    last_name VARCHAR(80) NOT NULL,
    job_title VARCHAR(100) NOT NULL,
    phone VARCHAR(30) NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_staff_user FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE SET NULL
) ENGINE=InnoDB;

CREATE TABLE hostel_blocks (
    block_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    block_name VARCHAR(100) NOT NULL UNIQUE,
    address VARCHAR(255) NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE
) ENGINE=InnoDB;

CREATE TABLE rooms (
    room_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    block_id BIGINT UNSIGNED NOT NULL,
    floor_number SMALLINT NOT NULL DEFAULT 0,
    room_number VARCHAR(30) NOT NULL,
    room_type VARCHAR(40) NOT NULL DEFAULT 'standard',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE KEY uq_rooms_block_number (block_id, room_number),
    CONSTRAINT fk_rooms_block FOREIGN KEY (block_id) REFERENCES hostel_blocks(block_id) ON DELETE RESTRICT
) ENGINE=InnoDB;

CREATE TABLE beds (
    bed_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    room_id BIGINT UNSIGNED NOT NULL,
    bed_label VARCHAR(30) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE KEY uq_beds_room_label (room_id, bed_label),
    CONSTRAINT fk_beds_room FOREIGN KEY (room_id) REFERENCES rooms(room_id) ON DELETE RESTRICT
) ENGINE=InnoDB;

CREATE TABLE allocations (
    allocation_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    student_id BIGINT UNSIGNED NOT NULL,
    bed_id BIGINT UNSIGNED NOT NULL,
    planned_start_date DATE NOT NULL,
    planned_end_date DATE NULL,
    checked_in_at DATETIME NULL,
    checked_out_at DATETIME NULL,
    effective_end_date DATE GENERATED ALWAYS AS (COALESCE(DATE(checked_out_at), planned_end_date)) STORED,
    status ENUM('reserved', 'checked_in', 'completed', 'cancelled') NOT NULL DEFAULT 'reserved',
    created_by BIGINT UNSIGNED NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    physical_student_id BIGINT UNSIGNED GENERATED ALWAYS AS
        (CASE WHEN checked_in_at IS NOT NULL AND checked_out_at IS NULL THEN student_id ELSE NULL END) STORED,
    physical_bed_id BIGINT UNSIGNED GENERATED ALWAYS AS
        (CASE WHEN checked_in_at IS NOT NULL AND checked_out_at IS NULL THEN bed_id ELSE NULL END) STORED,
    UNIQUE KEY uq_allocation_one_physical_stay_student (physical_student_id),
    UNIQUE KEY uq_allocation_one_physical_stay_bed (physical_bed_id),
    INDEX idx_allocations_bed_dates (bed_id, planned_start_date, effective_end_date),
    INDEX idx_allocations_student_dates (student_id, planned_start_date, effective_end_date),
    CONSTRAINT chk_allocation_planned_dates CHECK
        (planned_end_date IS NULL OR planned_end_date > planned_start_date),
    CONSTRAINT chk_allocation_actual_dates CHECK
        (checked_out_at IS NULL OR (checked_in_at IS NOT NULL AND checked_out_at >= checked_in_at)),
    CONSTRAINT chk_allocation_state CHECK
        ((status = 'checked_in' AND checked_in_at IS NOT NULL AND checked_out_at IS NULL)
         OR (status = 'completed' AND checked_out_at IS NOT NULL)
         OR (status IN ('reserved', 'cancelled') AND checked_in_at IS NULL AND checked_out_at IS NULL)),
    CONSTRAINT fk_allocations_student FOREIGN KEY (student_id) REFERENCES students(student_id) ON DELETE RESTRICT,
    CONSTRAINT fk_allocations_bed FOREIGN KEY (bed_id) REFERENCES beds(bed_id) ON DELETE RESTRICT,
    CONSTRAINT fk_allocations_creator FOREIGN KEY (created_by) REFERENCES users(user_id) ON DELETE SET NULL
) ENGINE=InnoDB;

CREATE TABLE maintenance_holds (
    hold_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    room_id BIGINT UNSIGNED NULL,
    bed_id BIGINT UNSIGNED NULL,
    start_date DATE NOT NULL,
    end_date DATE NULL,
    reason VARCHAR(500) NOT NULL,
    status ENUM('scheduled', 'active', 'released', 'cancelled') NOT NULL DEFAULT 'scheduled',
    created_by BIGINT UNSIGNED NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_holds_room_dates (room_id, start_date, end_date, status),
    INDEX idx_holds_bed_dates (bed_id, start_date, end_date, status),
    CONSTRAINT chk_hold_exact_scope CHECK ((room_id IS NULL) <> (bed_id IS NULL)),
    CONSTRAINT chk_hold_dates CHECK (end_date IS NULL OR end_date > start_date),
    CONSTRAINT fk_holds_room FOREIGN KEY (room_id) REFERENCES rooms(room_id) ON DELETE RESTRICT,
    CONSTRAINT fk_holds_bed FOREIGN KEY (bed_id) REFERENCES beds(bed_id) ON DELETE RESTRICT,
    CONSTRAINT fk_holds_creator FOREIGN KEY (created_by) REFERENCES users(user_id) ON DELETE SET NULL
) ENGINE=InnoDB;

CREATE TABLE invoices (
    invoice_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    invoice_number VARCHAR(50) NOT NULL UNIQUE,
    student_id BIGINT UNSIGNED NOT NULL,
    allocation_id BIGINT UNSIGNED NULL,
    issue_date DATE NOT NULL,
    due_date DATE NOT NULL,
    status ENUM('draft', 'posted', 'void') NOT NULL DEFAULT 'draft',
    created_by BIGINT UNSIGNED NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_invoices_student_due (student_id, status, due_date),
    CONSTRAINT chk_invoice_dates CHECK (due_date >= issue_date),
    CONSTRAINT fk_invoices_student FOREIGN KEY (student_id) REFERENCES students(student_id) ON DELETE RESTRICT,
    CONSTRAINT fk_invoices_allocation FOREIGN KEY (allocation_id) REFERENCES allocations(allocation_id) ON DELETE RESTRICT,
    CONSTRAINT fk_invoices_creator FOREIGN KEY (created_by) REFERENCES users(user_id) ON DELETE SET NULL
) ENGINE=InnoDB;

CREATE TABLE fee_categories (
    fee_category_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    category_name VARCHAR(80) NOT NULL UNIQUE,
    description VARCHAR(255) NOT NULL DEFAULT '',
    is_active BOOLEAN NOT NULL DEFAULT TRUE
) ENGINE=InnoDB;

CREATE TABLE fee_structures (
    fee_structure_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    fee_category_id BIGINT UNSIGNED NOT NULL,
    amount DECIMAL(12, 2) NOT NULL,
    frequency ENUM('one_time', 'monthly', 'term', 'annual') NOT NULL,
    effective_start DATE NOT NULL,
    effective_end DATE NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    INDEX idx_fee_structure_category_dates (fee_category_id, is_active, effective_start, effective_end),
    CONSTRAINT chk_fee_structure_amount CHECK (amount >= 0),
    CONSTRAINT chk_fee_structure_dates CHECK (effective_end IS NULL OR effective_end >= effective_start),
    CONSTRAINT fk_fee_structure_category FOREIGN KEY (fee_category_id) REFERENCES fee_categories(fee_category_id) ON DELETE RESTRICT
) ENGINE=InnoDB;

INSERT INTO fee_categories (category_name, description) VALUES
    ('Hostel accommodation', 'Room and bed accommodation charge'),
    ('Mess', 'Meal plan and mess charges'),
    ('Utilities', 'Hostel utility charges'),
    ('Damage', 'Approved damage recovery');

CREATE TABLE invoice_items (
    invoice_item_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    invoice_id BIGINT UNSIGNED NOT NULL,
    fee_category_id BIGINT UNSIGNED NULL,
    category VARCHAR(60) NOT NULL,
    description VARCHAR(255) NOT NULL,
    quantity DECIMAL(10, 2) NOT NULL DEFAULT 1.00,
    unit_amount DECIMAL(12, 2) NOT NULL,
    INDEX idx_invoice_items_invoice (invoice_id),
    CONSTRAINT chk_invoice_item_quantity CHECK (quantity > 0),
    CONSTRAINT chk_invoice_item_amount CHECK (unit_amount >= 0),
    CONSTRAINT fk_invoice_items_invoice FOREIGN KEY (invoice_id) REFERENCES invoices(invoice_id) ON DELETE RESTRICT,
    CONSTRAINT fk_invoice_items_category FOREIGN KEY (fee_category_id) REFERENCES fee_categories(fee_category_id) ON DELETE RESTRICT
) ENGINE=InnoDB;

CREATE TABLE payments (
    payment_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    receipt_number VARCHAR(50) NOT NULL UNIQUE,
    student_id BIGINT UNSIGNED NOT NULL,
    amount DECIMAL(12, 2) NOT NULL,
    received_at DATETIME NOT NULL,
    method ENUM('cash', 'bank_transfer', 'card_manual', 'other') NOT NULL DEFAULT 'other',
    reference VARCHAR(120) NULL,
    status ENUM('successful', 'reversed') NOT NULL DEFAULT 'successful',
    recorded_by BIGINT UNSIGNED NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_payments_student_date (student_id, received_at, status),
    CONSTRAINT chk_payment_amount CHECK (amount > 0),
    CONSTRAINT fk_payments_student FOREIGN KEY (student_id) REFERENCES students(student_id) ON DELETE RESTRICT,
    CONSTRAINT fk_payments_recorder FOREIGN KEY (recorded_by) REFERENCES users(user_id) ON DELETE SET NULL
) ENGINE=InnoDB;

CREATE TABLE payment_applications (
    payment_id BIGINT UNSIGNED NOT NULL,
    invoice_id BIGINT UNSIGNED NOT NULL,
    amount_applied DECIMAL(12, 2) NOT NULL,
    PRIMARY KEY (payment_id, invoice_id),
    CONSTRAINT chk_payment_application_amount CHECK (amount_applied > 0),
    CONSTRAINT fk_payment_applications_payment FOREIGN KEY (payment_id) REFERENCES payments(payment_id) ON DELETE RESTRICT,
    CONSTRAINT fk_payment_applications_invoice FOREIGN KEY (invoice_id) REFERENCES invoices(invoice_id) ON DELETE RESTRICT
) ENGINE=InnoDB;

CREATE TABLE checkout_clearances (
    clearance_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    allocation_id BIGINT UNSIGNED NOT NULL,
    balance_at_decision DECIMAL(12, 2) NOT NULL,
    decision ENUM('cleared', 'blocked') NOT NULL,
    decided_by BIGINT UNSIGNED NULL,
    decided_at DATETIME NOT NULL,
    note VARCHAR(500) NULL,
    INDEX idx_clearance_allocation_time (allocation_id, decided_at),
    CONSTRAINT chk_clearance_balance CHECK (balance_at_decision >= 0),
    CONSTRAINT fk_clearance_allocation FOREIGN KEY (allocation_id) REFERENCES allocations(allocation_id) ON DELETE RESTRICT,
    CONSTRAINT fk_clearance_user FOREIGN KEY (decided_by) REFERENCES users(user_id) ON DELETE SET NULL
) ENGINE=InnoDB;

CREATE TABLE attendance (
    attendance_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    student_id BIGINT UNSIGNED NOT NULL,
    attendance_date DATE NOT NULL,
    session_name VARCHAR(40) NOT NULL DEFAULT 'daily',
    status ENUM('present', 'absent', 'excused') NOT NULL,
    recorded_by BIGINT UNSIGNED NULL,
    recorded_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_attendance_student_day_session (student_id, attendance_date, session_name),
    CONSTRAINT fk_attendance_student FOREIGN KEY (student_id) REFERENCES students(student_id) ON DELETE RESTRICT,
    CONSTRAINT fk_attendance_recorder FOREIGN KEY (recorded_by) REFERENCES users(user_id) ON DELETE SET NULL
) ENGINE=InnoDB;

CREATE TABLE leave_requests (
    leave_request_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    student_id BIGINT UNSIGNED NOT NULL,
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    reason VARCHAR(1000) NOT NULL,
    status ENUM('pending', 'approved', 'rejected', 'cancelled') NOT NULL DEFAULT 'pending',
    reviewed_by BIGINT UNSIGNED NULL,
    reviewed_at DATETIME NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_leave_status_dates (status, start_date, end_date),
    CONSTRAINT chk_leave_dates CHECK (end_date >= start_date),
    CONSTRAINT fk_leave_student FOREIGN KEY (student_id) REFERENCES students(student_id) ON DELETE RESTRICT,
    CONSTRAINT fk_leave_reviewer FOREIGN KEY (reviewed_by) REFERENCES users(user_id) ON DELETE SET NULL
) ENGINE=InnoDB;

CREATE TABLE complaints (
    complaint_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    student_id BIGINT UNSIGNED NOT NULL,
    room_id BIGINT UNSIGNED NULL,
    category VARCHAR(60) NOT NULL,
    description TEXT NOT NULL,
    priority ENUM('low', 'normal', 'high', 'urgent') NOT NULL DEFAULT 'normal',
    status ENUM('open', 'in_progress', 'resolved', 'closed') NOT NULL DEFAULT 'open',
    assigned_staff_id BIGINT UNSIGNED NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_complaints_status_priority (status, priority, created_at),
    CONSTRAINT fk_complaints_student FOREIGN KEY (student_id) REFERENCES students(student_id) ON DELETE RESTRICT,
    CONSTRAINT fk_complaints_room FOREIGN KEY (room_id) REFERENCES rooms(room_id) ON DELETE SET NULL,
    CONSTRAINT fk_complaints_staff FOREIGN KEY (assigned_staff_id) REFERENCES staff(staff_id) ON DELETE SET NULL
) ENGINE=InnoDB;

CREATE TABLE maintenance_work_orders (
    work_order_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    complaint_id BIGINT UNSIGNED NULL,
    room_id BIGINT UNSIGNED NULL,
    bed_id BIGINT UNSIGNED NULL,
    assigned_staff_id BIGINT UNSIGNED NULL,
    description VARCHAR(1000) NOT NULL,
    status ENUM('open', 'in_progress', 'completed', 'cancelled') NOT NULL DEFAULT 'open',
    opened_at DATETIME NOT NULL,
    completed_at DATETIME NULL,
    CONSTRAINT fk_work_orders_complaint FOREIGN KEY (complaint_id) REFERENCES complaints(complaint_id) ON DELETE SET NULL,
    CONSTRAINT fk_work_orders_room FOREIGN KEY (room_id) REFERENCES rooms(room_id) ON DELETE RESTRICT,
    CONSTRAINT fk_work_orders_bed FOREIGN KEY (bed_id) REFERENCES beds(bed_id) ON DELETE RESTRICT,
    CONSTRAINT fk_work_orders_staff FOREIGN KEY (assigned_staff_id) REFERENCES staff(staff_id) ON DELETE SET NULL
) ENGINE=InnoDB;

CREATE TABLE visitors (
    visitor_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    student_id BIGINT UNSIGNED NOT NULL,
    visitor_name VARCHAR(160) NOT NULL,
    visitor_phone VARCHAR(30) NULL,
    purpose VARCHAR(255) NOT NULL,
    checked_in_at DATETIME NOT NULL,
    checked_out_at DATETIME NULL,
    recorded_by BIGINT UNSIGNED NULL,
    INDEX idx_visitors_student_date (student_id, checked_in_at),
    CONSTRAINT chk_visitor_checkout CHECK (checked_out_at IS NULL OR checked_out_at >= checked_in_at),
    CONSTRAINT fk_visitors_student FOREIGN KEY (student_id) REFERENCES students(student_id) ON DELETE RESTRICT,
    CONSTRAINT fk_visitors_recorder FOREIGN KEY (recorded_by) REFERENCES users(user_id) ON DELETE SET NULL
) ENGINE=InnoDB;

CREATE TABLE menus (
    menu_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    service_date DATE NOT NULL,
    meal_type ENUM('breakfast', 'lunch', 'dinner', 'snack') NOT NULL,
    title VARCHAR(120) NOT NULL,
    published BOOLEAN NOT NULL DEFAULT FALSE,
    created_by BIGINT UNSIGNED NULL,
    UNIQUE KEY uq_menu_date_meal (service_date, meal_type),
    CONSTRAINT fk_menus_creator FOREIGN KEY (created_by) REFERENCES users(user_id) ON DELETE SET NULL
) ENGINE=InnoDB;

CREATE TABLE menu_items (
    menu_item_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    menu_id BIGINT UNSIGNED NOT NULL,
    item_name VARCHAR(120) NOT NULL,
    dietary_note VARCHAR(160) NULL,
    CONSTRAINT fk_menu_items_menu FOREIGN KEY (menu_id) REFERENCES menus(menu_id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE meal_attendance (
    meal_attendance_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    menu_id BIGINT UNSIGNED NOT NULL,
    student_id BIGINT UNSIGNED NOT NULL,
    attended_at DATETIME NOT NULL,
    UNIQUE KEY uq_meal_student (menu_id, student_id),
    CONSTRAINT fk_meal_attendance_menu FOREIGN KEY (menu_id) REFERENCES menus(menu_id) ON DELETE RESTRICT,
    CONSTRAINT fk_meal_attendance_student FOREIGN KEY (student_id) REFERENCES students(student_id) ON DELETE RESTRICT
) ENGINE=InnoDB;

CREATE TABLE inventory_items (
    inventory_item_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    item_name VARCHAR(160) NOT NULL UNIQUE,
    unit_name VARCHAR(30) NOT NULL,
    reorder_level DECIMAL(12, 2) NOT NULL DEFAULT 0,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT chk_inventory_reorder CHECK (reorder_level >= 0)
) ENGINE=InnoDB;

CREATE TABLE inventory_transactions (
    inventory_transaction_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    inventory_item_id BIGINT UNSIGNED NOT NULL,
    staff_id BIGINT UNSIGNED NULL,
    quantity_change DECIMAL(12, 2) NOT NULL,
    reason VARCHAR(255) NOT NULL,
    occurred_at DATETIME NOT NULL,
    INDEX idx_inventory_item_time (inventory_item_id, occurred_at),
    CONSTRAINT chk_inventory_quantity_change CHECK (quantity_change <> 0),
    CONSTRAINT fk_inventory_transactions_item FOREIGN KEY (inventory_item_id) REFERENCES inventory_items(inventory_item_id) ON DELETE RESTRICT,
    CONSTRAINT fk_inventory_transactions_staff FOREIGN KEY (staff_id) REFERENCES staff(staff_id) ON DELETE SET NULL
) ENGINE=InnoDB;

CREATE TABLE notifications (
    notification_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    user_id BIGINT UNSIGNED NOT NULL,
    title VARCHAR(160) NOT NULL,
    body VARCHAR(1000) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    read_at DATETIME NULL,
    CONSTRAINT fk_notifications_user FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE audit_logs (
    audit_log_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    user_id BIGINT UNSIGNED NULL,
    action VARCHAR(80) NOT NULL,
    entity_type VARCHAR(80) NOT NULL,
    entity_id BIGINT UNSIGNED NULL,
    details JSON NULL,
    occurred_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_audit_entity (entity_type, entity_id, occurred_at),
    INDEX idx_audit_user_time (user_id, occurred_at),
    CONSTRAINT fk_audit_user FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE SET NULL
) ENGINE=InnoDB;

CREATE VIEW v_invoice_balances AS
SELECT i.invoice_id,
       i.invoice_number,
       i.student_id,
       i.status,
       COALESCE(SUM(ii.quantity * ii.unit_amount), 0.00) AS invoiced_amount,
       COALESCE((
           SELECT SUM(pa.amount_applied)
           FROM payment_applications pa
           JOIN payments p ON p.payment_id = pa.payment_id
           WHERE pa.invoice_id = i.invoice_id AND p.status = 'successful'
       ), 0.00) AS paid_amount,
       COALESCE(SUM(ii.quantity * ii.unit_amount), 0.00) - COALESCE((
           SELECT SUM(pa.amount_applied)
           FROM payment_applications pa
           JOIN payments p ON p.payment_id = pa.payment_id
           WHERE pa.invoice_id = i.invoice_id AND p.status = 'successful'
       ), 0.00) AS outstanding_amount
FROM invoices i
LEFT JOIN invoice_items ii ON ii.invoice_id = i.invoice_id
GROUP BY i.invoice_id, i.invoice_number, i.student_id, i.status;

CREATE VIEW v_bed_availability_today AS
SELECT b.bed_id,
             hb.block_name,
             r.room_number,
             b.bed_label,
             EXISTS (
                     SELECT 1 FROM allocations a
                     WHERE a.bed_id=b.bed_id AND a.checked_in_at IS NOT NULL AND a.checked_out_at IS NULL
             ) AS is_physically_occupied,
             EXISTS (
                     SELECT 1 FROM allocations a
                     WHERE a.bed_id=b.bed_id AND a.status<>'cancelled'
                         AND a.planned_start_date<=CURRENT_DATE
                         AND (a.effective_end_date IS NULL OR a.effective_end_date>CURRENT_DATE)
             ) AS has_scheduled_allocation,
             EXISTS (
                     SELECT 1 FROM maintenance_holds m
                     WHERE (m.bed_id=b.bed_id OR m.room_id=r.room_id)
                         AND m.status IN ('scheduled','active')
                         AND m.start_date<=CURRENT_DATE
                         AND (m.end_date IS NULL OR m.end_date>CURRENT_DATE)
             ) AS has_maintenance_hold,
             NOT EXISTS (
                     SELECT 1 FROM allocations a
                     WHERE a.bed_id=b.bed_id AND a.status<>'cancelled'
                         AND a.planned_start_date<=CURRENT_DATE
                         AND (a.effective_end_date IS NULL OR a.effective_end_date>CURRENT_DATE)
             ) AND NOT EXISTS (
                     SELECT 1 FROM maintenance_holds m
                     WHERE (m.bed_id=b.bed_id OR m.room_id=r.room_id)
                         AND m.status IN ('scheduled','active')
                         AND m.start_date<=CURRENT_DATE
                         AND (m.end_date IS NULL OR m.end_date>CURRENT_DATE)
             ) AS is_scheduled_available,
             NOT EXISTS (
                     SELECT 1 FROM allocations a
                     WHERE a.bed_id=b.bed_id AND a.checked_in_at IS NOT NULL AND a.checked_out_at IS NULL
             ) AND NOT EXISTS (
                     SELECT 1 FROM maintenance_holds m
                     WHERE (m.bed_id=b.bed_id OR m.room_id=r.room_id)
                         AND m.status IN ('scheduled','active')
                         AND m.start_date<=CURRENT_DATE
                         AND (m.end_date IS NULL OR m.end_date>CURRENT_DATE)
             ) AS is_physically_available
FROM beds b
JOIN rooms r ON r.room_id=b.room_id
JOIN hostel_blocks hb ON hb.block_id=r.block_id
WHERE b.is_active=TRUE AND r.is_active=TRUE AND hb.is_active=TRUE;