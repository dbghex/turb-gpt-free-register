# -*- coding: utf-8 -*-
"""后台 GCash 资格预检队列。"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.request import ProxyHandler, Request, build_opener

try:
    from curl_cffi import requests as curl_requests
except Exception:  # pragma: no cover - minimal environments
    curl_requests = None

from config import extract_link as extract_cfg
from config import proxy as proxy_cfg
from core import db
from core.chatgpt_plan import resolve_plan_check_route

logger = logging.getLogger(__name__)

_QUEUE_LIMIT = max(1, min(5000, int(getattr(proxy_cfg, "PLAN_CHECK_QUEUE_LIMIT", 500) or 500)))
_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)
_WORKERS = max(1, min(16, int(getattr(proxy_cfg, "PLAN_CHECK_WORKERS", 3) or 3)))
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="gcash-check")
_RATE_LOCK = threading.Lock()
_NEXT_REQUEST_AT = 0.0


def _setting(name: str, default=""):
    try:
        from config.env_loader import load_env
        load_env(override=True)
    except Exception:
        pass
    value = os.getenv(name)
    if value is not None and str(value).strip() != "":
        return str(value).strip()
    return getattr(extract_cfg, name, default)


def _base_url() -> str:
    value = str(_setting("EXTRACT_LINK_API_BASE", "https://ai.pupux.xyz") or "").strip().rstrip("/")
    if not value:
        raise ValueError("EXTRACT_LINK_API_BASE 为空")
    return value


def _cdk() -> str:
    value = str(_setting("EXTRACT_LINK_CDK", "") or "").strip()
    if not value:
        raise ValueError("未配置 EXTRACT_LINK_CDK，请先填写提链 CDK")
    return value


def _wait_rate_slot() -> None:
    global _NEXT_REQUEST_AT
    interval = max(0.0, min(30.0, float(getattr(proxy_cfg, "PLAN_CHECK_MIN_INTERVAL", 0.4) or 0.0)))
    with _RATE_LOCK:
        now = time.monotonic()
        scheduled = max(now, _NEXT_REQUEST_AT)
        _NEXT_REQUEST_AT = scheduled + interval
    if scheduled > now:
        time.sleep(scheduled - now)


def _decode_response(resp) -> dict:
    try:
        data = resp.json()
    except Exception:
        try:
            data = json.loads(resp.text or "{}")
        except Exception:
            data = {"detail": (getattr(resp, "text", "") or "")[:500]}
    return data if isinstance(data, dict) else {"detail": str(data)}


def _request_once(*, token: str, cdk: str, proxy: str) -> tuple[int, dict, dict]:
    url = f"{_base_url()}/api/paylinks/eligibility/gcash"
    timeout = max(5, min(300, int(float(_setting("EXTRACT_LINK_REQUEST_TIMEOUT", 30) or 30))))
    payload = {"cdk": cdk, "access_token": token}
    proxy = proxy_cfg.normalize_proxy_url(proxy) if proxy else ""
    if curl_requests is not None:
        session = curl_requests.Session()
        try:
            if proxy:
                session.proxies = {"http": proxy, "https": proxy}
            response = session.post(url, json=payload, headers={"Accept": "application/json"}, timeout=timeout)
            return int(response.status_code), _decode_response(response), dict(getattr(response, "headers", {}) or {})
        finally:
            try:
                session.close()
            except Exception:
                pass
    body = json.dumps(payload).encode("utf-8")
    req = Request(url, data=body, headers={"Accept": "application/json", "Content-Type": "application/json"}, method="POST")
    opener = build_opener(ProxyHandler({"http": proxy, "https": proxy}) if proxy else ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
            return int(response.status), json.loads(raw or "{}"), dict(response.headers.items())
    except Exception as exc:
        status = int(getattr(exc, "code", 0) or 0)
        raw = ""
        try:
            raw = exc.read().decode("utf-8", "replace")
        except Exception:
            pass
        try:
            data = json.loads(raw or "{}")
        except Exception:
            data = {"detail": raw or str(exc)}
        return status, data if isinstance(data, dict) else {"detail": str(data)}, dict(getattr(exc, "headers", {}) or {})


def check_gcash_eligibility(token: str, *, proxy: str | None = None) -> dict:
    token = str(token or "").strip()
    if not token:
        return {"query_ok": False, "error": "access_token 为空", "retryable": False, "checked_at": datetime.now().isoformat(timespec="seconds")}
    cdk = _cdk()
    route = resolve_plan_check_route(proxy)
    attempts = max(1, min(4, int(getattr(proxy_cfg, "PLAN_CHECK_MAX_ATTEMPTS", 2) or 2)))
    base_delay = max(0.0, min(30.0, float(getattr(proxy_cfg, "PLAN_CHECK_RETRY_DELAY", 1.5) or 0.0)))
    last = None
    for attempt in range(1, attempts + 1):
        _wait_rate_slot()
        status, data, headers = _request_once(token=token, cdk=cdk, proxy=str(route.get("proxy") or ""))
        checked_at = datetime.now().isoformat(timespec="seconds")
        retryable = status == 429 or status == 408 or status >= 500 or status == 0
        if 200 <= status < 300:
            required = ("valid", "payment_method_available", "checkout_amount_is_zero", "eligible", "outcome")
            if any(key not in data for key in required):
                return {
                    "query_ok": False,
                    "ok": False,
                    "http_status": status,
                    "checked_at": checked_at,
                    "error": "GCash 资格接口响应缺少必要业务字段",
                    "retryable": True,
                    **{k: route.get(k) for k in ("proxy_mode", "network_route", "proxy_used", "proxy_fallback_reason")},
                }
            result = {
                "query_ok": True,
                "ok": True,
                "http_status": status,
                "checked_at": checked_at,
                "valid": bool(data.get("valid")),
                "payment_method_available": bool(data.get("payment_method_available")),
                "checkout_amount_is_zero": bool(data.get("checkout_amount_is_zero")),
                "eligible": bool(data.get("eligible")),
                "outcome": data.get("outcome") or "",
                "retryable": bool(data.get("retryable")),
                "reason": data.get("reason") or "",
                "message": data.get("message") or "",
                **{k: route.get(k) for k in ("proxy_mode", "network_route", "proxy_used", "proxy_fallback_reason")},
            }
            result["qualification_ok"] = bool(
                result["valid"]
                and result["payment_method_available"]
                and result["checkout_amount_is_zero"]
                and result["eligible"]
                and str(result["outcome"]).lower() == "eligible"
            )
            return result
        detail = data.get("detail") or data.get("message") or data.get("reason") or f"HTTP {status or '未知'}"
        last = {
            "query_ok": False,
            "ok": False,
            "http_status": status or None,
            "checked_at": checked_at,
            "error": str(detail)[:500],
            "retryable": retryable,
            **{k: route.get(k) for k in ("proxy_mode", "network_route", "proxy_used", "proxy_fallback_reason")},
        }
        if not retryable or attempt >= attempts:
            return last
        retry_after = headers.get("Retry-After") or headers.get("retry-after")
        try:
            delay = max(0.0, min(60.0, float(retry_after))) if retry_after is not None else base_delay * attempt
        except (TypeError, ValueError):
            delay = base_delay * attempt
        if delay:
            time.sleep(delay)
    return last or {"query_ok": False, "error": "GCash 资格查询未执行", "retryable": False}


def _run(account_id: int, email: str, access_token: str) -> dict:
    try:
        if not db.mark_account_gcash_eligibility_running(account_id):
            return {"query_ok": False, "error": "账号已删除或查询状态已被重置"}
        result = check_gcash_eligibility(access_token)
        db.update_account_gcash_eligibility(account_id, result)
        if result.get("query_ok") and result.get("eligible"):
            logger.info("[GCash资格] 通过: %s", email)
        elif result.get("query_ok"):
            logger.info("[GCash资格] 未通过: %s reason=%s", email, result.get("reason") or result.get("outcome"))
        else:
            logger.warning("[GCash资格] 查询失败: %s error=%s", email, result.get("error"))
        return result
    except Exception as exc:
        result = {"query_ok": False, "ok": False, "checked_at": datetime.now().isoformat(timespec="seconds"), "error": f"{type(exc).__name__}: {str(exc)[:300]}", "retryable": False}
        try:
            db.update_account_gcash_eligibility(account_id, result)
        except Exception:
            logger.exception("[GCash资格] 写入失败状态异常: account_id=%s", account_id)
        logger.exception("[GCash资格] 异常: %s", email)
        return result
    finally:
        _SLOTS.release()


def enqueue_account_gcash_eligibility(*, account_id: int, email: str, access_token: str, trigger: str = "manual") -> dict:
    if not str(access_token or "").strip():
        return {"accepted": False, "busy": False, "error": "该账号没有 access_token"}
    try:
        _cdk()
    except Exception as exc:
        return {"accepted": False, "busy": False, "error": str(exc)}
    if not _SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "queue_full": True, "error": "GCash 资格查询队列已满，请稍后重试"}
    if not db.claim_account_gcash_eligibility(int(account_id), trigger=trigger):
        _SLOTS.release()
        return {"accepted": False, "busy": True, "error": "该账号正在查询 GCash 资格"}
    try:
        _EXECUTOR.submit(_run, int(account_id), str(email or ""), str(access_token).strip())
    except Exception as exc:
        _SLOTS.release()
        result = {"query_ok": False, "error": f"GCash 资格查询入队失败: {type(exc).__name__}: {str(exc)[:180]}"}
        db.update_account_gcash_eligibility(int(account_id), result)
        return {"accepted": False, "busy": False, "error": result["error"]}
    return {"accepted": True, "busy": False, "account_id": int(account_id), "email": str(email or ""), "status": "queued", "trigger": trigger}


def queue_settings() -> dict:
    return {"workers": _WORKERS, "queue_limit": _QUEUE_LIMIT}
