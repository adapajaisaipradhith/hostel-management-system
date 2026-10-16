from contextlib import contextmanager
import json
from typing import Any

import mysql.connector
from flask import current_app, g
from mysql.connector import MySQLConnection
from mysql.connector.pooling import MySQLConnectionPool


def _pool() -> MySQLConnectionPool:
    pool = current_app.extensions.get("mysql_pool")
    if pool is None:
        config = dict(current_app.config["DB_CONFIG"])
        config["pool_name"] = f"hostel_{id(current_app._get_current_object())}"
        pool = MySQLConnectionPool(**config)
        current_app.extensions["mysql_pool"] = pool
    return pool


def get_connection() -> MySQLConnection:
    connection = g.get("db_connection")
    if connection is None or not connection.is_connected():
        connection = _pool().get_connection()
        g.db_connection = connection
    return connection


def close_connection(_error: BaseException | None = None) -> None:
    connection = g.pop("db_connection", None)
    if connection is not None:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


@contextmanager
def cursor(*, dictionary: bool = True):
    connection = get_connection()
    db_cursor = connection.cursor(dictionary=dictionary)
    try:
        yield db_cursor
    finally:
        db_cursor.close()


def fetch_one(sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    with cursor() as db_cursor:
        db_cursor.execute(sql, params)
        return db_cursor.fetchone()


def fetch_all(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with cursor() as db_cursor:
        db_cursor.execute(sql, params)
        return db_cursor.fetchall()


def execute(sql: str, params: tuple[Any, ...] = ()) -> int:
    connection = get_connection()
    try:
        with cursor(dictionary=False) as db_cursor:
            db_cursor.execute(sql, params)
            row_id = db_cursor.lastrowid
        connection.commit()
        return row_id
    except Exception:
        connection.rollback()
        raise


def write_audit_record(db_cursor, user_id: int | None, action: str, entity_type: str, entity_id: int | None = None, details: dict | None = None) -> None:
    db_cursor.execute(
        "INSERT INTO audit_logs (user_id,action,entity_type,entity_id,details) VALUES (%s,%s,%s,%s,%s)",
        (user_id, action, entity_type, entity_id, json.dumps(details or {})),
    )


def ping_database() -> bool:
    try:
        return get_connection().is_connected()
    except mysql.connector.Error:
        return False