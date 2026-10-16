CREATE TABLE login_attempts (
    login_attempt_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    attempt_key CHAR(64) NOT NULL,
    succeeded BOOLEAN NOT NULL,
    attempted_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_login_attempts_window (attempt_key, succeeded, attempted_at),
    INDEX idx_login_attempts_cleanup (attempted_at)
) ENGINE=InnoDB;