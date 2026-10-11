from tests.conftest import add_user, register


def test_sku_edit_preserves_unsubmitted_prices_and_product_tenant_scope(client):
    h = register(client, 'sku-owner@example.com')
    product = client.post('/api/v1/products', headers=h, json={'name_zh': 'Valve'}).json()
    second = client.post('/api/v1/products', headers=h, json={'name_zh': 'Second'}).json()
    sku = client.post(f"/api/v1/products/{product['id']}/skus", headers=h,
                      json={'sku': 'SKU-1', 'factory_price': 30, 'cost_price': 10, 'floor_price': 15,
                            'spec': {'size': 'M'}, 'tier_prices': [{'min_qty': 100, 'price': 28}]}).json()
    path = f"/api/v1/products/{product['id']}/skus/{sku['id']}"
    r = client.put(path, headers=h, json={'sku': 'SKU-2', 'factory_price': 32})
    assert r.status_code == 200, r.text
    assert r.json()['cost_price'] == 10 and r.json()['floor_price'] == 15
    assert r.json()['spec'] == {'size': 'M'} and r.json()['tier_prices'] == [{'min_qty': 100, 'price': 28}]
    assert client.put(f"/api/v1/products/{second['id']}/skus/{sku['id']}", headers=h, json={'sku': 'BAD'}).status_code == 404
    _, sales = add_user(client, h, 'sku-sales@example.com', 'sales')
    assert client.put(path, headers=sales, json={'sku': 'BAD'}).status_code == 403
    other = register(client, 'other-sku@example.com', tenant='Other')
    assert client.put(path, headers=other, json={'sku': 'BAD'}).status_code == 404


def test_sku_editor_without_floor_permission_cannot_clear_hidden_cost(client):
    from sqlalchemy import select

    from app.core.db import SessionLocal, tenant_context
    from app.models import Permission, Role
    from tests.test_p4 import _ctx_db
    h = register(client, 'sku-editor-owner@example.com')
    product = client.post('/api/v1/products', headers=h, json={'name_zh': 'Valve'}).json()
    sku = client.post(f"/api/v1/products/{product['id']}/skus", headers=h,
                      json={'sku': 'SKU-1', 'cost_price': 10, 'floor_price': 15}).json()
    _, sales = add_user(client, h, 'sku-editor@example.com', 'sales')
    tid = _ctx_db(client, h)
    with tenant_context(tid), SessionLocal() as db:
        role = db.scalar(select(Role).where(Role.code == 'sales'))
        role.permissions.append(db.get(Permission, 'product.manage'))
        db.commit()
    path = f"/api/v1/products/{product['id']}/skus/{sku['id']}"
    assert client.put(path, headers=sales, json={'sku': 'SKU-2', 'floor_price': None}).status_code == 403
    r = client.put(path, headers=sales, json={'sku': 'SKU-2', 'factory_price': 22})
    assert r.status_code == 200 and 'cost_price' not in r.json()
    saved = client.get(f"/api/v1/products/{product['id']}", headers=h).json()['skus'][0]
    assert saved['cost_price'] == 10 and saved['floor_price'] == 15


def test_static_list_rejects_foreign_and_missing_members(client):
    import uuid

    from app.core.db import SessionLocal, tenant_context
    from app.models import Contact
    from tests.test_p4 import _ctx_db
    h = register(client, 'list-owner@example.com')
    other = register(client, 'list-other@example.com', tenant='Other')
    with tenant_context(_ctx_db(client, other)), SessionLocal() as db:
        contact = Contact(email='private@other.example', full_name='Private')
        db.add(contact)
        db.commit()
        foreign = str(contact.id)
    row = client.post('/api/v1/lists', headers=h, json={'name': 'Static'}).json()
    for cid in (foreign, str(uuid.uuid4())):
        r = client.post(f"/api/v1/lists/{row['id']}/members", headers=h, json={'add': [cid]})
        assert r.status_code == 404, r.text
    assert client.get(f"/api/v1/lists/{row['id']}", headers=h).json()['members'] == []
