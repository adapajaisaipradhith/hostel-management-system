import getpass

import click
from flask import current_app
from werkzeug.security import generate_password_hash

from app.db import cursor, get_connection, write_audit_record


def register_cli(app):
    @app.cli.command("create-admin")
    @click.option("--username", prompt=True)
    @click.option("--email", prompt=True)
    def create_admin(username: str, email: str):
        """Create the initial super-admin without placing a password in shell history."""
        password = getpass.getpass("Password (minimum 12 characters): ")
        confirmation = getpass.getpass("Confirm password: ")
        if len(password) < 12 or password != confirmation:
            raise click.ClickException("Passwords must match and be at least 12 characters.")

        connection = get_connection()
        try:
            with cursor(dictionary=False) as db_cursor:
                db_cursor.execute(
                    "INSERT INTO users (username, email, password_hash) VALUES (%s, %s, %s)",
                    (username.strip(), email.strip().lower(), generate_password_hash(password)),
                )
                user_id = db_cursor.lastrowid
                db_cursor.execute("SELECT role_id FROM roles WHERE role_name = %s", ("Super Admin",))
                role = db_cursor.fetchone()
                if role is None:
                    raise click.ClickException("Run the database migration first; the Super Admin role is missing.")
                db_cursor.execute(
                    "INSERT INTO user_roles (user_id, role_id) VALUES (%s, %s)",
                    (user_id, role[0]),
                )
                write_audit_record(db_cursor, None, "user.created", "user", user_id, {"role": "Super Admin", "source": "cli"})
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        click.echo(f"Created administrator account {username.strip()}.")

    @app.cli.command("create-user")
    @click.option("--username", prompt=True)
    @click.option("--email", prompt=True)
    @click.option("--role", type=click.Choice(["Super Admin", "Warden", "Accountant", "Student", "Mess Manager"]), prompt=True)
    @click.option("--student-number", default=None, help="Link this login to an existing student record.")
    @click.option("--staff-number", default=None, help="Link this login to an existing staff record.")
    def create_user(username: str, email: str, role: str, student_number: str | None, staff_number: str | None):
        """Create a role account; passwords are entered privately, not as CLI arguments."""
        if student_number and staff_number:
            raise click.ClickException("Link one profile type only.")
        password = getpass.getpass("Password (minimum 12 characters): ")
        confirmation = getpass.getpass("Confirm password: ")
        if len(password) < 12 or password != confirmation:
            raise click.ClickException("Passwords must match and be at least 12 characters.")
        connection = get_connection()
        try:
            with cursor(dictionary=False) as db_cursor:
                db_cursor.execute("SELECT role_id FROM roles WHERE role_name=%s", (role,))
                role_row = db_cursor.fetchone()
                if role_row is None:
                    raise click.ClickException("The requested role is not in the migration.")
                db_cursor.execute(
                    "INSERT INTO users (username,email,password_hash) VALUES (%s,%s,%s)",
                    (username.strip(), email.strip().lower(), generate_password_hash(password)),
                )
                user_id = db_cursor.lastrowid
                db_cursor.execute("INSERT INTO user_roles (user_id,role_id) VALUES (%s,%s)", (user_id, role_row[0]))
                if student_number:
                    db_cursor.execute("UPDATE students SET user_id=%s WHERE student_number=%s AND user_id IS NULL", (user_id, student_number))
                    if db_cursor.rowcount != 1:
                        raise click.ClickException("Student number was not found or already has a login.")
                if staff_number:
                    db_cursor.execute("UPDATE staff SET user_id=%s WHERE staff_number=%s AND user_id IS NULL", (user_id, staff_number))
                    if db_cursor.rowcount != 1:
                        raise click.ClickException("Staff number was not found or already has a login.")
                write_audit_record(db_cursor, None, "user.created", "user", user_id, {"role": role, "source": "cli"})
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        click.echo(f"Created {role} account {username.strip()}.")