"""Browser UI for inventory items."""

from __future__ import annotations

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from . import repository as repo

views_bp = Blueprint("views", __name__)


@views_bp.get("/")
def index():
    page = max(request.args.get("page", 1, type=int), 1)
    search = request.args.get("q", "").strip()
    per_page = current_app.config["ITEMS_PER_PAGE"]
    items, total = repo.list_items(search or None, page=page, per_page=per_page)
    pages = max((total + per_page - 1) // per_page, 1)
    return render_template("items.html", items=items, total=total, page=page, pages=pages, search=search)


@views_bp.route("/items/new", methods=["GET", "POST"])
def new_item():
    errors: dict[str, str] = {}
    form = {"sku": "", "name": "", "quantity": "0", "price_cents": "0"}
    if request.method == "POST":
        form = {k: request.form.get(k, "") for k in form}
        try:
            repo.create_item(form)
        except repo.ValidationError as exc:
            errors = exc.errors
        except repo.DuplicateError as exc:
            errors = {"sku": str(exc)}
        else:
            flash(f"Item {form['name']} created.")
            return redirect(url_for("views.index"))
    return render_template("item_form.html", form=form, errors=errors, title="New item", action=url_for("views.new_item")), (400 if errors else 200)


@views_bp.route("/items/<int:item_id>/edit", methods=["GET", "POST"])
def edit_item(item_id: int):
    item = repo.get_item(item_id)
    if item is None:
        return render_template("not_found.html"), 404
    errors: dict[str, str] = {}
    form = {k: str(item[k]) for k in ("sku", "name", "quantity", "price_cents")}
    if request.method == "POST":
        form = {k: request.form.get(k, "") for k in form}
        try:
            repo.update_item(item_id, form, partial=False)
        except repo.ValidationError as exc:
            errors = exc.errors
        except repo.DuplicateError as exc:
            errors = {"sku": str(exc)}
        else:
            flash(f"Item {form['name']} updated.")
            return redirect(url_for("views.index"))
    return render_template("item_form.html", form=form, errors=errors, title="Edit item", action=url_for("views.edit_item", item_id=item_id)), (400 if errors else 200)


@views_bp.post("/items/<int:item_id>/delete")
def delete_item(item_id: int):
    if repo.delete_item(item_id):
        flash("Item deleted.")
    return redirect(url_for("views.index"))
