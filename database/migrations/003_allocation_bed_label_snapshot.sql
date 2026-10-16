ALTER TABLE allocations
    ADD COLUMN bed_label_snapshot VARCHAR(30) NULL AFTER bed_id;

UPDATE allocations a
JOIN beds b ON b.bed_id = a.bed_id
SET a.bed_label_snapshot = b.bed_label
WHERE a.bed_label_snapshot IS NULL;