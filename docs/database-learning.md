# DBMS Learning Notes: Hostel Management System

This note explains the schema inspected directly from the local development database. The live target is MySQL 8.4.11 on `127.0.0.1:3307`; the other MySQL 26.7 instance remains separate on port 3306. Metadata reports 30 base tables and 2 views. `README.md` previously said 31 tables/views; that count was stale.

## 1. The Data Model

The two migrations currently create 32 objects: 30 base tables and 2 views. The central relationships are:

- `hostel_blocks` 1-to-many `rooms`, and `rooms` 1-to-many `beds`.
- Rooms can be edited; room/bed deactivation changes an `is_active` flag instead of deleting rows, and is blocked while a physical stay or future reservation depends on the space.
- `students` 1-to-many `allocations`; `beds` 1-to-many historical `allocations`.
- `allocations` keep planned dates, actual check-in/out timestamps, and the bed-label snapshot captured at reservation time. A planned end date is not proof of actual checkout, and relabeling a bed does not rename its historical allocations.
- `invoices` 1-to-many `invoice_items`; `payments` and `invoices` connect through `payment_applications` so partial and split payments can be represented.
- `fee_categories` 1-to-many dated `fee_structures`; invoice items also preserve a category label and charged amount as historical facts.
- `maintenance_holds` apply to exactly one room or one bed, and are considered alongside allocations when calculating availability.
- `complaints` may create `maintenance_work_orders`; a work order can reference one assigned `staff` member while the staff record remains independent of the login account.
- `checkout_clearances` stores each clearance decision instead of overwriting prior attempts.
- `users` and `roles` connect many-to-many through `user_roles`.

### Every installed table and view

| Object | Purpose |
|---|---|
| `allocations` | Reservation and stay history, with planned dates and actual check-in/out timestamps. |
| `attendance` | A student's daily/session attendance result. |
| `audit_logs` | Administrative event history; the generic entity reference is intentionally not a foreign key to many possible tables. |
| `beds` | Individual physical capacity inside a room. |
| `checkout_clearances` | Each financial clearance decision for an allocation, including blocked attempts. |
| `complaints` | Student-reported issues, status, priority, and optional staff assignment. |
| `fee_categories` | Stable labels for charge types such as accommodation or mess. |
| `fee_structures` | Amount, frequency, and effective dates for a fee category. |
| `hostel_blocks` | Top-level hostel buildings/blocks. |
| `inventory_items` | Inventory catalog and reorder threshold. |
| `inventory_transactions` | Append-only stock increases/decreases; current stock is their sum. |
| `invoice_items` | Itemized quantity, description, category snapshot, and charged amount for an invoice. |
| `invoices` | A student's billing document and its issue/due dates and posting status. |
| `leave_requests` | Student leave periods and approval state. |
| `login_attempts` | HMAC-keyed login success/failure timestamps used for throttling; no raw password, username, or IP. |
| `maintenance_holds` | A dated block on exactly one room or one bed. |
| `maintenance_work_orders` | Work arising from a complaint or location, optionally assigned to staff. |
| `meal_attendance` | A student's attendance for a menu/meal; `(menu_id, student_id)` is unique. |
| `menu_items` | One food item per row for a menu. |
| `menus` | A dated meal service and whether it is published. |
| `notifications` | User-specific in-app messages and read time. |
| `payment_applications` | Bridge between payments and invoices, including the amount of a payment applied to one invoice. |
| `payments` | Manual receipt, payer, amount, method, date, and success/reversal status. |
| `roles` | Named system permission groups. |
| `rooms` | Room label/floor/type and its parent block. |
| `staff` | Staff directory record, optionally linked to a login. |
| `students` | Student identity, contact/guardian details, admission date, and active flag. |
| `user_roles` | Many-to-many assignment of users to roles. |
| `users` | Login identity, password hash, active flag, and timestamps. |
| `visitors` | Student host, visitor details, and actual in/out timestamps. |
| `v_bed_availability_today` | Derived current physical occupancy, scheduled allocation, hold, and two kinds of availability. |
| `v_invoice_balances` | Derived invoice total, successful payments applied, and outstanding balance. |

