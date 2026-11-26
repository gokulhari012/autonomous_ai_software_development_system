import sqlite3

import pytest

from inventory import repository as repo
from inventory.db import get_db


def test_insert_read_update_delete(app):
    with app.app_context():
        item = repo.create_item({"sku": "A1", "name": "Anvil", "quantity": 2})
        assert repo.get_item(item["id"])["name"] == "Anvil"
        repo.update_item(item["id"], {"quantity": 7}, partial=True)
        assert repo.get_item(item["id"])["quantity"] == 7
        assert repo.delete_item(item["id"]) is True
        assert repo.get_item(item["id"]) is None


def test_check_constraint_rejects_negative_quantity(app):
    with app.app_context():
        with pytest.raises(sqlite3.IntegrityError):
            get_db().execute("INSERT INTO items (sku, name, quantity) VALUES ('X', 'x', -5)")


def test_duplicate_is_rolled_back(app):
    with app.app_context():
        repo.create_item({"sku": "DUP", "name": "One"})
        with pytest.raises(repo.DuplicateError):
            repo.create_item({"sku": "DUP", "name": "Two"})
        items, total = repo.list_items()
        assert total == 1 and items[0]["name"] == "One"


@pytest.mark.parametrize("value", ["abc", None, "1.5"])
def test_quantity_must_be_integer(value):
    with pytest.raises(repo.ValidationError):
        repo.validate_item({"sku": "a", "name": "b", "quantity": value})
