"""Delegated mailbox OAuth: bound state/PKCE/nonce, verified identity, encrypted tokens.

External endpoints and protocol hosts are fixed; enabling requires explicit operator config.
No OAuth token is returned to the browser or placed in a URL/log/audit payload.
"""
import base64
import hashlib
import hmac
import secrets
import uuid
from datetime import timedelta
from urllib.parse import urlencode, urlsplit

import httpx
import jwt
from email_validator import EmailNotValidError, validate_email
from sqlalchemy import func, select, update

from app.core.config import get_settings
from app.core.db import SessionLocal, current_tenant, tenant_context, utcnow
from app.core.errors import AppError
from app.outreach.crypto import decrypt, encrypt

PROVIDERS = {
    'google': {
        'authorize': 'https://accounts.google.com/o/oauth2/v2/auth',
        'token': 'https://oauth2.googleapis.com/token',
        'jwks': 'https://www.googleapis.com/oauth2/v3/certs',
        'mail_scopes': ['https://mail.google.com/'],
        'smtp': 'smtp.gmail.com', 'imap': 'imap.gmail.com',
    },
    'microsoft': {
        'authorize': 'https://login.microsoftonline.com/common/oauth2/v2.0/authorize',
        'token': 'https://login.microsoftonline.com/common/oauth2/v2.0/token',
        'jwks': 'https://login.microsoftonline.com/common/discovery/v2.0/keys',
        'mail_scopes': ['https://outlook.office.com/IMAP.AccessAsUser.All', 'https://outlook.office.com/SMTP.Send'],
        'smtp': 'smtp.office365.com', 'imap': 'outlook.office365.com',
    },
}


def config(provider):
    if provider not in PROVIDERS:
        raise AppError('COMMON_VALIDATION')
    settings = get_settings()
    client_id = getattr(settings, provider + '_oauth_client_id')
    client_secret = getattr(settings, provider + '_oauth_client_secret', '')
    base = urlsplit(settings.public_base_url)
    safe_origin = (base.scheme == 'https' and base.netloc and not base.username and not base.password
                   and not base.query and not base.fragment and base.path in ('', '/'))
    enabled = getattr(settings, 'mailbox_oauth_enabled', False) and client_id and client_secret and safe_origin
    redirect = settings.public_base_url.rstrip('/') + '/mailboxes/oauth/' + provider + '/callback'
    return bool(enabled), client_id, client_secret, redirect


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def start(db, provider, user_id):
    from app.models import MailboxOAuthAttempt
    enabled, client_id, _, redirect = config(provider)
    meta = PROVIDERS[provider]
    if not enabled:
        return {'provider': provider, 'stub': True, 'status': 'unavailable', 'authorize_url': None, 'state': None,
                'scopes': meta['mail_scopes'], 'note': 'OAuth 尚未由管理员配置启用；本次没有绑定邮箱。',
                'next_steps': ['管理员配置应用凭据、HTTPS回调地址及服务商权限',
                               '在隔离测试邮箱验证授权、刷新及SMTP/IMAP认证后启用', '当前可使用 SMTP / IMAP 入口']}
    db.execute(update(MailboxOAuthAttempt).where(MailboxOAuthAttempt.expires_at <= utcnow(),
               MailboxOAuthAttempt.status == 'pending').values(status='expired', verifier_enc=None))
    state, verifier, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    db.add(MailboxOAuthAttempt(provider=provider, created_by=user_id, state_hash=digest(state),
                              verifier_enc=encrypt(verifier), nonce_hash=digest(nonce), redirect_uri=redirect,
                              expires_at=utcnow() + timedelta(minutes=10), status='pending'))
    db.commit()
    scope = ['openid', 'email', 'profile', *meta['mail_scopes']]
    params = {'client_id': client_id, 'redirect_uri': redirect, 'response_type': 'code', 'state': state,
              'nonce': nonce, 'scope': ' '.join(scope + (['offline_access'] if provider == 'microsoft' else [])),
              'code_challenge': base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('='),
              'code_challenge_method': 'S256', 'prompt': 'consent'}
    if provider == 'google':
        params['access_type'] = 'offline'
    return {'provider': provider, 'stub': False, 'status': 'authorization_required', 'state': state,
            'authorize_url': meta['authorize'] + '?' + urlencode(params), 'scopes': meta['mail_scopes'],
            'note': '继续在服务商授权；返回后连接邮箱，发信仍须通过现有合规和域名检查。'}