The development database currently has one admin user and no student, block, room, bed, allocation, or invoice rows. The separate `hostel_management_test_20261017` database contains synthetic integration-test data and one `LEARN-DEMO-01` practice block. All DML examples below that change rows are scoped to that test database.

A primary key identifies one row. A foreign key makes the database reject references to missing parent rows. `UNIQUE(block_id, room_number)` allows the same room number in different blocks but disallows duplicates inside one block. `UNIQUE(room_id, bed_label)` follows the same pattern for beds.

## 2. Why Normalize?

Imagine a spreadsheet with one row per student, columns for room, block, bed, fee, payment, guardian, and a list of past stays. It creates common anomalies:

- **1NF:** A value should be atomic. Do not store several beds or payments in comma-separated text.
- **2NF:** With a composite key, every non-key value must depend on the entire key. An invoice description and amount belong to an invoice line, not to an unrelated payment row.
- **3NF:** Non-key facts should depend on the row key, not another non-key fact. Block details belong in `hostel_blocks`, not duplicated on every bed.

The split tables reduce update mistakes and let the database enforce relationships with foreign keys. Historical invoice line amounts stay fixed even when a fee structure changes later.

## 3. Inspect the Actual MySQL Database

Connect with the versioned client and the isolated server's Unix socket:

```sh
/opt/homebrew/opt/mysql@8.4/bin/mysql --protocol=socket --socket=/tmp/mysql84.sock -u root hostel_management
```

Then try these read-only statements:

```sql
SELECT VERSION(), DATABASE();
SHOW TABLES;
SHOW CREATE TABLE allocations\G
SHOW CREATE VIEW v_invoice_balances\G
SHOW CREATE VIEW v_bed_availability_today\G
```

`SHOW CREATE TABLE` is useful DBMS learning: it displays keys, indexes, constraints, defaults, and generated columns as MySQL actually installed them.

## 4. Availability and Date Ranges

The booking interval is half-open: `[planned_start_date, planned_end_date)`. Start is included, end is excluded. Two scheduled bookings touch without overlap when one ends exactly when the next begins.

For existing range `[existing_start, existing_end)` and proposed range `[new_start, new_end)`, the overlap predicate is:

```sql
existing_start < new_end
AND new_start < existing_end
```

For open-ended ranges, `NULL` means no known end and must be treated as later than the proposed period. The app uses `effective_end_date`, a generated column that takes an actual checkout date when present and otherwise the planned end. This preserves the original planned date while letting an early actual checkout end the effective reservation.

Try querying the view:

```sql
SELECT block_name, room_number, bed_label,
       is_physically_occupied, has_scheduled_allocation,
       has_maintenance_hold, is_scheduled_available,
       is_physically_available
FROM v_bed_availability_today
ORDER BY block_name, room_number, bed_label;
```

A reservation can make a bed scheduled-unavailable while it remains physically empty. A resident whose planned end passed but who has not checked out remains physically occupied. Those are deliberately separate columns, not one `available` flag.

## 5. Join and Aggregate Practice

**Exercise:** list each current physical occupant with block, room, and bed. Start with this shape and complete the missing selected student fields yourself:

```sql
SELECT /* add student name */, hb.block_name, r.room_number, b.bed_label
FROM allocations a
JOIN students s ON s.student_id = a.student_id
JOIN beds b ON b.bed_id = a.bed_id
JOIN rooms r ON r.room_id = b.room_id
JOIN hostel_blocks hb ON hb.block_id = r.block_id
WHERE a.checked_in_at IS NOT NULL
  AND a.checked_out_at IS NULL;
```

A `JOIN` keeps rows that match on both sides. A `LEFT JOIN` keeps the left-side row even if no child row exists. Try counting beds per block, including a block with no rooms:

```sql
SELECT hb.block_name, COUNT(DISTINCT b.bed_id) AS bed_count
FROM hostel_blocks hb
LEFT JOIN rooms r ON r.block_id = hb.block_id
LEFT JOIN beds b ON b.room_id = r.room_id
GROUP BY hb.block_id, hb.block_name;
```

Try `GROUP BY` plus `HAVING` to show only blocks with more than two beds. Explain why `HAVING` filters groups while `WHERE` filters individual rows.

## 6. Subqueries, Balances, and Views

