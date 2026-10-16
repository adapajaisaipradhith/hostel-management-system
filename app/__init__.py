from datetime import date

from flask import Flask, jsonify, render_template
from flask_wtf.csrf import CSRFProtect

from app.config import Config
from app.db import close_connection, ping_database
from app.auth import auth_bp
from app.cli import register_cli
from app.dashboard import dashboard_bp
from app.routes import bp as records_bp

csrf = CSRFProtect()


def create_app(config_override: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_object(Config)
    if config_override:
        app.config.update(config_override)

    csrf.init_app(app)
    app.teardown_appcontext(close_connection)
    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(records_bp)
    register_cli(app)

    @app.context_processor
    def inject_current_date():
        return {"current_date": date.today().isoformat()}

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    @app.get("/health/ready")
    def readiness():
        connected = ping_database()
        return jsonify({"status": "ready" if connected else "unavailable", "database": connected}), 200 if connected else 503

    @app.errorhandler(404)
    def not_found(_error):
        return render_template("errors/404.html"), 404

    @app.errorhandler(500)
    def server_error(_error):
        return render_template("errors/500.html"), 500

    return app