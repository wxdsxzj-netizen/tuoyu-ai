"""OAuth tests use runtime-generated credentials/signatures and local provider/transport doubles."""
import base64
import hashlib
import secrets
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.db import SessionLocal, tenant_context, utcnow
from app.outreach import oauth, transport
from app.outreach.crypto import decrypt
from tests.conftest import register
from tests.test_p4 import _ctx_db


def test_callback_requires_authenticated_mailbox_permission(client):
    response = client.post('/api/v1/mailboxes/oauth/google:callback',
                           json={'state': secrets.token_urlsafe(32), 'code': secrets.token_urlsafe(24)})
    assert response.status_code == 401, response.text

@pytest.fixture
def provider_env(client, monkeypatch):
    settings = get_settings()
    for name, value in {'mailbox_oauth_enabled': True, 'google_oauth_client_id': secrets.token_urlsafe(16),
        'google_oauth_client_secret': secrets.token_urlsafe(24), 'microsoft_oauth_client_id': secrets.token_urlsafe(16),
        'microsoft_oauth_client_secret': secrets.token_urlsafe(24), 'public_base_url': 'https://app.example.com',
        'credentials_key': Fernet.generate_key().decode()}.items():
        monkeypatch.setattr(settings, name, value)
    auth = register(client, 'oauth-owner@example.com')
    tenant = _ctx_db(client, auth)
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    kid = secrets.token_hex(8)
    public = jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key(), as_dict=True)
    public['kid'] = kid
    context = {'auth': auth, 'tenant': tenant, 'calls': [], 'auth_requests': {}, 'claims_override': {},
               'access': secrets.token_urlsafe(32), 'refresh': secrets.token_urlsafe(48), 'private': private}

    def request(method, url, **kwargs):
        if method == 'GET':
            return {'keys': [public]}
        data = kwargs['data']
        provider = 'google' if url == oauth.PROVIDERS['google']['token'] else 'microsoft'
        context['calls'].append(data.copy())
        scope = ' '.join(oauth.PROVIDERS[provider]['mail_scopes'])
        if data['grant_type'] == 'refresh_token':
            return {'access_token': context['access'], 'refresh_token': context['refresh'], 'expires_in': 3600}
        query = context['auth_requests'][provider]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(data['code_verifier'].encode()).digest()).decode().rstrip('=')
        assert challenge == query['code_challenge'][0]
        assert data['redirect_uri'] == query['redirect_uri'][0]
        claims = {'iss': 'https://accounts.google.com', 'aud': getattr(settings, provider + '_oauth_client_id'),
                  'sub': secrets.token_urlsafe(16), 'nonce': query['nonce'][0], 'iat': utcnow(),
                  'exp': utcnow() + timedelta(hours=1), 'email': 'bound@example.com', 'email_verified': True}
        if provider == 'microsoft':
            tid = str(uuid.uuid4())
            claims.update(tid=tid, iss=f'https://login.microsoftonline.com/{tid}/v2.0', preferred_username='bound@example.com')
        claims.update(context['claims_override'])
        result = {'access_token': context['access'], 'refresh_token': context['refresh'], 'expires_in': 3600,
                  'token_type': 'Bearer', 'scope': scope, 'id_token': jwt.encode(claims, private, algorithm='RS256', headers={'kid': kid})}
        result.update(context.get('token_override', {}))
        return result

    monkeypatch.setattr(oauth, 'request_json', request)
    return context


def start(client, context, provider='google'):
    response = client.get(f'/api/v1/mailboxes/oauth/{provider}:start', headers=context['auth'])
    assert response.status_code == 200 and response.json()['stub'] is False, response.text
    query = parse_qs(urlsplit(response.json()['authorize_url']).query)
    context['auth_requests'][provider] = query
    return {'state': query['state'][0], 'code': secrets.token_urlsafe(24)}


def complete(client, context, provider='google'):
    body = start(client, context, provider)
    response = client.post(f'/api/v1/mailboxes/oauth/{provider}:callback', headers=context['auth'], json=body)
    assert response.status_code == 200, response.text
    return response.json(), body


@pytest.mark.parametrize('provider', ['google', 'microsoft'])
def test_verified_callback_binds_once_and_only_stores_encrypted_tokens(client, provider_env, provider):
    from app.models import MailboxOAuthCredential
    context = provider_env
    result, body = complete(client, context, provider)
    again = client.post(f'/api/v1/mailboxes/oauth/{provider}:callback', headers=context['auth'], json=body)
    assert again.status_code == 200 and again.json()['id'] == result['id']
    assert len(context['calls']) == 1
    assert context['access'] not in str(result) and context['refresh'] not in str(result)
    with tenant_context(context['tenant']), SessionLocal() as db:
        credential = db.scalar(select(MailboxOAuthCredential))
        assert decrypt(credential.access_token_enc) == context['access']
        assert decrypt(credential.refresh_token_enc) == context['refresh']
        raw = db.execute(text('SELECT access_token_enc, refresh_token_enc FROM mailbox_oauth_credentials WHERE tenant_id=:tid'),
                         {'tid': context['tenant']}).one()
        assert context['access'] not in str(raw) and context['refresh'] not in str(raw)
    listing = client.get('/api/v1/mailboxes', headers=context['auth'])
    assert context['access'] not in listing.text and context['refresh'] not in listing.text