`v_invoice_balances` derives amounts from invoice items and successful payment applications. It avoids a manually updated balance column getting out of sync:

```sql
SELECT invoice_number, invoiced_amount, paid_amount, outstanding_amount
FROM v_invoice_balances
WHERE status = 'posted' AND outstanding_amount > 0
ORDER BY outstanding_amount DESC;
```

Find students with no active physical allocation with `NOT EXISTS`:

```sql
SELECT s.student_number, s.first_name, s.last_name
FROM students s
WHERE s.is_active = TRUE
  AND NOT EXISTS (
      SELECT 1 FROM allocations a
      WHERE a.student_id = s.student_id
        AND a.checked_in_at IS NOT NULL
        AND a.checked_out_at IS NULL
  );
```

A view stores a query definition, not a second copy of each result. Re-run the query after adding records and the view computes from current rows.

## 7. Indexes and EXPLAIN

Indexes speed up reads by keeping searchable key values in an ordered structure. They consume disk and add work to inserts, updates, and deletes. An index should answer a query pattern, not merely exist because a column exists.

Inspect a query plan:

```sql
EXPLAIN
SELECT allocation_id
FROM allocations
WHERE bed_id = 1
  AND planned_start_date < '2030-01-10'
  AND (effective_end_date IS NULL OR effective_end_date > '2030-01-01');
```

Use a real bed ID from your database. Look at `key`, `rows`, and `type`; then explain whether the allocation date index helps this lookup.

## 8. Transactions and Integrity

An allocation is concurrency-sensitive. Two staff actions could both see an empty bed. The service starts a transaction, locks the student and bed/room parent rows, checks overlaps and maintenance holds, inserts the reservation, then commits. On any failed rule it rolls back.

Checkout similarly locks the student and relevant posted invoices, calculates the current balance from the balance view, records a clearance decision, and records actual checkout only if the balance is zero. A blocked decision is retained as history.

Fee structures are retired by setting `is_active = FALSE`; do not rewrite or delete them to change the current price. Posted invoice items keep the amount that was charged at the time, so past balances and receipts remain reproducible.

Try the transaction control commands only in your disposable test database:

```sql
START TRANSACTION;
SELECT bed_id FROM beds WHERE bed_id = 1 FOR UPDATE;
-- Inspect a safe test operation, then choose one:
ROLLBACK;
-- Or, only when you intentionally want to keep a valid change:
-- COMMIT;
```

`FOR UPDATE` locks selected rows until commit or rollback. It does not replace the overlap query; both are needed. The generated unique keys provide an extra database constraint for one current physical allocation per student and bed.

## 9. Login Throttling

Migration `002_login_throttling.sql` adds `login_attempts`. The table stores a keyed HMAC of the normalized login name and client address, whether the attempt succeeded, and a timestamp. It does not store the submitted password, raw login name, or raw IP address.

The app counts failed attempts in the last 15 minutes and blocks after five. MySQL's `GET_LOCK`/`RELEASE_LOCK` serializes attempts sharing the same key, so parallel requests cannot all pass the limit check at once. The composite index starts with `attempt_key` because each request checks one key's recent attempts.

Inspect only aggregate counts in development:

```sql
SELECT succeeded, COUNT(*) AS attempts
FROM login_attempts
WHERE attempted_at >= NOW() - INTERVAL 15 MINUTE
GROUP BY succeeded;
```

The project includes unit tests for the user-facing throttle response and a MySQL integration test for repeated failed attempts and a correct password hash. As with other event tables, retention cleanup should be added before a long-running production deployment.

## 10. Bed Label Migration

Migration `003_allocation_bed_label_snapshot.sql` adds `allocations.bed_label_snapshot` and backfills it from the associated bed. New reservations save the label at creation time. The allocation history query uses that snapshot, falling back to the current bed label only if a legacy/manual allocation has no snapshot. This additive change keeps the existing `bed_id` foreign key and prevents a bed rename from changing how old stays are displayed.

## 11. Step-by-Step SQL Lessons

Connect to the disposable schema, not the development database:

```sh
/opt/homebrew/opt/mysql@8.4/bin/mysql --protocol=socket --socket=/tmp/mysql84.sock -u root hostel_management_test_20261017
```

