"""MoeMail API: permanent mailboxes, durable IDs and OTP polling."""
from __future__ import annotations

import json
import logging
import re
import secrets
import time
from dataclasses import asdict, dataclass
from urllib.parse import quote, urlsplit

import requests

from config import email as cfg
from core import db
from core.otp_utils import looks_like_openai_email
from core.generic_api_mail_client import _extract_yangyang_openai_code, _parse_generic_api_ts

logger = logging.getLogger(__name__)
_CONTEXT_CACHE: dict[str, 'MoeMailAccount'] = {}


class MoeMailError(RuntimeError):
    pass


class MoeMailTransportError(MoeMailError):
    pass


def normalize_base(value: str) -> str:
    base = str(value or '').strip().rstrip('/')
    p = urlsplit(base)
    if p.scheme not in {'http', 'https'} or not p.hostname or p.username or p.password or p.query or p.fragment:
        raise MoeMailError('MoeMail API 地址无效，请填写服务根地址')
    return base


def validate_config(*, require_domain: bool = True, api_base=None, api_key=None) -> tuple[str, str, str]:
    base = normalize_base(cfg.MOEMAIL_API_BASE if api_base is None else api_base)
    key = str(cfg.MOEMAIL_API_KEY if api_key is None else api_key).strip()
    if not key:
        raise MoeMailError('请在配置 → 邮箱 / OTP → MoeMail 填写 API Key')
    domain = str(cfg.MOEMAIL_DOMAIN or '').strip().lower()
    if require_domain and (not domain or not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', domain) or '.' not in domain):
        raise MoeMailError('请获取并选择 MoeMail 邮箱域名')
    return base, key, domain


class MoeMailClient:
    def __init__(self, *, api_base=None, api_key=None):
        self.base, self.key, _ = validate_config(require_domain=False, api_base=api_base, api_key=api_key)
        self.deadline = None
        self.http = requests.Session()
        self.http.trust_env = False

    def close(self):
        self.http.close()

    def request(self, method: str, path: str, *, params=None, body=None) -> dict:
        timeout = max(1, min(120, int(cfg.MOEMAIL_REQUEST_TIMEOUT)))
        if self.deadline is not None:
            timeout = min(timeout, self.deadline - time.monotonic())
            if timeout <= 0:
                raise MoeMailTransportError("MoeMail 取码等待时间已结束")
        try:
            response = self.http.request(method, self.base + path, params=params, json=body,
                headers={'X-API-Key': self.key, 'Accept': 'application/json'},
                timeout=timeout, allow_redirects=False)
        except requests.RequestException as exc:
            raise MoeMailTransportError(f'MoeMail 网络请求失败：{type(exc).__name__}') from None
        if response.status_code in (401, 403):
            raise MoeMailError('MoeMail API Key 无效或没有访问权限')
        if response.status_code == 429:
            raise MoeMailError('MoeMail 请求限流，请稍后重试')
        if not 200 <= response.status_code < 300:
            # Never print arbitrary response bodies or authenticated request headers.
            text = (response.text or '').lower()
            if any(x in text for x in ('maxemails', 'limit', 'quota', '上限', '额度')):
                raise MoeMailError('MoeMail 邮箱额度已耗尽或达到创建上限')
            if 'domain' in text or '域名' in text:
                raise MoeMailError('MoeMail 域名无效或不可用')
            raise MoeMailError(f'MoeMail 请求失败：HTTP {response.status_code}')
        try:
            result = response.json()
        except ValueError:
            raise MoeMailError('MoeMail 返回非 JSON 响应') from None
        if not isinstance(result, dict):
            raise MoeMailError('MoeMail 响应格式无效')
        return result

    def domains(self) -> list[str]:
        value = self.request('GET', '/api/config').get('emailDomains')
        items = re.split(r'[,;\s]+', value) if isinstance(value, str) else value
        if not isinstance(items, list):
            raise MoeMailError('MoeMail 配置缺少 emailDomains')
        domains = list(dict.fromkeys(str(v).strip().lower() for v in items if str(v).strip()))
        if not domains:
            raise MoeMailError('MoeMail 没有可用邮箱域名')
        return domains

    def pages(self, path: str, field: str):
        cursor = None
        seen = set()
        for _ in range(100):
            data = self.request('GET', path, params={'cursor': cursor} if cursor else None)
            items = data.get(field)
            if not isinstance(items, list):
                raise MoeMailError(f'MoeMail 响应缺少 {field} 列表')
            yield [item for item in items if isinstance(item, dict)]
            cursor = data.get('nextCursor')
            if not cursor:
                return
            if not isinstance(cursor, str) or cursor in seen:
                raise MoeMailError('MoeMail 分页游标无效或重复')
            seen.add(cursor)
        raise MoeMailError('MoeMail 分页超过100页，已停止查询')

    def find_mailbox(self, email: str):
        for items in self.pages('/api/emails', 'emails'):
            for item in items:
                if str(item.get('address') or '').lower() == email.lower() and item.get('id'):
                    return item
        return None


@dataclass
class MoeMailAccount:
    email: str
    email_id: str
    api_base: str
    expiry_time: int = 0
    source: str = 'moemail'
    status: str = 'created'


def _store(account: MoeMailAccount):
    db.save_provider_mailbox(asdict(account))
    _CONTEXT_CACHE[account.email.lower()] = account
    return account


def get_account_context(email: str) -> MoeMailAccount | None:
    target = email.lower().strip()
    if target in _CONTEXT_CACHE:
        return _CONTEXT_CACHE[target]
    metadata = db.get_provider_mailbox('moemail', target)
    if not metadata:
        row = db.get_account_by_email(target) or {}
        raw = row.get('extra_json') or {}
        try:
            extra = json.loads(raw) if isinstance(raw, str) else raw
            metadata = extra.get('email_service') or {}
        except (ValueError, AttributeError):
            metadata = {}
    if metadata and metadata.get('source') == 'moemail' and str(metadata.get('email') or '').lower() == target:
        account = MoeMailAccount(email=target, email_id=str(metadata.get('email_id') or ''),
            api_base=str(metadata.get('api_base') or ''), status=metadata.get('status', 'created'))
        _CONTEXT_CACHE[target] = account
        return account
    return None


def get_account_context_metadata(email: str) -> dict | None:
    account = get_account_context(email)
    return asdict(account) if account else None


def restore_account_context(client: MoeMailClient, email: str) -> MoeMailAccount:
    account = get_account_context(email)
    if account and account.api_base != client.base:
        raise MoeMailError('该 MoeMail 邮箱属于不同 API 地址，请恢复原服务配置后收码')
    if account and account.email_id:
        return account
    item = client.find_mailbox(email)
    if not item:
        raise MoeMailError('MoeMail 邮箱列表找不到该地址；未创建替代邮箱')
    return _store(MoeMailAccount(email=email.lower(), email_id=str(item['id']), api_base=client.base))


def pick_account() -> MoeMailAccount:
    base, _, domain = validate_config()
    client = MoeMailClient()
    try:
        if domain not in client.domains():
            raise MoeMailError('所选 MoeMail 域名已不可用，请重新获取域名')
        name = 'm' + secrets.token_hex(8)
        email = f'{name}@{domain}'
        pending = _store(MoeMailAccount(email=email, email_id='', api_base=base, status='pending'))
        logger.info('[MoeMail] 创建永久邮箱：%s，expiryTime=0', email)
        uncertain = False
        try:
            client.request('POST', '/api/emails/generate', body={'name': name, 'domain': domain, 'expiryTime': 0})
        except MoeMailTransportError:
            uncertain = True
            logger.warning('[MoeMail] 创建结果未确认，仅查询候选地址，不重复创建：%s', email)
        # Read-back uses the verified list schema, not an assumed generate response.
        for attempt in range(3):
            item = client.find_mailbox(email)
            if item:
                expires = str(item.get('expiresAt') or '')
                if expires and not expires.startswith('9999-'):
                    raise MoeMailError(f'MoeMail 未返回永久有效期，已保留邮箱但不用于任务：{email}')
                return _store(MoeMailAccount(email=email, email_id=str(item['id']), api_base=base))
            if attempt < 2:
                time.sleep(0.5)
        pending.status = 'unknown' if uncertain else 'unconfirmed'
        _store(pending)
        raise MoeMailError(f'MoeMail 创建后未能确认邮箱 ID：{email}；未重复创建')
    finally:
        client.close()


def release_account(email: str, status: str = 'available', note: str | None = None):
    account = get_account_context(email)
    if account:
        account.status = status
        _store(account)
    _CONTEXT_CACHE.pop(email.lower(), None)
    logger.info('[MoeMail] 本地任务释放邮箱：%s，保留永久邮箱，不删除或重新分配', email)


def fetch_latest_otp(email: str, after_ts: float | None = None, max_wait: int | None = None,
                     poll_interval: float | None = None, settle_seconds: float | None = None) -> str:
    client = MoeMailClient()
    try:
        deadline = time.monotonic() + (max_wait if max_wait is not None else cfg.OTP_MAX_WAIT)
        client.deadline = deadline
        account = restore_account_context(client, email)
        path = '/api/emails/' + quote(account.email_id, safe='')
        interval = poll_interval if poll_interval is not None else cfg.OTP_POLL_INTERVAL
        settle = settle_seconds if settle_seconds is not None else cfg.OTP_SETTLE_SECONDS
        best = None
        best_ts = float('-inf')
        until = None
        logger.info('[MoeMail] 开始轮询邮箱：%s', email)
        while time.monotonic() < deadline:
            try:
                messages = [item for page in client.pages(path, 'messages') for item in page]
                messages.sort(key=lambda m: _parse_generic_api_ts(m.get('received_at')) or 0, reverse=True)
                for message in messages:
                    timestamp = _parse_generic_api_ts(message.get('received_at'))
                    if timestamp is None or (after_ts and timestamp + 2 < after_ts) or timestamp < best_ts:
                        continue
                    body = '\n'.join(str(message.get(k) or '') for k in ('content', 'html'))
                    if not looks_like_openai_email({'from': message.get('from_address'), 'subject': message.get('subject'), 'content': body}):
                        continue
                    code = _extract_yangyang_openai_code(str(message.get('subject') or ''), body)
                    if not code and message.get('id'):
                        detail = client.request('GET', path + '/' + quote(str(message['id']), safe='')).get('message')
                        if isinstance(detail, dict):
                            code = _extract_yangyang_openai_code(str(detail.get('subject') or message.get('subject') or ''),
                                '\n'.join(str(detail.get(k) or '') for k in ('content', 'html')))
                    if not code:
                        continue
                    if code != best:
                        best, best_ts, until = code, timestamp, time.monotonic() + max(0, settle)
                        logger.info('[MoeMail] 锁定验证码：%s，等待稳定', code)
                    else:
                        best_ts = max(best_ts, timestamp)
                    break
                if best and until is not None and time.monotonic() >= until:
                    logger.info('[MoeMail] 返回验证码：%s', best)
                    return best
            except MoeMailTransportError as exc:
                logger.warning('[MoeMail] 取码网络异常：%s', exc)
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(max(0.05, interval), remaining))
        raise MoeMailError('等待 MoeMail 新验证码超时')
    finally:
        client.close()
