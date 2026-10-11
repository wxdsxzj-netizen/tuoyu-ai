from app.core.config import get_settings
from tests.conftest import register


def test_client_id_alone_never_offers_an_unimplemented_callback(client, monkeypatch):
    h = register(client, 'oauth-owner@example.com')
    for provider in ('google', 'microsoft'):
        monkeypatch.setattr(get_settings(), f'{provider}_oauth_client_id', 'public-client-id')
        for method in (client.get, client.post):
            r = method(f'/api/v1/mailboxes/oauth/{provider}:start', headers=h)
            assert r.status_code == 200
            assert r.json()['stub'] is True
            assert r.json()['authorize_url'] is None
            assert r.json()['status'] == 'unavailable'
            assert r.json()['next_steps']
    assert client.get('/api/v1/mailboxes', headers=h).json() == []