def request_json(method, url, **kwargs):
    # No redirect following: endpoint URLs cannot be redirected to a different credential recipient.
    with httpx.Client(timeout=20, follow_redirects=False) as client:
        response = client.request(method, url, **kwargs)
    if response.status_code != 200:
        raise AppError('DELIVERY_AUTH_FAILED', message='服务商授权失败，请重新授权。')
    result = response.json()
    if not isinstance(result, dict):
        raise AppError('DELIVERY_AUTH_FAILED')
    return result


def identity(provider, encoded, client_id, nonce_hash):
    try:
        header = jwt.get_unverified_header(encoded)
        if header.get('alg') != 'RS256' or not header.get('kid'):
            raise ValueError('unsupported signing key')
        keys = request_json('GET', PROVIDERS[provider]['jwks'])['keys']
        key = next(key for key in keys if key.get('kid') == header['kid'])
        claims = jwt.decode(encoded, jwt.PyJWK.from_dict(key).key, algorithms=['RS256'], audience=client_id,
                            options={'require': ['exp', 'iat', 'iss', 'aud', 'sub', 'nonce']}, leeway=30)
        audiences = claims['aud'] if isinstance(claims['aud'], list) else [claims['aud']]
        # This application has no additional trusted audience/client configuration.
        if any(audience != client_id for audience in audiences):
            raise ValueError('untrusted additional audience')
        if 'azp' in claims and claims['azp'] != client_id:
            raise ValueError('authorized party mismatch')
        if not hmac.compare_digest(digest(claims['nonce']), nonce_hash):
            raise ValueError('nonce mismatch')
        if provider == 'google':
            if claims['iss'] not in {'https://accounts.google.com', 'accounts.google.com'} or claims.get('email_verified') is not True:
                raise ValueError('unverified Google identity')
            address = claims['email']
        else:
            tenant = str(uuid.UUID(claims['tid']))
            if claims['iss'] != f'https://login.microsoftonline.com/{tenant}/v2.0':
                raise ValueError('invalid Microsoft issuer')
            address = claims['preferred_username']
        address = validate_email(address, check_deliverability=False).normalized.lower()
        return address, claims['sub']
    except (ValueError, KeyError, StopIteration, TypeError, jwt.PyJWTError, EmailNotValidError):
        raise AppError('DELIVERY_AUTH_FAILED', message='授权身份校验失败，请重新授权。') from None


def token_values(provider, result):
    required = set(PROVIDERS[provider]['mail_scopes'])
    granted = set(str(result.get('scope', '')).split())
    if not required <= granted or str(result.get('token_type', '')).lower() != 'bearer':
        raise AppError('DELIVERY_AUTH_FAILED', message='未获得所需邮箱权限，请重新授权。')
    access = result.get('access_token')
    lifetime = result.get('expires_in')
    if not isinstance(access, str) or not access or not isinstance(lifetime, (int, float)) or not 60 <= lifetime <= 86400:
        raise AppError('DELIVERY_AUTH_FAILED')
    return encrypt(access), utcnow() + timedelta(seconds=lifetime)