@pytest.mark.parametrize('claims', [{'nonce': 'wrong'}, {'aud': 'wrong'}, {'iss': 'https://untrusted.example'},
                                   {'email_verified': False}, {'exp': 1}])
def test_invalid_identity_never_binds_or_reexchanges_the_code(client, provider_env, claims):
    from app.models import Mailbox
    context = provider_env
    context['claims_override'] = claims
    body = start(client, context)
    response = client.post('/api/v1/mailboxes/oauth/google:callback', headers=context['auth'], json=body)
    assert response.status_code == 422, response.text
    retry = client.post('/api/v1/mailboxes/oauth/google:callback', headers=context['auth'], json=body)
    assert retry.status_code == 409 and len(context['calls']) == 1
    with tenant_context(context['tenant']), SessionLocal() as db:
        assert db.scalar(select(Mailbox)) is None


@pytest.mark.parametrize('provider', ['google', 'microsoft'])
@pytest.mark.parametrize('case', ['multiple_wrong', 'multiple_missing', 'single_wrong', 'multiple_correct', 'single_array_correct'])
def test_audience_and_authorized_party_must_be_trusted(client, provider_env, provider, case):
    from app.models import Mailbox
    context = provider_env
    client_id = getattr(get_settings(), provider + '_oauth_client_id')
    other_client = secrets.token_urlsafe(16)
    claims = {'aud': [client_id, other_client] if case.startswith('multiple') else client_id}
    if case == 'single_array_correct':
        claims['aud'] = [client_id]
    if case != 'multiple_missing':
        claims['azp'] = client_id if case.endswith('_correct') else other_client
    context['claims_override'] = claims
    body = start(client, context, provider)
    response = client.post(f'/api/v1/mailboxes/oauth/{provider}:callback', headers=context['auth'], json=body)
    accepted = case == 'single_array_correct'
    assert response.status_code == (200 if accepted else 422), response.text
    with tenant_context(context['tenant']), SessionLocal() as db:
        assert (db.scalar(select(Mailbox)) is not None) is accepted
    assert len(context['calls']) == 1


def test_cross_tenant_state_and_wrong_provider_cannot_consume_authorization(client, provider_env):
    context = provider_env
    body = start(client, context)
    other = register(client, 'other-oauth-owner@example.com')
    response = client.post('/api/v1/mailboxes/oauth/google:callback', headers=other, json=body)
    assert response.status_code == 404
    response = client.post('/api/v1/mailboxes/oauth/microsoft:callback', headers=context['auth'], json=body)
    assert response.status_code == 404
    assert context['calls'] == []
    response = client.post('/api/v1/mailboxes/oauth/google:callback', headers=context['auth'], json=body)
    assert response.status_code == 200


def test_cancel_and_missing_scope_fail_without_fake_success(client, provider_env):
    from app.models import Mailbox
    context = provider_env
    body = start(client, context)
    response = client.post('/api/v1/mailboxes/oauth/google:callback', headers=context['auth'], json={**body, 'error': 'access_denied'})
    assert response.status_code == 200 and response.json()['cancelled'] is True
    assert context['calls'] == []
    body = start(client, context)
    context['token_override'] = {'scope': 'openid email'}
    response = client.post('/api/v1/mailboxes/oauth/google:callback', headers=context['auth'], json=body)
    assert response.status_code == 422
    with tenant_context(context['tenant']), SessionLocal() as db:
        assert db.scalar(select(Mailbox)) is None


def test_concurrent_refresh_commits_once_even_when_callers_mailbox_transaction_rolls_back(client, provider_env):
    from app.models import Mailbox, MailboxOAuthCredential
    context = provider_env
    result, _ = complete(client, context)
    mid = uuid.UUID(result['id'])
    context['calls'].clear()
    with tenant_context(context['tenant']), SessionLocal() as db:
        credential = db.scalar(select(MailboxOAuthCredential))
        credential.expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    context['access'], context['refresh'] = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    with tenant_context(context['tenant']), SessionLocal() as outer:
        mailbox = outer.scalar(select(Mailbox).where(Mailbox.id == mid).with_for_update())
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(oauth.access_token, mailbox) for _ in range(2)]
            assert [f.result(timeout=5) for f in futures] == [context['access']] * 2
        outer.rollback()
    assert len(context['calls']) == 1
    with tenant_context(context['tenant']), SessionLocal() as db:
        assert decrypt(db.scalar(select(MailboxOAuthCredential)).refresh_token_enc) == context['refresh']


