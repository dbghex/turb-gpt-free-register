"""Protocol-only account password setup, based on the add-password HAR.

No captured credentials are reused. All challenges and OAuth state belong to
the current session; unknown states fail closed rather than guessing endpoints.
"""
from __future__ import annotations

import json
import logging
import secrets
import string
import time
from urllib.parse import urlencode, urljoin, urlsplit

import pyotp

logger = logging.getLogger(__name__)


class PasswordSetupError(RuntimeError):
    """A safe, credential-free error suitable for persisted task status."""


class PasswordResultUnknown(PasswordSetupError):
    pass


def choose_password(configured: str = "") -> str:
    if configured:
        if len(configured) < 12:
            raise PasswordSetupError("配置密码至少需要 12 个字符")
        return configured
    groups = (string.ascii_uppercase, string.ascii_lowercase, string.digits, "!@#_-+")
    chars = [secrets.choice(group) for group in groups]
    chars += [secrets.choice("".join(groups)) for _ in range(16 - len(chars))]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def safe_error(exc: Exception) -> str:
    # Transport errors can contain authenticated proxy URLs and OAuth URLs.
    if isinstance(exc, PasswordSetupError):
        return str(exc)
    return f"{type(exc).__name__}: 协议请求或后台处理失败（敏感详情已省略）"


def _json_response(resp, stage: str) -> dict:
    if not 200 <= resp.status_code < 300:
        headers = getattr(resp, "headers", {}) or {}
        content_type = str(headers.get("content-type") or "").split(";", 1)[0]
        # Log classifications only, never HTML, request URLs or cookie values.
        body = str(getattr(resp, "text", "") or "").lower()
        challenge = (headers.get("cf-mitigated") == "challenge"
                     or "cf-chl-" in body or "just a moment" in body)
        logger.warning("[密码] %s被拒绝：HTTP %s，响应类型=%s，挑战页面=%s",
                       stage, resp.status_code, content_type or "未知", challenge)
        reason = ""
        try:
            error = resp.json().get("error") or {}
            code = error.get("code") if isinstance(error, dict) else None
            reason = {
                "add_password_ineligible": "账号不符合添加密码条件",
                "password_contains_user_info": "密码包含用户信息",
                "password_too_weak": "密码强度不足",
                "password_already_used": "密码已被使用",
                "invalid_otp": "验证码无效",
            }.get(code, "")
        except Exception:
            pass
        if challenge:
            reason = "服务端返回访问挑战，当前协议会话无法继续"
        raise PasswordSetupError(f"{stage}失败：HTTP {resp.status_code}" + (f"，{reason}" if reason else ""))
    try:
        result = resp.json()
    except Exception:
        raise PasswordSetupError(f"{stage}返回了非 JSON 响应") from None
    if not isinstance(result, dict):
        raise PasswordSetupError(f"{stage}响应格式无效")
    return result


def _trusted_url(url: str) -> str:
    url = urljoin("https://auth.openai.com/", str(url or ""))
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in {"auth.openai.com", "chatgpt.com"}
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise PasswordSetupError("认证响应包含非预期跳转地址")
    return url


def _navigate(session, url: str) -> str:
    for _ in range(10):
        url = _trusted_url(url)
        headers = (session.get_chatgpt_navigate_headers() if urlsplit(url).hostname == "chatgpt.com"
                   else session.get_auth_navigate_headers())
        resp = session.get(url, headers=headers, allow_redirects=False)
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("location") or resp.headers.get("Location")
            if not location:
                raise PasswordSetupError("认证跳转缺少 Location")
            url = urljoin(url, location)
            continue
        if not 200 <= resp.status_code < 300:
            raise PasswordSetupError(f"认证页面加载失败：HTTP {resp.status_code}")
        return url
    raise PasswordSetupError("认证跳转次数超出限制")


def _post_auth(session, path: str, body: dict, *, referer: str, flow: str | None = None,
               password_submission: bool = False) -> dict:
    headers = session.get_auth_headers(referer=referer)
    if flow:
        from core.openai_auth import request_sentinel_token, build_sentinel_header
        token, so = build_sentinel_header(session, request_sentinel_token(session, flow), flow)
        headers["openai-sentinel-token"] = token
        if so:
            headers["openai-sentinel-so-token"] = so
    try:
        resp = session.post("https://auth.openai.com" + path, headers=headers,
                            data=json.dumps(body), allow_redirects=False)
    except Exception:
        if password_submission:
            raise PasswordResultUnknown("密码提交未收到确认，结果未知；未自动重试") from None
        raise
    if password_submission and (resp.status_code >= 500 or 300 <= resp.status_code < 400):
        raise PasswordResultUnknown("密码提交结果未确认；未自动重试")
    try:
        return _json_response(resp, "添加密码" if password_submission else "重认证")
    except PasswordSetupError:
        if password_submission and 200 <= resp.status_code < 300:
            raise PasswordResultUnknown("密码提交成功响应无法解析，结果未确认") from None
        raise