The examples use the synthetic rows already in that test database. Integration tests add uniquely named rows and retain them, so counts can grow after reruns. The development `hostel_management` schema remains separate.

### Lesson 1: SELECT

**Concept:** `SELECT` chooses columns; `FROM` chooses the table. `LIMIT` keeps a result small.

```sql
SELECT student_number, first_name, last_name
FROM students
LIMIT 5;
```

**Expected:** At most five synthetic student rows, each with those three columns.
**Practice:** Select only `student_number` and `email` for five students.

### Lesson 2: WHERE

**Concept:** `WHERE` filters individual rows before grouping. `%` in `LIKE` matches any sequence.

```sql
SELECT student_number, is_active
FROM students
WHERE student_number LIKE 'FS-%' AND is_active = TRUE;
```

**Expected:** Only active synthetic students whose number begins `FS-`.
**Practice:** Find active students whose number starts with `R-`.

### Lesson 3: ORDER BY

**Concept:** Results have no guaranteed order unless you request one. `ASC` is ascending; `DESC` is descending.

```sql
SELECT block_name, address
FROM hostel_blocks
ORDER BY block_name ASC
LIMIT 5;
```

**Expected:** The first five block names alphabetically.
**Practice:** Return the five newest recorded payments using `received_at DESC`.

### Lesson 4: INSERT

**Concept:** `INSERT` adds a row. Name the columns so you do not depend on table column order.

```sql
START TRANSACTION;
INSERT INTO hostel_blocks (block_name, address)
VALUES ('LEARN-DEMO-02', 'Synthetic SQL practice');
SELECT block_name, address FROM hostel_blocks WHERE block_name='LEARN-DEMO-02';
ROLLBACK;
SELECT COUNT(*) FROM hostel_blocks WHERE block_name='LEARN-DEMO-02';
```

**Expected:** The first `SELECT` sees the row inside the transaction; after rollback the count is `0`.
**Practice:** Insert a different uniquely named synthetic block in a transaction and roll it back.

### Lesson 5: UPDATE

**Concept:** `UPDATE` changes matching rows. A primary-key or unique-key `WHERE` clause prevents accidental broad updates.

```sql
START TRANSACTION;
UPDATE hostel_blocks
SET address='Temporary practice value'
WHERE block_name='LEARN-DEMO-01';
SELECT address FROM hostel_blocks WHERE block_name='LEARN-DEMO-01';
ROLLBACK;
SELECT address FROM hostel_blocks WHERE block_name='LEARN-DEMO-01';
```

**Expected:** Inside the transaction the address is temporary; after rollback it is `Synthetic SQL lesson only` again.
**Practice:** In a transaction, change the same test row's address, inspect it, and roll back.

### Lesson 6: DELETE

**Concept:** `DELETE` removes matching rows, so always preview the predicate with `SELECT` first. Foreign keys may reject deletion when history refers to a row; deactivation is often safer.

```sql
START TRANSACTION;
SELECT block_id FROM hostel_blocks WHERE block_name='LEARN-DEMO-01';
DELETE FROM hostel_blocks WHERE block_name='LEARN-DEMO-01';
SELECT COUNT(*) FROM hostel_blocks WHERE block_name='LEARN-DEMO-01';
ROLLBACK;
SELECT COUNT(*) FROM hostel_blocks WHERE block_name='LEARN-DEMO-01';
```

**Expected:** Count is `0` after delete inside the transaction and `1` after rollback.
**Practice:** Write a `DELETE` for a temporary test row using its exact unique name; do not execute it without a transaction.

### Lesson 7: GROUP BY and aggregates

**Concept:** Aggregates summarize groups. `WHERE` filters source rows; `HAVING` filters completed groups.

```sql
SELECT hb.block_name, COUNT(b.bed_id) AS bed_count
FROM hostel_blocks hb
LEFT JOIN rooms r ON r.block_id=hb.block_id
LEFT JOIN beds b ON b.room_id=r.room_id
GROUP BY hb.block_id, hb.block_name
ORDER BY bed_count DESC
LIMIT 5;
```

**Expected:** Up to five synthetic blocks and their bed counts, including a block with zero beds.
**Practice:** Add `HAVING COUNT(b.bed_id) > 1` and explain which rows disappear.

