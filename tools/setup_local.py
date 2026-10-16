"""Safely prepare a local MySQL 8.4 database and a private project .env file."""

import os
import secrets
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import mysql.connector


ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
MIGRATION_DIR = ROOT / "database" / "migrations"
DATABASE = "hostel_management"
APP_USER = "hostel_app"
MYSQL_SOCKET = os.getenv("MYSQL_ADMIN_SOCKET", "/tmp/mysql84.sock")
MYSQL_PORT = 3307


def main() -> int:
    if ENV_FILE.exists():
        print("Refusing to overwrite an existing .env file. Keep its credentials safe and configure it manually.")
        return 2
    migrations = sorted(MIGRATION_DIR.glob("*.sql"))
    if not migrations:
        print("No migration files were found; no database changes were made.")
        return 2

    try:
        admin = mysql.connector.connect(user="root", unix_socket=MYSQL_SOCKET, autocommit=True)
    except mysql.connector.Error as error:
        print(f"Could not connect to MySQL 8.4 through {MYSQL_SOCKET}: {error.msg}")
        print("If MySQL root has a password, enter it via a local secure prompt and update this helper; do not put it in chat.")
        return 2

    try:
        with admin.cursor() as cursor:
            cursor.execute("SELECT VERSION(), @@port, @@datadir")
            version, port, datadir = cursor.fetchone()
            if not version.startswith("8.") or port != MYSQL_PORT or Path(datadir).resolve() != Path("/opt/homebrew/var/mysql84").resolve():
                print("Refusing setup: connected server is not the isolated MySQL 8.x instance on port 3307.")
                return 2

            cursor.execute(
                "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=%s",
                (DATABASE,),
            )
            database_exists = cursor.fetchone() is not None
            if database_exists:
                cursor.execute(
                    "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s",
                    (DATABASE,),
                )
                table_count = cursor.fetchone()[0]
                if table_count:
                    print(f"Database {DATABASE} already has {table_count} tables/views; refusing to rerun the initial migration.")
                    print("No tables were dropped or altered. Inspect the database and configure .env to reuse it.")
                    return 2
            else:
                cursor.execute(
                    f"CREATE DATABASE `{DATABASE}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
                )

            cursor.execute(
                "SELECT COUNT(*) FROM mysql.user WHERE user=%s AND host='127.0.0.1'",
                (APP_USER,),
            )
            if cursor.fetchone()[0]:
                print(f"MySQL account {APP_USER}@127.0.0.1 already exists; refusing to change its password or grants.")
                print("No database was reset. Resolve the existing account before continuing.")
                return 2

        mysql_client = os.getenv("MYSQL84_CLIENT", "/opt/homebrew/opt/mysql@8.4/bin/mysql")
        if not Path(mysql_client).is_file():
            mysql_client = shutil.which("mysql") or ""
        if not mysql_client:
            print("Could not locate the MySQL client; database created only if it was absent, but migration was not applied.")
            return 2

        for migration in migrations:
            result = subprocess.run(
                [mysql_client, "--protocol=socket", f"--socket={MYSQL_SOCKET}", "-u", "root", f"--database={DATABASE}"],
                input=migration.read_text(encoding="utf-8"),
                text=True,
                capture_output=True,
                check=False,
            )
            if result.returncode:
                print(f"Migration {migration.name} failed. The database has not been dropped or reset; inspect the error and current schema before retrying.")
                print(result.stderr.strip())
                return result.returncode

        app_password = secrets.token_urlsafe(48)
        secret_key = secrets.token_urlsafe(48)
        with admin.cursor() as cursor:
            cursor.execute(
                f"CREATE USER '{APP_USER}'@'127.0.0.1' IDENTIFIED BY %s",
                (app_password,),
            )
            cursor.execute(
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON `{DATABASE}`.* TO '{APP_USER}'@'127.0.0.1'"
            )

        contents = (
            f"FLASK_SECRET_KEY={secret_key}\n"
            f"DB_HOST=127.0.0.1\n"
            f"DB_PORT={MYSQL_PORT}\n"
            f"DB_NAME={DATABASE}\n"
            f"DB_USER={APP_USER}\n"
            f"DB_PASSWORD={app_password}\n"
            "DB_POOL_SIZE=5\n"
            "FLASK_DEBUG=1\n"
            "SESSION_COOKIE_SECURE=0\n"
        )
        file_descriptor = os.open(ENV_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as env_file:
                env_file.write(contents)
        except Exception:
            try:
                ENV_FILE.unlink(missing_ok=True)
            finally:
                with admin.cursor() as cursor:
                    cursor.execute(f"DROP USER '{APP_USER}'@'127.0.0.1'")
            raise

        app_connection = mysql.connector.connect(
            host="127.0.0.1",
            port=MYSQL_PORT,
            database=DATABASE,
            user=APP_USER,
            password=app_password,
        )
        try:
            with app_connection.cursor() as cursor:
                cursor.execute("SELECT VERSION(), DATABASE(), CURRENT_USER()")
                app_version, selected_database, current_user = cursor.fetchone()
                cursor.execute("SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s", (DATABASE,))
                object_count = cursor.fetchone()[0]
        finally:
            app_connection.close()

        permissions = stat.S_IMODE(ENV_FILE.stat().st_mode)
        if permissions != 0o600:
            ENV_FILE.chmod(0o600)
        print(f"MySQL {app_version} connection verified for {selected_database} as {current_user}.")
        print(f"{len(migrations)} ordered migrations applied; {object_count} tables/views are present.")
        print("Generated .env with a random app password and Flask secret; file permissions are owner-only (0600). Secret values were not displayed.")
        return 0
    finally:
        admin.close()


if __name__ == "__main__":
    sys.exit(main())