def complete(db, provider, user_id, state, code=None, error=None):
    from app.models import Mailbox, MailboxOAuthAttempt, MailboxOAuthCredential, SendingDomain
    enabled, client_id, client_secret, _ = config(provider)
    if not enabled:
        raise AppError('DELIVERY_PROVIDER_UNAVAILABLE')
    attempt = db.scalar(select(MailboxOAuthAttempt).where(MailboxOAuthAttempt.state_hash == digest(state),
        MailboxOAuthAttempt.provider == provider, MailboxOAuthAttempt.created_by == user_id).with_for_update())
    if not attempt:
        raise AppError('COMMON_NOT_FOUND')
    if attempt.status == 'completed':
        mailbox = db.get(Mailbox, attempt.mailbox_id)
        if not mailbox or mailbox.deleted_at or mailbox.owner_id != user_id:
            raise AppError('COMMON_NOT_FOUND')
        return mailbox
    if attempt.status != 'pending' or attempt.expires_at <= utcnow():
        raise AppError('COMMON_INVALID_STATE', message='授权已过期或结果待确认，请重新开始授权。')
    if error:
        attempt.status, attempt.verifier_enc = 'cancelled', None
        db.commit()
        return None
    if not code:
        raise AppError('COMMON_REQUIRED')
    verifier = decrypt(attempt.verifier_enc)
    attempt_id, redirect, nonce_hash = attempt.id, attempt.redirect_uri, attempt.nonce_hash
    attempt.status, attempt.verifier_enc = 'exchanging', None
    db.commit()  # A one-time code with uncertain provider outcome is never blindly exchanged twice.
    try:
        result = request_json('POST', PROVIDERS[provider]['token'], data={'grant_type': 'authorization_code',
            'code': code, 'client_id': client_id, 'client_secret': client_secret, 'redirect_uri': redirect, 'code_verifier': verifier})
        address, subject = identity(provider, result.get('id_token'), client_id, nonce_hash)
        access_enc, expires_at = token_values(provider, result)
        refresh = result.get('refresh_token')
        if not isinstance(refresh, str) or not refresh:
            raise AppError('DELIVERY_AUTH_FAILED', message='服务商未提供离线刷新凭据，请重新授权。')
        from app.outreach.intents import lock_tenant
        lock_tenant(db)
        from app.models import User
        db.expire_all()
        actor = db.get(User, user_id)
        if (not actor or not actor.is_active or actor.status != 'active' or actor.deleted_at
                or 'mailbox.manage' not in actor.role.permission_codes):
            raise AppError('COMMON_FORBIDDEN')
        mailbox = db.scalar(select(Mailbox).where(Mailbox.address == address, Mailbox.deleted_at.is_(None)).with_for_update())
        if mailbox and mailbox.owner_id != user_id:
            raise AppError('COMMON_FORBIDDEN')
        domain_name = address.split('@')[1]
        domain = db.scalar(select(SendingDomain).where(SendingDomain.domain == domain_name, SendingDomain.deleted_at.is_(None)))
        if not domain:
            domain = SendingDomain(domain=domain_name, created_by=user_id)
            db.add(domain)
            db.flush()
        if not mailbox:
            count = db.scalar(select(func.count()).select_from(Mailbox).where(Mailbox.domain_id == domain.id, Mailbox.deleted_at.is_(None)))
            if count >= 3:
                raise AppError('DELIVERY_DOMAIN_MAILBOX_LIMIT')
            mailbox = Mailbox(address=address, owner_id=user_id, created_by=user_id,
                              display_name=address.split('@')[0], domain_id=domain.id)
            db.add(mailbox)
            db.flush()
        mailbox.provider = mailbox.oauth_provider = provider
        mailbox.oauth_status, mailbox.auth_status, mailbox.status = 'connected', 'ok', 'bound'
        mailbox.smtp_host, mailbox.smtp_port, mailbox.smtp_security = PROVIDERS[provider]['smtp'], 587, 'starttls'
        mailbox.imap_host, mailbox.imap_port, mailbox.imap_ssl = PROVIDERS[provider]['imap'], 993, True
        mailbox.smtp_username = mailbox.imap_username = address
        mailbox.smtp_password_enc = mailbox.imap_password_enc = None
        credential = db.scalar(select(MailboxOAuthCredential).where(MailboxOAuthCredential.mailbox_id == mailbox.id).with_for_update())
        if credential is None:
            credential = MailboxOAuthCredential(mailbox_id=mailbox.id)
            db.add(credential)
        credential.provider, credential.subject = provider, subject
        credential.access_token_enc, credential.refresh_token_enc, credential.expires_at = access_enc, encrypt(refresh), expires_at
        credential.status = 'connected'
        mailbox.oauth_meta = {'connected_at': utcnow().isoformat(), 'mode': 'oauth', 'scopes': PROVIDERS[provider]['mail_scopes']}
        attempt = db.get(MailboxOAuthAttempt, attempt_id)
        attempt.status, attempt.mailbox_id = 'completed', mailbox.id
        db.flush()
        return mailbox
    except Exception as exc:
        db.rollback()
        attempt = db.get(MailboxOAuthAttempt, attempt_id)
        attempt.status = 'failed'
        db.commit()
        if isinstance(exc, AppError):
            raise
        raise AppError('DELIVERY_AUTH_FAILED', message='授权未完成，请重新开始；不要重复提交授权码。') from None


