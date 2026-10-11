"""O-06/O-07: trusted line costs, SKU identity, and versioned quote edits."""
import pytest

from tests.conftest import add_user, register

P = "/api/v1"


def setup(client):
    owner = register(client, "owner@quotes.example.com")
    _, sales = add_user(client, owner, "sales@quotes.example.com", "sales")
    account = client.post(f"{P}/accounts", headers=sales, json={"name": "Synthetic buyer"}).json()
    product = client.post(f"{P}/products", headers=owner, json={"name_zh": "合成测试产品"}).json()
    sku = client.post(f"{P}/products/{product['id']}/skus", headers=owner,
                      json={"sku": "AUDIT-SKU", "cost_price": 5, "floor_price": 11, "factory_price": 12}).json()
    return owner, sales, account["id"], sku


def test_hidden_costs_survive_notes_edit_and_reordering(client):
    owner, sales, account, _ = setup(client)
    items = [{"name": "A", "qty": 1, "unit_price": 100, "unit_cost": 96},
             {"name": "B", "qty": 1, "unit_price": 100, "unit_cost": 98}]
    q = client.post(f"{P}/quotes", headers=owner, json={"account_id": account, "items": items}).json()
    view = client.get(f"{P}/quotes/{q['id']}", headers=sales).json()
    assert all("unit_cost" not in row for row in view["items"])
    edited = client.put(f"{P}/quotes/{q['id']}", headers=sales,
                        json={"account_id": account, "items": list(reversed(view["items"])),
                              "version": view.get("version", 1), "notes": "Notes only"})
    assert edited.status_code == 200
    persisted = client.get(f"{P}/quotes/{q['id']}", headers=owner).json()
    assert {row["name"]: row["unit_cost"] for row in persisted["items"]} == {"A": 96, "B": 98}
    sub = client.post(f"{P}/quotes/{q['id']}:submit", headers=sales).json()
    assert sub["status"] == "pending_approval" and sub["approval_level"] == "owner"


def test_sales_cannot_supply_trusted_cost(client):
    _, sales, account, _ = setup(client)
    response = client.post(f"{P}/quotes", headers=sales,
                           json={"account_id": account, "items": [{"name": "Forged", "qty": 1, "unit_price": 100, "unit_cost": 0}]})
    assert response.status_code == 403


@pytest.mark.parametrize("reference", ["id", "code"])
def test_sku_cost_and_floor_are_resolved_and_preserved(client, reference):
    owner, sales, account, sku = setup(client)
    row = {"name": "SKU product", "qty": 1, "unit_price": 10}
    row.update({"sku_id": sku["id"]} if reference == "id" else {"sku": sku["sku"]})
    response = client.post(f"{P}/quotes", headers=sales, json={"account_id": account, "items": [row]})
    assert response.status_code == 201
    q = response.json()
    full = client.get(f"{P}/quotes/{q['id']}", headers=owner).json()
    assert full["items"][0].get("sku_id") == sku["id"] and full["items"][0]["unit_cost"] == 5
    sub = client.post(f"{P}/quotes/{q['id']}:submit", headers=sales).json()
    assert sub["status"] == "pending_approval" and any("底价" in x for x in sub["approval_reasons"])


def test_wrong_tenant_sku_and_unknown_code_fail_closed(client):
    _, _, _, sku = setup(client)
    other = register(client, "owner@otherquotes.example.com")
    account = client.post(f"{P}/accounts", headers=other, json={"name": "Other buyer"}).json()["id"]
    item = {"name": "Foreign SKU", "qty": 1, "unit_price": 10}
    assert client.post(f"{P}/quotes", headers=other, json={"account_id": account, "items": [{**item, "sku_id": sku["id"]}]}).status_code == 404
    assert client.post(f"{P}/quotes", headers=other, json={"account_id": account, "items": [{**item, "sku": "MISSING"}]}).status_code == 400


def test_missing_cost_requires_review_and_stale_edit_conflicts(client):
    owner, sales, account, _ = setup(client)
    q = client.post(f"{P}/quotes", headers=sales,
                    json={"account_id": account, "items": [{"name": "Custom", "qty": 1, "unit_price": 10}]}).json()
    body = {"account_id": account, "items": q["items"], "version": q.get("version", 1), "notes": "first edit"}
    assert client.put(f"{P}/quotes/{q['id']}", headers=sales, json=body).status_code == 200
    assert client.put(f"{P}/quotes/{q['id']}", headers=sales, json={**body, "notes": "stale overwrite"}).status_code == 409
    sub = client.post(f"{P}/quotes/{q['id']}:submit", headers=sales).json()
    assert sub["status"] == "pending_approval"
    assert client.post(f"{P}/quotes/{q['id']}:review", headers=owner, json={"approve": True}).status_code == 400


def test_quote_rounds_fractional_cents_consistently(client):
    owner, _, account, _ = setup(client)
    q = client.post(f"{P}/quotes", headers=owner,
                    json={"account_id": account, "items": [{"name": "Precision", "qty": 1, "unit_price": 1.005, "unit_cost": 0}]}).json()
    assert q["subtotal"] == 1.01 and q["total"] == 1.01


def test_attachment_quote_uses_trusted_sku_without_exposing_cost_to_sales(client):
    owner, sales, account, sku = setup(client)
    response = client.post(f"{P}/quotes:from-attachment", headers=sales,
                           files={"file": ("synthetic.csv", b"AUDIT-SKU,20 pcs,USD 12", "text/csv")},
                           data={"account_id": account, "create": "true"})
    assert response.status_code == 200, response.text
    result = response.json()
    assert "unit_cost" not in result["items"][0]
    quote = client.get(f"{P}/quotes/{result['quote_id']}", headers=owner).json()
    assert quote["items"][0]["sku_id"] == sku["id"]
    assert quote["items"][0]["unit_cost"] == 5
    assert quote["items"][0]["id"]