def _trigger(session, email: str) -> str:
    headers = session.get_nextauth_headers(referer="https://chatgpt.com/")
    logger.info("[密码] 读取登录 Providers，初始化重认证流程")
    _json_response(session.get("https://chatgpt.com/api/auth/providers", headers=headers), "获取 Providers")
    logger.info("[密码] 获取 CSRF 并发起添加密码重认证")
    csrf = _json_response(session.get("https://chatgpt.com/api/auth/csrf", headers=headers), "获取 CSRF").get("csrfToken")
    if not csrf:
        raise PasswordSetupError("缺少 CSRF Token")
    params = {"login_hint": email, "reauth": "password", "post_login_add_password": "true",
              "max_age": "0", "ext-oai-did": session.device_id}
    headers = {**headers, "content-type": "application/x-www-form-urlencoded", "origin": "https://chatgpt.com"}
    resp = session.post("https://chatgpt.com/api/auth/signin/openai?" + urlencode(params),
                        headers=headers, data=urlencode({"callbackUrl": "https://chatgpt.com/",
                                                        "csrfToken": csrf, "json": "true"}), allow_redirects=False)
    url = _json_response(resp, "发起添加密码重认证").get("url")
    if not url:
        raise PasswordSetupError("重认证响应缺少授权地址")
    return _trusted_url(url)


def complete_reauthentication(session, email: str, initial_url: str, *, after_ts: float,
                              password: str = "", totp_secret: str = "",
                              target: str = "password") -> str:
    """Follow actual server states; MFA is optional, never a prerequisite."""
    from core.email_provider import wait_for_otp

    url = _trusted_url(initial_url)
    result = {}
    visited = set()
    for _ in range(8):
        url = _navigate(session, url)
        path = urlsplit(url).path.rstrip("/")
        page = result.get("page") or {}
        kind = page.get("type") or ""
        if path == "/reset-password/new-password" or kind == "reset_password_new_password":
            if target != "password":
                raise PasswordSetupError("2FA 重认证意外进入添加密码页面")
            return url
        if urlsplit(url).hostname == "chatgpt.com":
            if target == "callback":
                return url
            raise PasswordSetupError("未进入添加密码页面，不能确认已设置密码")
        step = (kind, path)
        if step in visited:
            raise PasswordSetupError("重认证状态重复，已停止以避免重复提交")
        visited.add(step)
        if path == "/email-verification" or kind in {"email_otp_verification", "email_verification"}:
            logger.info("[密码] 等待邮箱重认证验证码")
            code = wait_for_otp(email, after_ts=after_ts)
            logger.info("[密码] 已收到邮箱验证码：%s，正在提交重认证", code)
            result = _post_auth(session, "/api/accounts/email-otp/validate", {"code": code},
                                referer=url, flow="email_otp_validate")
        elif path == "/log-in/password" or kind in {"password", "login_password"}:
            if not password:
                raise PasswordSetupError("重认证要求已有登录密码，但本地没有已确认密码")
            result = _post_auth(session, "/api/accounts/password/verify", {"password": password},
                                referer=url, flow="password_verify")
        elif path.startswith("/mfa-challenge") or kind == "mfa_challenge":
            payload = page.get("payload") or {}
            factors = payload.get("factors") or (result.get("oai-client-auth-session") or {}).get("mfa_factors") or []
            factor = next((f for f in factors if f.get("factor_type") == "totp" and not f.get("is_recovery")), None)
            factor_id = (factor or {}).get("id") or payload.get("factor_id")
            if not factor_id and path.startswith("/mfa-challenge/"):
                factor_id = path.rsplit("/", 1)[-1]
            if not factor_id or not totp_secret:
                raise PasswordSetupError("重认证要求 TOTP，但缺少可用验证因子或账号密钥")
            logger.info("[密码] 服务端要求已有 TOTP，开始验证")
            _post_auth(session, "/api/accounts/mfa/issue_challenge",
                       {"id": factor_id, "type": "totp", "force_fresh_challenge": False}, referer=url)
            totp_code = pyotp.TOTP(totp_secret).now()
            logger.info("[密码] 提交 TOTP 验证码：%s", totp_code)
            result = _post_auth(session, "/api/accounts/mfa/verify",
                                {"id": factor_id, "type": "totp", "code": totp_code}, referer=url)
        else:
            raise PasswordSetupError("服务端返回尚不支持的重认证状态")
        url = result.get("continue_url")
        if not url:
            raise PasswordSetupError("重认证响应缺少后续地址")
        url = _trusted_url(url)
    raise PasswordSetupError("重认证步骤超出限制")


def setup_password(session, email: str, password: str, *, totp_secret: str = "", on_confirmed=None) -> dict:
    """Persist immediately after confirmation, before the fallible OAuth callback."""
    from core.account_export import fetch_session

    choose_password(password)  # validate without logging the supplied value
    after_ts = time.time()
    url = complete_reauthentication(session, email, _trigger(session, email),
                                   after_ts=after_ts, totp_secret=totp_secret)
    logger.info("[密码] 重认证完成，提交登录密码：%s", password)
    result = _post_auth(session, "/api/accounts/password/add", {"password": password},
                        referer=url, flow="password_reset", password_submission=True)
    callback = result.get("continue_url")
    if (result.get("page") or {}).get("type") != "external_url" or not callback:
        raise PasswordResultUnknown("添加密码响应未包含已知成功状态，结果未确认")
    parsed = urlsplit(_trusted_url(callback))
    if parsed.hostname != "chatgpt.com" or parsed.path != "/api/auth/callback/openai":
        raise PasswordResultUnknown("添加密码响应未包含预期回调，结果未确认")
    if on_confirmed:
        on_confirmed(password)
    logger.info("[密码] 服务端已确认添加密码成功")
    try:
        logger.info("[密码] 完成 OAuth 回调并刷新会话")
        _navigate(session, callback)
        session_info = fetch_session(session)
        return {"ok": True, "status": "success", "session": session_info,
                "access_token": session_info["accessToken"], "message": "登录密码已设置"}
    except Exception as exc:
        return {"ok": True, "status": "success", "message": "登录密码已设置；会话刷新失败",
                "session_refresh_error": safe_error(exc)}
