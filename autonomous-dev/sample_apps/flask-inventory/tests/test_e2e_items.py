"""Browser workflow tests. The orchestrator starts the app and passes --base-url."""

import re
import uuid

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


def test_create_edit_delete_item_workflow(page: Page):
    sku = f"E2E-{uuid.uuid4().hex[:6]}"
    page.goto("/")
    page.get_by_role("link", name="New item").click()
    page.get_by_label("SKU").fill(sku)
    page.get_by_label("Name").fill("Browser widget")
    page.get_by_label("Quantity").fill("3")
    page.get_by_role("button", name="Save").click()
    expect(page.get_by_role("status")).to_contain_text("created")
    expect(page.get_by_role("cell", name=sku)).to_be_visible()

    page.get_by_role("link", name="Edit Browser widget").first.click()
    page.get_by_label("Name").fill("Browser gadget")
    page.get_by_role("button", name="Save").click()
    expect(page.get_by_role("cell", name="Browser gadget", exact=True)).to_be_visible()

    page.reload()  # persistence across refresh
    expect(page.get_by_role("cell", name="Browser gadget", exact=True)).to_be_visible()

    page.get_by_role("button", name="Delete Browser gadget").first.click()
    expect(page.get_by_role("status")).to_contain_text("deleted")


def test_validation_messages_shown(page: Page):
    page.goto("/items/new")
    page.get_by_role("button", name="Save").click()
    expect(page.get_by_role("alert").first).to_be_visible()
    expect(page).to_have_title(re.compile("New item"))


def test_search_filters_items(page: Page):
    page.goto("/?q=does-not-exist-" + uuid.uuid4().hex)
    expect(page.get_by_text("No items found.")).to_be_visible()
