def _create(client, **overrides):
    payload = {"sku": "SKU-1", "name": "Widget", "quantity": 5, "price_cents": 250, **overrides}
    return client.post("/api/items", json=payload)


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok", "database": "ok"}


def test_create_and_get_item(client):
    created = _create(client)
    assert created.status_code == 201
    item_id = created.get_json()["id"]
    fetched = client.get(f"/api/items/{item_id}")
    assert fetched.status_code == 200
    assert fetched.get_json()["name"] == "Widget"


def test_create_requires_json(client):
    assert client.post("/api/items", data="sku=x").status_code == 415


def test_create_validation_errors(client):
    response = client.post("/api/items", json={"sku": "", "quantity": -1})
    assert response.status_code == 400
    fields = response.get_json()["fields"]
    assert {"sku", "name", "quantity"} <= set(fields)


def test_duplicate_sku_rejected(client):
    assert _create(client).status_code == 201
    assert _create(client).status_code == 409


def test_update_put_and_patch(client):
    item_id = _create(client).get_json()["id"]
    put = client.put(f"/api/items/{item_id}", json={"sku": "SKU-1", "name": "Gadget", "quantity": 1, "price_cents": 100})
    assert put.status_code == 200 and put.get_json()["name"] == "Gadget"
    patch = client.patch(f"/api/items/{item_id}", json={"quantity": 9})
    assert patch.status_code == 200 and patch.get_json()["quantity"] == 9 and patch.get_json()["name"] == "Gadget"


def test_delete_item(client):
    item_id = _create(client).get_json()["id"]
    assert client.delete(f"/api/items/{item_id}").status_code == 204
    assert client.get(f"/api/items/{item_id}").status_code == 404
    assert client.delete(f"/api/items/{item_id}").status_code == 404


def test_search_and_pagination(client):
    for i in range(12):
        _create(client, sku=f"S{i}", name=f"Bolt {i}")
    page1 = client.get("/api/items?page=1").get_json()
    assert page1["total"] == 12 and len(page1["items"]) == 10
    assert len(client.get("/api/items?page=2").get_json()["items"]) == 2
    assert client.get("/api/items?q=Bolt 1").get_json()["total"] == 3  # Bolt 1, 10, 11
