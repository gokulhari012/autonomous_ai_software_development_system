"""Inventory Management - a small Flask sample application."""

from __future__ import annotations

import logging
import os
import secrets

from flask import Flask, jsonify

from . import db
from .api import api_bp
from .views import views_bp

log = logging.getLogger("inventory")


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
        DATABASE=db.database_path_from_env(os.environ.get("DATABASE_URL"), app.instance_path),
        ITEMS_PER_PAGE=10,
    )
    if test_config:
        app.config.update(test_config)
    os.makedirs(app.instance_path, exist_ok=True)
    logging.basicConfig(level=logging.INFO)

    db.init_app(app)
    app.register_blueprint(api_bp)
    app.register_blueprint(views_bp)

    @app.get("/health")
    def health():
        try:
            db.get_db().execute("SELECT 1").fetchone()
            database = "ok"
        except Exception:  # pragma: no cover - reported, not raised
            log.exception("health check database failure")
            database = "error"
        return jsonify({"status": "ok" if database == "ok" else "degraded", "database": database}), (200 if database == "ok" else 503)

    return app