### Lesson 8: JOIN

**Concept:** A join combines related rows using key equality. `LEFT JOIN` keeps a parent even when it has no child row.

```sql
SELECT hb.block_name, r.room_number, b.bed_label
FROM hostel_blocks hb
JOIN rooms r ON r.block_id=hb.block_id
JOIN beds b ON b.room_id=r.room_id
WHERE b.is_active=TRUE
ORDER BY hb.block_name, r.room_number, b.bed_label
LIMIT 5;
```

**Expected:** Up to five active synthetic beds, with their room and block names.
**Practice:** Change the joins to `LEFT JOIN`; what happens to an empty block?

### Lesson 9: Subqueries

**Concept:** `NOT EXISTS` asks whether a related row is absent. It avoids fragile `NOT IN` behavior when nulls are possible.

```sql
SELECT s.student_number
FROM students s
WHERE NOT EXISTS (
  SELECT 1 FROM allocations a
  WHERE a.student_id=s.student_id
    AND a.checked_in_at IS NOT NULL
    AND a.checked_out_at IS NULL
);
```

**Expected:** Synthetic students without a current physical allocation.
**Practice:** Find beds with no active physical stay using `NOT EXISTS`.

### Lesson 10: Views

**Concept:** A view is a named query, not a separately stored balance. It recomputes from invoice items and successful payment applications.

```sql
SELECT invoice_number, invoiced_amount, paid_amount, outstanding_amount
FROM v_invoice_balances
WHERE status='posted' AND outstanding_amount > 0
ORDER BY outstanding_amount DESC;
```

**Expected:** Posted synthetic invoices with a positive balance; the test data includes both paid and unpaid cases.
**Practice:** Query `v_bed_availability_today` and explain why physical and scheduled availability can differ.

### Lesson 11: Constraints

**Concept:** Primary keys identify rows; foreign keys protect references; `UNIQUE` protects candidate keys; `CHECK` enforces row-level rules.

```sql
-- This should fail its CHECK constraint; afterward run ROLLBACK if in a transaction.
INSERT INTO inventory_items (item_name, unit_name, reorder_level)
VALUES ('LEARN-INVALID-REORDER', 'unit', -1);

-- This should fail its foreign key because room_id 999999 is not present.
INSERT INTO beds (room_id, bed_label) VALUES (999999, 'A');
```

**Expected:** MySQL rejects the negative reorder level and missing room reference; invalid rows are not created.
**Practice:** Identify which constraint rejects a duplicate `(room_id, bed_label)` pair.

### Lesson 12: Indexes and EXPLAIN

**Concept:** An index can reduce rows scanned for a lookup, but costs storage and makes writes slower. `EXPLAIN` estimates the access plan.

```sql
EXPLAIN
SELECT allocation_id FROM allocations
WHERE bed_id=1 AND planned_start_date<'2030-01-10'
  AND (effective_end_date IS NULL OR effective_end_date>'2030-01-01');
```

**Expected:** MySQL reports a candidate index and estimated rows; the exact plan depends on test data and chosen ID.
**Practice:** Run `EXPLAIN` for `students WHERE last_name='Test'` and locate the index used.

### Lesson 13: Transactions

**Concept:** A transaction groups changes so they all commit or all roll back. `FOR UPDATE` locks rows to serialize competing operations; it does not replace checking business rules.

```sql
START TRANSACTION;
SELECT bed_id FROM beds WHERE bed_id=1 FOR UPDATE;
-- Inspect or make only an intentional synthetic test change.
ROLLBACK;
```

**Expected:** The row remains unchanged; the lock is released at rollback.
**Practice:** Explain why allocation must lock both the student and bed before inserting a date range.

## 12. Verification Status

All three ordered migrations were applied to `hostel_management` on MySQL 8.4.11. A generated app account connected successfully, and the database contains 32 tables/views. The app account has only data manipulation privileges on this schema. The `admin` user is verified as a Super Admin with a non-empty password hash; no password or hash is documented here.

The integration tests use disposable schemas and leave their uniquely named test rows behind. They cover sign-in hashing/throttling, maintenance work-order assignment, and fee retirement preserving posted invoice lines, in addition to booking, checkout, inventory, and page queries. Never point them at important or production data.
