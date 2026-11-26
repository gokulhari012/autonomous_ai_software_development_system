"""Item repository and validation."""

from __future__ import annotations

import sqlite3
from typing import Any

from .db import get_db


class ValidationError(ValueError):
    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


class DuplicateError(ValueError):
    pass


def validate_item(data: dict[str, Any], partial: bool = False) -> dict[str, Any]:
    errors: dict[str, str] = {}
    clean: dict[str, Any] = {}
    for field in ("sku", "name"):
        if field in data:
            value = str(data[field]).strip()
            if not value:
                errors[field] = "must not be empty"
            elif len(value) > 120:
                errors[field] = "must be at most 120 characters"
            else:
                clean[field] = value
        elif not partial:
            errors[field] = "is required"
    for field in ("quantity", "price_cents"):
        if field in data:
            try:
                value = int(data[field])
            except (TypeError, ValueError):
                errors[field] = "must be an integer"
                continue
            if value < 0:
                errors[field] = "must not be negative"
            else:
                clean[field] = value
    if errors:
        raise ValidationError(errors)
    return clean


def list_items(search: str | None = None, page: int = 1, per_page: int = 10) -> tuple[list[dict[str, Any]], int]:
    db = get_db()
    offset = max(page - 1, 0) * per_page
    if search:
        pattern = f"%{search}%"
        total = db.execute("SELECT COUNT(*) FROM items WHERE name LIKE ? OR sku LIKE ?", (pattern, pattern)).fetchone()[0]
        rows = db.execute(
            "SELECT * FROM items WHERE name LIKE ? OR sku LIKE ? ORDER BY id LIMIT ? OFFSET ?",
            (pattern, pattern, per_page, offset),
        ).fetchall()
    else:
        total = db.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        rows = db.execute("SELECT * FROM items ORDER BY id LIMIT ? OFFSET ?", (per_page, offset)).fetchall()
    return [dict(r) for r in rows], total


def get_item(item_id: int) -> dict[str, Any] | None:
    row = get_db().execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    return dict(row) if row else None


def create_item(data: dict[str, Any]) -> dict[str, Any]:
    clean = validate_item(data)
    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO items (sku, name, quantity, price_cents) VALUES (?, ?, ?, ?)",
            (clean["sku"], clean["name"], clean.get("quantity", 0), clean.get("price_cents", 0)),
        )
        db.commit()
    except sqlite3.IntegrityError as exc:
        db.rollback()
        raise DuplicateError(f"an item with SKU {clean['sku']!r} already exists") from exc
    return get_item(cur.lastrowid)  # type: ignore[return-value]


def update_item(item_id: int, data: dict[str, Any], partial: bool) -> dict[str, Any] | None:
    current = get_item(item_id)
    if current is None:
        return None
    clean = validate_item(data, partial=partial)
    merged = {**current, **clean}
    db = get_db()
    try:
        db.execute(
            "UPDATE items SET sku = ?, name = ?, quantity = ?, price_cents = ? WHERE id = ?",
            (merged["sku"], merged["name"], merged["quantity"], merged["price_cents"], item_id),
        )
        db.commit()
    except sqlite3.IntegrityError as exc:
        db.rollback()
        raise DuplicateError("another item already uses this SKU") from exc
    return get_item(item_id)


def delete_item(item_id: int) -> bool:
    db = get_db()
    cur = db.execute("DELETE FROM items WHERE id = ?", (item_id,))
    db.commit()
    return cur.rowcount == 1