def access_token(mailbox):
    """Commit rotated tokens independently BEFORE SMTP/IMAP or a caller transaction can fail.

The credential row is separate from Mailbox so an outbound transaction holding Mailbox
or wallet locks never blocks this refresh on a parent-row update.
    """
    from app.models import MailboxOAuthCredential
    if current_tenant() not in (None, mailbox.tenant_id):
        raise AppError('COMMON_FORBIDDEN')
    provider = mailbox.oauth_provider
    enabled, client_id, client_secret, _ = config(provider)
    if not enabled:
        raise AppError('DELIVERY_PROVIDER_UNAVAILABLE')
    failure = None
    with tenant_context(mailbox.tenant_id), SessionLocal() as db:
        credential = db.scalar(select(MailboxOAuthCredential).where(MailboxOAuthCredential.mailbox_id == mailbox.id).with_for_update())
        if not credential or credential.provider != provider or credential.status != 'connected':
            raise AppError('DELIVERY_AUTH_FAILED', message='邮箱授权已失效，请重新授权。')
        if credential.expires_at > utcnow() + timedelta(seconds=60):
            return decrypt(credential.access_token_enc)
        previous_refresh = credential.refresh_token_enc
        try:
            result = request_json('POST', PROVIDERS[provider]['token'], data={'grant_type': 'refresh_token',
                'refresh_token': decrypt(credential.refresh_token_enc), 'client_id': client_id, 'client_secret': client_secret})
            # Providers can omit unchanged scope/token_type on refresh; retained scopes were verified at consent.
            result.setdefault('scope', ' '.join(PROVIDERS[provider]['mail_scopes']))
            result.setdefault('token_type', 'Bearer')
            credential.access_token_enc, credential.expires_at = token_values(provider, result)
            if result.get('refresh_token'):
                credential.refresh_token_enc = encrypt(result['refresh_token'])
            db.commit()
            return decrypt(credential.access_token_enc)
        except Exception:
            db.rollback()
            try:
                credential = db.scalar(select(MailboxOAuthCredential).where(
                    MailboxOAuthCredential.mailbox_id == mailbox.id).with_for_update())
                # Do not overwrite a concurrent refresh that already committed a different credential.
                if credential and credential.refresh_token_enc == previous_refresh:
                    credential.status, credential.access_token_enc = 'expired', None
                    db.commit()
            except Exception:
                db.rollback()
            failure = AppError('DELIVERY_AUTH_FAILED', message='邮箱刷新失败或结果不明，请重新授权。')
    raise failure


def validate_transport(mailbox, protocol):
    meta = PROVIDERS.get(mailbox.oauth_provider)
    if not meta or mailbox.oauth_status != 'connected':
        raise AppError('DELIVERY_AUTH_FAILED')
    if protocol == 'smtp':
        valid = (mailbox.smtp_host == meta['smtp'] and mailbox.smtp_port == 587 and mailbox.smtp_security == 'starttls'
                 and mailbox.smtp_username == mailbox.address)
    else:
        valid = (mailbox.imap_host == meta['imap'] and mailbox.imap_port == 993 and mailbox.imap_ssl
                 and mailbox.imap_username == mailbox.address)
    if not valid:
        raise AppError('DELIVERY_AUTH_FAILED', message='OAuth邮箱传输配置不匹配，请重新连接。')


def sasl(address, token):
    if any(character in address + token for character in ('\r', '\n', '\x00', '\x01')):
        raise AppError('DELIVERY_AUTH_FAILED')
    return f'user={address}\x01auth=Bearer {token}\x01\x01'
