import os
import secrets
from datetime import timedelta


class Config:
    SECRET_KEY = os.getenv("FLASK_SECRET_KEY") or secrets.token_urlsafe(32)
    DEBUG = os.getenv("FLASK_DEBUG", "0") == "1"
    DB_CONFIG = {
        "host": os.getenv("DB_HOST", "127.0.0.1"),
        "port": int(os.getenv("DB_PORT", "3306")),
        "database": os.getenv("DB_NAME", "hostel_management"),
        "user": os.getenv("DB_USER", "hostel_app"),
        "password": os.getenv("DB_PASSWORD", ""),
        "pool_name": "hostel_pool",
        "pool_size": max(1, min(int(os.getenv("DB_POOL_SIZE", "5")), 32)),
        "autocommit": False,
        "use_unicode": True,
    }
    if os.getenv("DB_UNIX_SOCKET"):
        DB_CONFIG.pop("host")
        DB_CONFIG["unix_socket"] = os.getenv("DB_UNIX_SOCKET")
    SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "0") == "1"
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    PERMANENT_SESSION_LIFETIME = timedelta(hours=8)
    WTF_CSRF_TIME_LIMIT = 3600