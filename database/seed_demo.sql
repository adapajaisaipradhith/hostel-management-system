-- Fictional demonstration records only. Do not use real student data.
INSERT IGNORE INTO hostel_blocks (block_name, address) VALUES ('DEMO-NORTH', 'Example campus');
SET @demo_block_id = (SELECT block_id FROM hostel_blocks WHERE block_name='DEMO-NORTH');

INSERT IGNORE INTO rooms (block_id, floor_number, room_number, room_type)
VALUES (@demo_block_id, 1, 'N-101', 'double'), (@demo_block_id, 1, 'N-102', 'single');
SET @demo_room_double = (SELECT room_id FROM rooms WHERE block_id=@demo_block_id AND room_number='N-101');
SET @demo_room_single = (SELECT room_id FROM rooms WHERE block_id=@demo_block_id AND room_number='N-102');

INSERT IGNORE INTO beds (room_id, bed_label) VALUES
    (@demo_room_double, 'A'), (@demo_room_double, 'B'), (@demo_room_single, 'A');

INSERT IGNORE INTO students (student_number, first_name, last_name, email, phone, admission_date)
VALUES
    ('DEMO-1001', 'Asha', 'Rao', 'asha.demo1001@example.test', '000-000-1001', CURRENT_DATE),
    ('DEMO-1002', 'Noah', 'Kim', 'noah.demo1002@example.test', '000-000-1002', CURRENT_DATE);

INSERT IGNORE INTO allocations (student_id, bed_id, planned_start_date, planned_end_date, status)
SELECT s.student_id, b.bed_id, CURRENT_DATE, DATE_ADD(CURRENT_DATE, INTERVAL 30 DAY), 'reserved'
FROM students s JOIN beds b ON b.room_id=@demo_room_double
WHERE s.student_number='DEMO-1001' AND b.bed_label='A'
  AND NOT EXISTS (
      SELECT 1 FROM allocations a WHERE a.student_id=s.student_id AND a.bed_id=b.bed_id
        AND a.status<>'cancelled' AND a.planned_start_date=CURRENT_DATE
  );

INSERT IGNORE INTO invoices (invoice_number, student_id, issue_date, due_date, status)
SELECT 'DEMO-INV-1001', student_id, CURRENT_DATE, DATE_ADD(CURRENT_DATE, INTERVAL 14 DAY), 'posted'
FROM students WHERE student_number='DEMO-1001';

SET @demo_invoice_id = (SELECT invoice_id FROM invoices WHERE invoice_number='DEMO-INV-1001');
INSERT INTO invoice_items (invoice_id, fee_category_id, category, description, quantity, unit_amount)
SELECT @demo_invoice_id, fee_category_id, category_name, 'Example monthly accommodation fee', 1, 350.00
FROM fee_categories WHERE category_name='Hostel accommodation'
AND NOT EXISTS (SELECT 1 FROM invoice_items WHERE invoice_id=@demo_invoice_id AND description='Example monthly accommodation fee');