def test_smtp_and_imap_use_xoauth2_and_reject_token_exfiltration_hosts(client, provider_env, monkeypatch):
    from app.models import Mailbox
    context = provider_env
    result, _ = complete(client, context)
    payloads = []

    class SMTP:
        def __init__(self, *a, **kw):
            self.greeted = False

        def ehlo_or_helo_if_needed(self):
            self.greeted = True

        def starttls(self, **kw):
            self.greeted = False

        def close(self):
            pass

        def auth(self, mechanism, authobject, **kw):
            assert self.greeted, 'SMTP must re-greet after STARTTLS before AUTH'
            assert mechanism == 'XOAUTH2'
            payloads.append(authobject())
            assert authobject(b'provider-error-challenge') == ''

    class IMAP:
        def __init__(self, *a, **kw):
            pass

        def authenticate(self, mechanism, callback):
            assert mechanism == 'XOAUTH2'
            payloads.append(callback(b'').decode())

    monkeypatch.setattr(get_settings(), 'outbound_sink_hosts', 'smtp.gmail.com,imap.gmail.com')
    monkeypatch.setattr(transport.smtplib, 'SMTP', SMTP)
    monkeypatch.setattr(transport.imaplib, 'IMAP4_SSL', IMAP)
    with tenant_context(context['tenant']), SessionLocal() as db:
        mailbox = db.get(Mailbox, uuid.UUID(result['id']))
        transport._connect(mailbox)
        transport.imap_connect(mailbox)
        expected = oauth.sasl(mailbox.address, context['access'])
        assert payloads == [expected, expected]
        mailbox.smtp_host = 'attacker.example'
        monkeypatch.setattr(get_settings(), 'outbound_sink_hosts', 'attacker.example')
        with pytest.raises(Exception) as failure:
            transport._connect(mailbox)
        assert getattr(failure.value, 'code', None) == 'DELIVERY_AUTH_FAILED'
        assert len(payloads) == 2


def test_unbinding_erases_refresh_credentials(client, provider_env):
    from app.models import MailboxOAuthCredential
    result, _ = complete(client, provider_env)
    response = client.delete('/api/v1/mailboxes/' + result['id'], headers=provider_env['auth'])
    assert response.status_code == 204
    with tenant_context(provider_env['tenant']), SessionLocal() as db:
        credential = db.scalar(select(MailboxOAuthCredential))
        assert credential.status == 'revoked'
        assert credential.access_token_enc is None and credential.refresh_token_enc is None


def test_refresh_failure_is_persisted_and_does_not_blindly_retry(client, provider_env, monkeypatch):
    from app.core.errors import AppError
    from app.models import Mailbox, MailboxOAuthCredential
    context = provider_env
    result, _ = complete(client, context)
    with tenant_context(context['tenant']), SessionLocal() as db:
        credential = db.scalar(select(MailboxOAuthCredential))
        credential.expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    calls = []

    def revoked(*args, **kwargs):
        calls.append('refresh')
        raise AppError('DELIVERY_AUTH_FAILED')

    monkeypatch.setattr(oauth, 'request_json', revoked)
    with tenant_context(context['tenant']), SessionLocal() as db:
        mailbox = db.get(Mailbox, uuid.UUID(result['id']))
        for _ in range(2):
            with pytest.raises(AppError):
                oauth.access_token(mailbox)
        credential = db.scalar(select(MailboxOAuthCredential))
        assert credential.status == 'expired' and credential.access_token_enc is None
    assert calls == ['refresh']


def test_expired_state_cannot_exchange_a_code(client, provider_env):
    from app.models import MailboxOAuthAttempt
    context = provider_env
    body = start(client, context)
    with tenant_context(context['tenant']), SessionLocal() as db:
        db.scalar(select(MailboxOAuthAttempt)).expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    response = client.post('/api/v1/mailboxes/oauth/google:callback', headers=context['auth'], json=body)
    assert response.status_code == 409 and context['calls'] == []


def test_refresh_database_failure_rolls_back_before_safe_error_and_preserves_no_plaintext(client, provider_env, monkeypatch):
    from sqlalchemy import event

    from app.core.errors import AppError
    from app.models import Mailbox, MailboxOAuthCredential
    context = provider_env
    result, _ = complete(client, context)
    with tenant_context(context['tenant']), SessionLocal() as db:
        db.scalar(select(MailboxOAuthCredential)).expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    factory = oauth.SessionLocal

    def broken_session():
        db = factory()
        event.listen(db, 'before_commit', lambda session: session.execute(text('SELECT 1 / 0')), once=True)
        return db

    monkeypatch.setattr(oauth, 'SessionLocal', broken_session)
    with tenant_context(context['tenant']), SessionLocal() as db:
        mailbox = db.get(Mailbox, uuid.UUID(result['id']))
        with pytest.raises(AppError) as failure:
            oauth.access_token(mailbox)
        assert failure.value.code == 'DELIVERY_AUTH_FAILED'
        assert context['access'] not in str(failure.value) and context['refresh'] not in str(failure.value)
        assert db.scalar(select(MailboxOAuthCredential)).status == 'expired'


def test_oauth_password_patch_cannot_replace_provider_authorization(client, provider_env):
    result, _ = complete(client, provider_env)
    response = client.patch('/api/v1/mailboxes/' + result['id'], headers=provider_env['auth'],
                            json={'smtp_password': secrets.token_urlsafe(24)})
    assert response.status_code == 400
