"""Country checkout quotes. Creates an open checkout; never confirms payment.

Only an allowlist of quote fields is persisted. Captured HAR credentials and
customer/session secrets are never reused or returned to the UI.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import uuid
from copy import copy, deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests

from config.proxy import normalize_proxy_url
from core.chatgpt_plan import normalize_token, token_claims
from core.session import BrowserSession, close_browser_session

logger = logging.getLogger(__name__)
CONFIG_PATH = Path(__file__).resolve().parent.parent / "checkout.yaml"
CHECKOUT_PATH = "/backend-api/payments/checkout"
PRICING_PATH = "/backend-api/checkout_pricing_config/configs/"
_ROUND_ROBIN = {}
_PROXY_LOCK = threading.Lock()
_EXPONENTS = {"INR": 2, "PHP": 2, "BRL": 2, "VND": 0, "JPY": 0}
STRIPE_API_VERSION = "2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1"


@dataclass(frozen=True, repr=False)
class PlanAuthContext:
    """In-memory snapshot of the successful plan request; never persisted."""
    token: str
    headers: dict
    cookies: tuple
    session_options: dict


def capture_plan_context(session, token: str, claims: dict) -> PlanAuthContext:
    from core.chatgpt_plan import _common_headers
    options = {name: getattr(session, name) for name in (
        "device_id", "oai_session_id", "auth_session_logging_id", "sentinel_sid", "fingerprint_seed"
    ) if getattr(session, name, None)}
    options["browser_profile"] = deepcopy(session.browser_profile)
    # Cookie objects preserve domain/path/secure/expiry, unlike a flattened dict.
    cookies = tuple(copy(cookie) for cookie in session.session.cookies.jar
                    if cookie.domain.lstrip(".").lower() in {"chatgpt.com", "auth.openai.com"})
    headers = {k: v for k, v in _common_headers(session, token, claims).items()
               if k.lower() not in {"cookie", "host", "content-length"}}
    return PlanAuthContext(normalize_token(token), headers, cookies, options)


class CheckoutError(RuntimeError):
    def __init__(self, message: str, *, status="failed", http_status=None, error_code=None, diagnostic=None):
        super().__init__(message)
        self.status = status
        self.http_status = http_status
        self.error_code = error_code
        self.diagnostic = diagnostic or {}
        self.stop_remaining = http_status in (401, 429)


def stamp() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass(frozen=True)
class CountryPlan:
    country: str
    currency: str
    proxies: tuple[str, ...] = field(default=(), repr=False)


@dataclass(frozen=True)
class CheckoutConfig:
    common: dict
    plans: tuple[CountryPlan, ...]
    proxy_enabled: bool

    def public_summary(self):
        return {**deepcopy(self.common), "countries": [
            {"country": p.country, "currency": p.currency} for p in self.plans],
            "proxy_enabled": self.proxy_enabled}


def load_config(path: Path | None = None) -> CheckoutConfig | None:
    path = path or CONFIG_PATH
    if not path.exists():
        return None
    try:
        import yaml
        if path.stat().st_size > 65536:
            raise CheckoutError("checkout.yaml 超过64KB")
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except CheckoutError:
        raise
    except Exception:
        # YAML parser errors can embed the full proxy line.
        raise CheckoutError("checkout.yaml 读取失败或 YAML 格式无效") from None
    if not isinstance(raw, dict) or not isinstance(raw.get("plans"), dict) or not raw["plans"]:
        raise CheckoutError("checkout.yaml 需要非空 plans 国家列表")
    if len(raw["plans"]) > 32:
        raise CheckoutError("checkout.yaml 最多支持32个国家")
    common = {}
    for key in ("entry_point", "plan_name", "checkout_ui_mode"):
        value = raw.get(key)
        if not isinstance(value, str) or not value or len(value) > 100:
            raise CheckoutError(f"checkout.yaml 的 {key} 无效")
        common[key] = value
    if common["checkout_ui_mode"] != "custom":
        raise CheckoutError("当前只支持 custom checkout 响应")
    promo = raw.get("promo_campaign")
    if (not isinstance(promo, dict) or not isinstance(promo.get("promo_campaign_id"), str)
            or not promo["promo_campaign_id"] or len(promo["promo_campaign_id"]) > 150
            or not isinstance(promo.get("is_coupon_from_query_param"), bool)):
        raise CheckoutError("checkout.yaml 需要有效的 promo_campaign，才能复现 plus.har 的优惠报价")
    common["promo_campaign"] = {
        "promo_campaign_id": promo["promo_campaign_id"],
        "is_coupon_from_query_param": promo["is_coupon_from_query_param"],
    }
    proxy = raw.get("proxy", {})
    if not isinstance(proxy, dict) or not isinstance(proxy.get("enabled", False), bool):
        raise CheckoutError("checkout.yaml 的 proxy.enabled 必须是布尔值")
    if proxy.get("strategy", "round_robin") != "round_robin":
        raise CheckoutError("checkout 代理策略只支持 round_robin")
    plans = []
    for country, plan in raw["plans"].items():
        if not isinstance(country, str) or not re.fullmatch(r"[A-Z]{2}", country) or not isinstance(plan, dict):
            raise CheckoutError("plans 国家键必须是两位大写国家代码")
        billing = plan.get("billing_details") or {}
        currency = billing.get("currency") if isinstance(billing, dict) else None
        if (not isinstance(billing, dict) or billing.get("country") != country
                or not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency)):
            raise CheckoutError(f"{country} 的账单国家或币种无效")
        pool = plan.get("proxy_pool") or []
        if not isinstance(pool, list) or any(not isinstance(p, str) for p in pool):
            raise CheckoutError(f"{country} 的 proxy_pool 必须是文本列表")
        plans.append(CountryPlan(country, currency, tuple(p.strip() for p in pool if p.strip())))
    return CheckoutConfig(common, tuple(plans), proxy.get("enabled", False))


def build_body(config: CheckoutConfig, plan: CountryPlan) -> dict:
    return {
        "entry_point": config.common["entry_point"],
        "plan_name": config.common["plan_name"],
        "billing_details": {"country": plan.country, "currency": plan.currency},
        "promo_campaign": deepcopy(config.common["promo_campaign"]),
    }


def select_proxy(config: CheckoutConfig, plan: CountryPlan) -> str:
    if not config.proxy_enabled:
        return ""  # Explicit direct mode; never inherit the general proxy pool.
    if not plan.proxies:
        raise CheckoutError(f"{plan.country} 未配置国家代理")
    key = (plan.country, hashlib.sha256("\n".join(plan.proxies).encode()).hexdigest())
    with _PROXY_LOCK:
        index = _ROUND_ROBIN.get(key, 0)
        _ROUND_ROBIN[key] = (index + 1) % len(plan.proxies)
    try:
        proxy = normalize_proxy_url(plan.proxies[index % len(plan.proxies)])
        parts = urlsplit(proxy)
        if parts.scheme not in {"http", "https", "socks5", "socks5h"} or not parts.hostname or not parts.port:
            raise ValueError()
    except Exception:
        raise CheckoutError(f"{plan.country} 代理格式无效") from None
    return proxy


def _methods(value, *, custom=False):
    if value is None:
        return [], False
    if not isinstance(value, list):
        return [], True
    methods, unknown = [], False
    for item in value:
        code = item.get("type") if custom and isinstance(item, dict) else item
        if not isinstance(code, str) or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,63}", code):
            unknown = True
            continue
        code = "UPI" if code.lower() == "upi" else code
        if code.lower() not in {m.lower() for m in methods}:
            methods.append(code)
    return methods, unknown


def _amount_display(amount_minor: int, exponent: int) -> str:
    value = format(Decimal(amount_minor).scaleb(-exponent), "f")
    return value.rstrip("0").rstrip(".") if "." in value else value


def _currency_exponent(plan: CountryPlan, pricing: dict | None) -> int:
    currency_config = (pricing or {}).get("currency_config") or {}
    if (str((pricing or {}).get("country_code") or "").upper() == plan.country
            and str(currency_config.get("symbol_code") or "").upper() == plan.currency):
        exponent = currency_config.get("minor_unit_exponent")
        if type(exponent) is int and 0 <= exponent <= 4:
            return exponent
    exponent = _EXPONENTS.get(plan.currency)
    if exponent is None:
        raise CheckoutError(f"{plan.country} 无法确定币种精度")
    return exponent


def _stripe_init_form(publishable_key: str, *, locale: str = "zh-CN",
                      timezone: str = "Asia/Shanghai", stripe_js_id: str | None = None) -> dict:
    """Fields observed in plus.har; the session ID is supplied in the URL."""
    return {
        "browser_locale": locale,
        "browser_timezone": timezone,
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": stripe_js_id or str(uuid.uuid4()),
        "elements_session_client[locale]": locale,
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "auto",
        "elements_options_client[saved_payment_method][enable_redisplay]": "auto",
        "key": publishable_key,
        "_stripe_version": STRIPE_API_VERSION,
    }


def parse_stripe_init(checkout: dict, stripe: dict, plan: CountryPlan,
                      pricing: dict | None = None, *, preflight: dict | None = None) -> dict:
    """Parse invoice amount_due without persisting Stripe customer secrets."""
    if checkout.get("checkout_provider") != "stripe":
        raise CheckoutError("checkout 未返回 Stripe 会话")
    if checkout.get("billing_details") != {"country": plan.country, "currency": plan.currency}:
        raise CheckoutError("checkout 国家或币种与请求不一致")
    if (not isinstance(stripe, dict) or stripe.get("object") != "checkout.session"
            or stripe.get("status") != "open"):
        raise CheckoutError("Stripe init 未返回开放结账会话")
    invoice = stripe.get("invoice") or {}
    amount = invoice.get("amount_due") if isinstance(invoice, dict) else None
    if type(amount) is not int or amount < 0:
        raise CheckoutError("Stripe init 缺少有效 invoice.amount_due")
    if (str(invoice.get("currency") or "").upper() != plan.currency
            or str(stripe.get("currency") or "").upper() != plan.currency):
        raise CheckoutError("Stripe init 币种与请求不一致")
    methods, unknown = _methods(stripe.get("payment_method_types"))
    if stripe.get("payment_method_types") is None or unknown:
        raise CheckoutError("Stripe init 付款方式格式无效")
    exponent = _currency_exponent(plan, pricing)
    return {
        "country": plan.country, "requested_currency": plan.currency, "currency": plan.currency,
        "status": "success", "provider": "stripe", "amount_minor": amount,
        "currency_exponent": exponent, "amount_display": _amount_display(amount, exponent),
        "payment_method_types": methods, "custom_payment_methods": [],
        "can_confirm": None, "automatic_tax_enabled": checkout.get("automatic_tax_enabled") is True,
        "promo_check_state": (preflight or {}).get("promo_check_state"),
        "app_store_subscription_in_retry": (preflight or {}).get("app_store_subscription_in_retry"),
        "checked_at": stamp(), "error": None,
    }


def _init_stripe_checkout(checkout: dict, plan: CountryPlan, pricing: dict | None,
                          proxy: str, timeout: float, preflight: dict | None = None,
                          *, http_factory=None) -> dict:
    session_id = checkout.get("checkout_session_id")
    key = checkout.get("publishable_key")
    if (not isinstance(session_id, str) or not session_id.startswith("cs_")
            or not isinstance(key, str) or not key.startswith("pk_")):
        raise CheckoutError("checkout 缺少 Stripe session ID 或 publishable key", status="unknown")
    url = "https://api.stripe.com/v1/payment_pages/" + quote(session_id, safe="") + "/init"
    http = (http_factory or requests.Session)()
    try:
        http.trust_env = False
        if proxy:
            http.proxies = {"http": proxy, "https": proxy}
        from core.chatgpt_plan import _plan_check_settings, _retry_wait_seconds, _retryable_plan_error
        _, attempts, base_delay = _plan_check_settings(None, None, None)
        for attempt in range(1, attempts + 1):
            response = None
            try:
                response = http.post(url, data=_stripe_init_form(key), headers={
                    "Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": "https://js.stripe.com", "Referer": "https://js.stripe.com/",
                }, timeout=max(1.0, min(20.0, timeout)), allow_redirects=False)
                if response.status_code == 200 or not _retryable_plan_error(response.status_code) or attempt >= attempts:
                    break
                failure = f"HTTP {response.status_code}"
            except requests.RequestException as exc:
                if attempt >= attempts:
                    raise
                failure = type(exc).__name__
            wait_seconds = _retry_wait_seconds(response, base_delay, attempt)
            logger.warning("[Checkout] %s Stripe init 临时失败，第 %s/%s 次，%.1fs 后重试：%s",
                           plan.country, attempt, attempts, wait_seconds, failure)
            if wait_seconds > 0:
                time.sleep(wait_seconds)
        if response.status_code != 200:
            raise CheckoutError(f"Stripe init HTTP {response.status_code}",
                                status="unknown", http_status=response.status_code)
        try:
            payload = response.json()
        except ValueError:
            raise CheckoutError("Stripe init 返回非 JSON", status="unknown") from None
        return parse_stripe_init(checkout, payload, plan, pricing, preflight=preflight)
    except requests.RequestException as exc:
        raise CheckoutError(f"Stripe init 网络异常（{type(exc).__name__}）", status="unknown") from None
    finally:
        http.close()


def parse_quote(payload: dict, plan: CountryPlan, pricing: dict | None = None, *,
                preflight: dict | None = None) -> dict:
    if isinstance(payload, dict):
        current = payload
        for _ in range(3):
            if isinstance(current.get("checkout_state"), dict):
                payload = current
                break
            wrapper = next((current.get(k) for k in ("data", "result", "checkout", "checkout_session")
                            if isinstance(current.get(k), dict)), None)
            if wrapper is None:
                break
            current = wrapper
    if not isinstance(payload, dict) or not isinstance(payload.get("checkout_state"), dict):
        raise CheckoutError("checkout 响应缺少结账状态")
    state = payload["checkout_state"]
    currency = str(state.get("currency") or "").upper()
    billing = payload.get("billing_details") or {}
    state_country = ((state.get("billingAddress") or {}).get("address") or {}).get("country")
    if (currency != plan.currency or billing.get("country", plan.country) != plan.country
            or str(billing.get("currency", currency)).upper() != plan.currency
            or (state_country and state_country != plan.country)):
        raise CheckoutError("checkout 返回国家或币种与请求不一致")
    methods, unknown = _methods(payload.get("payment_method_types"))
    custom, custom_unknown = _methods(payload.get("custom_payment_methods"), custom=True)
    for method in custom:
        if method.lower() not in {m.lower() for m in methods}:
            methods.append(method)
    total = ((state.get("total") or {}).get("total") or {}).get("minorUnitsAmount")
    try:
        exponent = _currency_exponent(plan, pricing)
    except CheckoutError:
        exponent = None
    amount, error = None, None
    if type(total) is not int or total < 0:
        error = "金额未返回或格式无效"
    elif exponent is None:
        error = "无法确定币种精度"
    else:
        amount = _amount_display(total, exponent)
    if unknown or custom_unknown:
        # Keep the valid methods visible; opaque custom IDs have no payment
        # type in the response and cannot safely be named.
        error = error or "部分付款方式结构无法识别"
    if payload.get("payment_method_types") is None and payload.get("custom_payment_methods") is None:
        error = error or "付款方式未返回"
    if payload.get("status") != "open" or payload.get("payment_status") not in {"unpaid", "no_payment_required"}:
        raise CheckoutError("checkout 未返回开放的未支付结账会话")
    return {"country": plan.country, "requested_currency": plan.currency, "currency": currency,
            "status": "partial" if error else "success", "amount_minor": total if type(total) is int else None,
            "provider": "open_ai",
            "currency_exponent": exponent, "amount_display": amount, "payment_method_types": methods,
            "custom_payment_methods": custom, "payment_methods_unknown": unknown or custom_unknown,
            "can_confirm": state.get("canConfirm") if isinstance(state.get("canConfirm"), bool) else None,
            "automatic_tax_enabled": payload.get("automatic_tax_enabled") is True,
            "promo_check_state": (preflight or {}).get("promo_check_state"),
            "app_store_subscription_in_retry": (preflight or {}).get("app_store_subscription_in_retry"),
            "checked_at": stamp(), "error": error}


def format_note(results: list[dict]) -> str:
    lines = []
    for result in results:
        country = result["country"]
        status = result.get("status")
        if result.get("amount_display") is not None and status in {"success", "partial"}:
            methods = ",".join(result.get("payment_method_types") or [])
            lines.append(f"{country}:{result['amount_display']}{result['currency']}:[{methods}]")
        else:
            label = {"queued": "等待查询", "running": "查询中", "unknown": "结果未确认",
                     "interrupted": "查询中断", "skipped": "未查询"}.get(status, "查询失败")
            reason = str(result.get("error") or label).replace("\n", " ").replace("\r", " ").replace("[", "(").replace("]", ")")[:100]
            lines.append(f"{country}:{label}:[{reason}]")
    return "\n".join(lines)


def _http_error(response, stage):
    status = int(response.status_code)
    if 200 <= status < 300:
        return
    reason = {401: "账号认证失效", 403: "请求被拒绝或需要会话验证", 429: "服务端限流"}.get(status)
    if not reason:
        reason = f"{stage}失败，HTTP {status}"
        # Only known classification keywords, never echo response text/URLs.
        text = str(getattr(response, "text", "") or "").lower()
        if status in (400, 409, 422) and any(k in text for k in ("promo", "coupon", "ineligible")):
            reason = "该账号或国家不符合配置的促销条件"
    raise CheckoutError(reason, http_status=status)


def _request_with_plan_retry(session, stage: str, request_fn, *, allow_transport_retry: bool = True):
    """Use plan retry settings in one browser session, retaining CF cookies."""
    from core.chatgpt_plan import (
        _clear_plan_circuit, _plan_check_settings, _retry_wait_seconds, _retryable_plan_error,
    )
    _, attempts, base_delay = _plan_check_settings(None, None, None)
    for attempt in range(1, attempts + 1):
        response = None
        try:
            response = request_fn()
        except CheckoutError:
            raise
        except Exception as exc:
            # A checkout POST might have created a session before the connection
            # failed. Never replay it when the server outcome is unknown.
            if not allow_transport_retry or attempt >= attempts:
                raise
            failure = type(exc).__name__
        else:
            status = int(response.status_code)
            retryable = (_retryable_plan_error(status) if allow_transport_retry
                         else status in {403, 429})
            if status < 400 or not retryable or attempt >= attempts:
                return response
            failure = f"HTTP {status}"
        _clear_plan_circuit(session)
        wait_seconds = _retry_wait_seconds(response, base_delay, attempt)
        logger.warning("[Checkout] %s 临时失败，第 %s/%s 次，保留 session/deviceId/CF Cookie，%.1fs 后重试：%s",
                       stage, attempt, attempts, wait_seconds, failure)
        if wait_seconds > 0:
            time.sleep(wait_seconds)
    raise AssertionError("unreachable")


def query_country(config: CheckoutConfig, plan: CountryPlan, token: str, *, heartbeat=lambda: True,
                  session_factory=None, auth_context: PlanAuthContext | None = None,
                  stripe_http_factory=None) -> dict:
    session = None
    submitted = False
    stage = "选择国家代理"
    deadline = time.monotonic() + 90

    def timeout():
        if not heartbeat():
            raise CheckoutError("任务已失效，停止查询", status="interrupted")
        remain = deadline - time.monotonic()
        if remain <= 0:
            raise CheckoutError("该国查询超时", status="unknown" if submitted else "failed")
        return min(15, remain)

    try:
        proxy = select_proxy(config, plan)
        stage = "创建代理会话"
        options = deepcopy(auth_context.session_options) if auth_context else {}
        session = (session_factory or BrowserSession)(proxy=proxy, detect_exit_geo=auth_context is None, **options)
        if auth_context:
            token = auth_context.token
            session.session.cookies.clear()
            for cookie in auth_context.cookies:
                session.session.cookies.jar.set_cookie(copy(cookie))
        logger.info("[Checkout] 查询国家=%s 币种=%s 网络=%s", plan.country, plan.currency, "国家代理" if proxy else "直连")
        if auth_context:
            logger.info("[Checkout] %s 复用本次查资格会话的 accessToken、Cookie 和设备上下文", plan.country)
        else:
            stage = "读取首页"
            response = _request_with_plan_retry(session, "会话初始化", lambda: session.get(
                "https://chatgpt.com/", headers=session.get_chatgpt_navigate_headers(),
                allow_redirects=False, timeout=timeout()))
            if response.status_code >= 400:
                _http_error(response, "会话初始化")
        claims = token_claims(token)
        promo_id = config.common["promo_campaign"]["promo_campaign_id"]
        page_url = "https://chatgpt.com/?promo_campaign=" + quote(promo_id, safe="")

        # The checkout page loads this before its promo/pricing preflight.
        stage = "加载checkout Sentinel SDK"
        sdk_headers = session._get_common_headers()
        sdk_headers["referer"] = page_url
        sdk_resp = _request_with_plan_retry(session, "Sentinel SDK", lambda: session.get(
            "https://chatgpt.com/backend-api/sentinel/sdk.js", headers=sdk_headers,
            allow_redirects=False, timeout=timeout()))
        if sdk_resp.status_code >= 400:
            _http_error(sdk_resp, "Sentinel SDK加载")

        def headers(path):
            result = dict(auth_context.headers) if auth_context else session.get_chatgpt_headers(referer=page_url)
            result.update({"authorization": f"Bearer {normalize_token(token)}",
                           "x-openai-target-path": path,
                           "x-openai-target-route": (PRICING_PATH + "{country_code}" if path == PRICING_PATH + plan.country else path),
                           "referer": page_url})
            if path == CHECKOUT_PATH:
                result["content-type"] = "application/json"
            if claims.get("account_id"):
                result["chatgpt-account-id"] = claims["account_id"]
            return result

        preflight = {}
        stage = "验证促销活动"
        promo_path = "/backend-api/promo_campaign/check_coupon"
        promo_url = ("https://chatgpt.com" + promo_path + "?coupon=" + quote(promo_id, safe="")
                     + "&is_coupon_from_query_param=true")
        promo_resp = _request_with_plan_retry(session, "促销资格检查", lambda: session.get(
            promo_url, headers=headers(promo_path), allow_redirects=False, timeout=timeout()))
        if promo_resp.status_code in (401, 403, 429):
            _http_error(promo_resp, "促销资格检查")
        if 200 <= promo_resp.status_code < 300:
            try:
                promo_data = promo_resp.json()
                if isinstance(promo_data, dict):
                    state_name = promo_data.get("state")
                    if isinstance(state_name, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,40}", state_name):
                        preflight["promo_check_state"] = state_name
            except ValueError:
                pass
        stage = "检查账单重试状态"
        retry_path = "/backend-api/subscriptions/has_app_store_subscription_in_billing_retry"
        retry_resp = _request_with_plan_retry(session, "App Store 订阅状态检查", lambda: session.get(
            "https://chatgpt.com" + retry_path, headers=headers(retry_path),
            allow_redirects=False, timeout=timeout()))
        if retry_resp.status_code in (401, 403, 429):
            _http_error(retry_resp, "App Store 订阅状态检查")
        if 200 <= retry_resp.status_code < 300:
            try:
                retry_data = retry_resp.json()
                if isinstance(retry_data, dict) and isinstance(retry_data.get("value"), bool):
                    preflight["app_store_subscription_in_retry"] = retry_data["value"]
            except ValueError:
                pass

        pricing = None
        stage = "读取币种精度"
        price_path = PRICING_PATH + plan.country
        try:
            response = _request_with_plan_retry(session, "币种精度查询", lambda: session.get(
                "https://chatgpt.com" + price_path, headers=headers(price_path),
                allow_redirects=False, timeout=timeout()))
            if response.status_code in (401, 403, 429):
                _http_error(response, "币种精度查询")
            if 200 <= response.status_code < 300:
                data = response.json()
                pricing = data if isinstance(data, dict) else None
        except CheckoutError:
            raise
        except Exception:
            logger.info("[Checkout] %s 定价精度未取得，使用已知币种精度", plan.country)
        stage = "提交checkout"
        # HAR confirms checkout uses a dedicated Sentinel flow. Generate a fresh
        # token for each explicit 403/429 rejection, keeping the same session.
        from core.openai_auth import request_sentinel_token, build_sentinel_header

        def submit_checkout():
            nonlocal submitted
            checkout_headers = headers(CHECKOUT_PATH)
            sentinel = request_sentinel_token(session, "chatgpt_checkout", sentinel_origin="https://chatgpt.com", page_url=page_url)
            sentinel_token, sentinel_so = build_sentinel_header(session, sentinel, "chatgpt_checkout", page_url=page_url)
            checkout_headers["openai-sentinel-token"] = sentinel_token
            if sentinel_so:
                checkout_headers["openai-sentinel-so-token"] = sentinel_so
            request_timeout = timeout()
            submitted = True
            return session.post("https://chatgpt.com" + CHECKOUT_PATH,
                                headers=checkout_headers, json=build_body(config, plan),
                                allow_redirects=False, timeout=request_timeout)

        response = _request_with_plan_retry(session, "创建checkout", submit_checkout,
                                            allow_transport_retry=False)
        _http_error(response, "创建checkout")
        stage = "解析checkout响应"
        try:
            payload = response.json()
        except ValueError:
            raise CheckoutError("checkout 响应不是JSON，创建结果未确认", status="unknown") from None
        if isinstance(payload, dict) and payload.get("checkout_provider") == "stripe":
            stage = "初始化 Stripe checkout"
            return _init_stripe_checkout(payload, plan, pricing, proxy, timeout(), preflight,
                                         http_factory=stripe_http_factory)
        if isinstance(payload, dict) and not isinstance(payload.get("checkout_state"), dict):
            code = payload.get("code")
            wrapped = payload.get("data")
            if isinstance(wrapped, dict) and isinstance(wrapped.get("checkout_state"), dict):
                payload = wrapped
            else:
                error = payload.get("error")
                code = error.get("code") if isinstance(error, dict) else code
                code = str(code) if isinstance(code, (int, str)) else ""
                code = code[:64] if re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", code or "") else ""
                safe_code = f"，服务端错误码={code}" if code else ""
                keys = sorted(k for k in payload if re.fullmatch(r"[A-Za-z0-9_]{1,64}", str(k)))[:30]
                diagnostic = {k: payload.get(k) for k in (
                    "status", "payment_status", "checkout_kind", "automatic_tax_enabled",
                    "requires_manual_approval", "one_click_trial_eligible", "payment_method_types"
                ) if isinstance(payload.get(k), (str, int, float, bool, list, type(None)))}
                diagnostic["checkout_state_type"] = type(payload.get("checkout_state")).__name__
                diagnostic["checkout_snapshot_type"] = type(payload.get("checkout_snapshot")).__name__
                diagnostic["preflight"] = dict(preflight)
                facts = [f"checkout_state={diagnostic['checkout_state_type']}"]
                for key, label in (("status", "app"), ("payment_status", "payment"),
                                   ("one_click_trial_eligible", "one_click"),
                                   ("requires_manual_approval", "manual_approval")):
                    if key in diagnostic:
                        value = str(diagnostic[key]).lower() if isinstance(diagnostic[key], bool) else diagnostic[key]
                        facts.append(f"{label}={value}")
                if preflight.get("promo_check_state"):
                    facts.append(f"promo={preflight['promo_check_state']}")
                logger.warning("[Checkout] %s 返回无结账状态的HTTP200应用响应：keys=%s code=%s",
                               plan.country, keys, code or "-")
                raise CheckoutError(f"checkout 应用层未返回结账会话{safe_code}，结果未确认（{';'.join(facts)}）",
                                    status="unknown", error_code=code or None,
                                    diagnostic=diagnostic)
        return parse_quote(payload, plan, pricing, preflight=preflight)
    except CheckoutError as exc:
        return {"country": plan.country, "currency": plan.currency, "status": exc.status,
                "checked_at": stamp(), "error": str(exc), "http_status": exc.http_status,
                "stop_remaining": exc.stop_remaining, "stage": stage, "error_code": exc.error_code,
                "diagnostic": exc.diagnostic}
    except Exception as exc:
        return {"country": plan.country, "currency": plan.currency,
                "status": "unknown" if submitted else "failed", "checked_at": stamp(),
                "stage": stage, "error_type": type(exc).__name__,
                "error": f"{stage}异常（{type(exc).__name__}）" + ("，创建结果未确认" if submitted else "")}
    finally:
        if session is not None:
            try:
                close_browser_session(session)
            except Exception:
                logger.warning("[Checkout] 关闭国家查询会话失败：%s", plan.country)
