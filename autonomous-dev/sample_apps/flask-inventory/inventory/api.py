"""JSON API for inventory items."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from . import repository as repo

api_bp = Blueprint("api", __name__, url_prefix="/api")


def _payload() -> dict:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return {}
    return data


@api_bp.get("/items")
def list_items():
    page = request.args.get("page", 1, type=int)
    items, total = repo.list_items(request.args.get("q"), page=page, per_page=10)
    return jsonify({"items": items, "total": total, "page": page})


@api_bp.post("/items")
def create_item():
    if not request.is_json:
        return jsonify({"error": "request body must be JSON"}), 415
    try:
        item = repo.create_item(_payload())
    except repo.ValidationError as exc:
        return jsonify({"error": "validation failed", "fields": exc.errors}), 400
    except repo.DuplicateError as exc:
        return jsonify({"error": str(exc)}), 409
    return jsonify(item), 201


@api_bp.get("/items/<int:item_id>")
def get_item(item_id: int):
    item = repo.get_item(item_id)
    if item is None:
        return jsonify({"error": "item not found"}), 404
    return jsonify(item)


@api_bp.route("/items/<int:item_id>", methods=["PUT", "PATCH"])
def update_item(item_id: int):
    if not request.is_json:
        return jsonify({"error": "request body must be JSON"}), 415
    try:
        item = repo.update_item(item_id, _payload(), partial=request.method == "PATCH")
    except repo.ValidationError as exc:
        return jsonify({"error": "validation failed", "fields": exc.errors}), 400
    except repo.DuplicateError as exc:
        return jsonify({"error": str(exc)}), 409
    if item is None:
        return jsonify({"error": "item not found"}), 404
    return jsonify(item)


@api_bp.delete("/items/<int:item_id>")
def delete_item(item_id: int):
    if not repo.delete_item(item_id):
        return jsonify({"error": "item not found"}), 404
    return "", 204
