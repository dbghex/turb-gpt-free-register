# -*- coding: utf-8 -*-
"""套餐/Plus 资格查询后台队列。"""
from __future__ import annotations

import logging
import random
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from config import proxy as proxy_cfg
from core import db
from core.chatgpt_plan import check_account_plan, prepare_checkout_auth_context

logger = logging.getLogger(__name__)


def _int_setting(name: str, default: int, lower: int, upper: int) -> int:
    try:
        value = int(getattr(proxy_cfg, name, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


def _float_setting(name: str, default: float, lower: float, upper: float) -> float:
    try:
        value = float(getattr(proxy_cfg, name, default) or 0.0)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


_WORKERS = _int_setting("PLAN_CHECK_WORKERS", 3, 1, 16)
_QUEUE_LIMIT = _int_setting("PLAN_CHECK_QUEUE_LIMIT", 500, _WORKERS, 5000)
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="plan-check")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)
_RATE_LOCK = threading.Lock()
_NEXT_REQUEST_AT = 0.0
_ACTIVE_LOCK = threading.Lock()
_ACTIVE_ACCOUNTS: set[int] = set()


def _run_checkout_stage(account_id, run_id, config, auth_context=None):
    from core.checkout_quotes import query_country
    account = db.get_account(account_id)
    if not account:
        return
    summary = config.public_summary()
    results = [{"country": p.country, "currency": p.currency, "status": "queued"} for p in config.plans]

    def save(status="running", reason=""):
        return db.update_checkout_run(account_id, run_id, status=status, reason=reason,
                                      results=results, config_summary=summary)

    if not save():
        return
    for index, plan in enumerate(config.plans):
        results[index]["status"] = "running"
        if not save():
            return
        _wait_for_rate_slot()
        results[index] = query_country(config, plan, account.get("access_token") or "",
            heartbeat=lambda: db.update_checkout_run(account_id, run_id), auth_context=auth_context)
        logger.info("[Checkout] account_id=%s country=%s status=%s", account_id, plan.country, results[index]["status"])
        if not save():
            return
        if results[index].get("stop_remaining"):
            for remaining in results[index + 1:]:
                remaining.update(status="skipped", error="前序请求认证失效或服务端限流，已停止")
            break
    successes = sum(r.get("status") == "success" for r in results)
    useful = any(r.get("status") in {"success", "partial"} for r in results)
    save("success" if successes == len(results) else "partial" if useful else "failed")


def _run_checkout_only(*, account_id: int, access_token: str, run_id: str, config) -> dict:
    stopped = threading.Event()
    heartbeat_thread = None

    def keep_alive():
        while not stopped.wait(15):
            if not db.update_checkout_run(account_id, run_id):
                return

    try:
        if not db.mark_account_checkout_running(account_id, run_id):
            return {"ok": False, "error": "查资格任务状态已失效"}
        heartbeat_thread = threading.Thread(target=keep_alive, daemon=True)
        heartbeat_thread.start()
        auth_context = prepare_checkout_auth_context(access_token)
        _run_checkout_stage(account_id, run_id, config, auth_context)
        row = db.get_account(account_id) or {}
        return {"ok": row.get("checkout_status") == "success", "status": row.get("checkout_status")}
    except Exception as exc:
        logger.exception("[Checkout] 账号 #%s 查资格任务异常", account_id)
        db.update_checkout_run(account_id, run_id, status="failed",
                               reason=f"查资格任务异常（{type(exc).__name__}）")
        return {"ok": False, "error": type(exc).__name__}
    finally:
        stopped.set()
        if heartbeat_thread:
            heartbeat_thread.join(timeout=1)
        try:
            db.update_checkout_run(account_id, run_id, finish=True)
        finally:
            with _ACTIVE_LOCK:
                _ACTIVE_ACCOUNTS.discard(account_id)
            _QUEUE_SLOTS.release()


def enqueue_account_checkout_check(*, account_id: int, access_token: str) -> dict:
    from core.checkout_quotes import load_config, CheckoutError
    account_id = int(account_id)
    if not str(access_token or "").strip():
        return {"accepted": False, "error": "账号缺少 access_token"}
    try:
        config = load_config()
    except CheckoutError as exc:
        return {"accepted": False, "error": str(exc)}
    if config is None:
        return {"accepted": False, "error": "缺少 checkout.yaml"}
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "error": "查资格队列已满"}
    run_id = uuid.uuid4().hex
    with _ACTIVE_LOCK:
        if account_id in _ACTIVE_ACCOUNTS or not db.claim_account_checkout(account_id, run_id):
            _QUEUE_SLOTS.release()
            return {"accepted": False, "busy": True, "error": "账号正在查套餐或查资格"}
        _ACTIVE_ACCOUNTS.add(account_id)
    try:
        _EXECUTOR.submit(_run_checkout_only, account_id=account_id,
                         access_token=access_token, run_id=run_id, config=config)
    except Exception as exc:
        db.update_checkout_run(account_id, run_id, status="failed", reason="查资格入队失败", finish=True)
        with _ACTIVE_LOCK:
            _ACTIVE_ACCOUNTS.discard(account_id)
        _QUEUE_SLOTS.release()
        return {"accepted": False, "error": f"入队失败（{type(exc).__name__}）"}
    return {"accepted": True, "status": "queued", "account_id": account_id}


def _wait_for_rate_slot() -> None:
    """为所有查询线程分配错开的请求启动时间。"""
    global _NEXT_REQUEST_AT
    min_interval = _float_setting("PLAN_CHECK_MIN_INTERVAL", 0.4, 0.0, 30.0)
    jitter = _float_setting("PLAN_CHECK_JITTER", 0.3, 0.0, 30.0)
    with _RATE_LOCK:
        now = time.monotonic()
        scheduled = max(now, _NEXT_REQUEST_AT) + (random.uniform(0.0, jitter) if jitter else 0.0)
        _NEXT_REQUEST_AT = scheduled + min_interval
    wait_seconds = scheduled - now
    if wait_seconds > 0:
        time.sleep(wait_seconds)


def _registration_recheck_delay() -> float:
    return _float_setting("PLAN_CHECK_REGISTRATION_RECHECK_DELAY", 2.0, 0.0, 30.0)


def _is_free_plan_result(result: dict) -> bool:
    plan_type = str(result.get("current_plan_type") or "").strip().lower()
    subscription_plan = str(result.get("subscription_plan") or "").strip().lower()
    return plan_type == "free" or (not plan_type and subscription_plan == "chatgptfreeplan")


def _run_plan_check(
    *,
    account_id: int,
    email: str,
    access_token: str,
    trigger: str,
    proxy: str | None,
    timezone_offset_min: str,
    run_id: str | None = None,
) -> dict:
    stopped = threading.Event()
    heartbeat_thread = None
    result = None

    def keep_alive():
        while not stopped.wait(15):
            try:
                if not db.update_plan_run_lifecycle(account_id, run_id):
                    return
            except Exception:
                logger.warning("[Plan] 更新任务心跳失败：account_id=%s", account_id)

    try:
        if not db.mark_account_plan_check_running(account_id, **({"run_id": run_id} if run_id else {})):
            return {"ok": False, "error": "账号已删除或套餐查询状态已被重置"}
        if run_id:
            heartbeat_thread = threading.Thread(target=keep_alive, daemon=True)
            heartbeat_thread.start()

        _wait_for_rate_slot()
        result = check_account_plan(
            access_token,
            proxy=proxy,
            timezone_offset_min=timezone_offset_min,
        )

        recheck_delay = _registration_recheck_delay()
        should_recheck = (
            trigger == "registration_auto"
            and recheck_delay > 0
            and bool(result.get("ok"))
            and _is_free_plan_result(result)
            and not bool(result.get("plus_trial_eligible"))
        )
        if should_recheck:
            logger.info("[Plan] 新账号暂未发现 Plus 试用资格，%.1fs 后复查一次: %s", recheck_delay, email)
            time.sleep(recheck_delay)
            _wait_for_rate_slot()
            recheck_result = check_account_plan(
                access_token,
                proxy=proxy,
                timezone_offset_min=timezone_offset_min,
                max_attempts=1,
            )
            if recheck_result.get("ok"):
                result = recheck_result
            else:
                logger.warning(
                    "[Plan] 新账号资格复查失败，保留首次成功结果: %s, %s",
                    email,
                    recheck_result.get("error") or "未知错误",
                )

        if not db.update_account_plan_check(acc_id=account_id, result=result, **({"run_id": run_id} if run_id else {})):
            return result
        if result.get("ok"):
            logger.info(
                "[Plan] 后台查询成功: %s, plan=%s, plus_trial=%s, trigger=%s",
                email,
                result.get("current_plan_type") or "unknown",
                bool(result.get("plus_trial_eligible")),
                trigger,
            )
        else:
            logger.warning(
                "[Plan] 后台查询失败: %s, trigger=%s, error=%s",
                email,
                trigger,
                result.get("error") or "未知错误",
            )
        return result
    except Exception as exc:
        result = {
            "ok": False,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"{type(exc).__name__}: {str(exc)[:180]}",
        }
        try:
            db.update_account_plan_check(acc_id=account_id, result=result, **({"run_id": run_id} if run_id else {}))
        except Exception:
            logger.exception("[Plan] 写入后台查询异常状态失败: account_id=%s", account_id)
        logger.exception("[Plan] 后台查询异常: %s", email)
        return result
    finally:
        stopped.set()
        if heartbeat_thread:
            heartbeat_thread.join(timeout=1)
        try:
            if run_id:
                db.update_plan_run_lifecycle(account_id, run_id, finish=True)
        finally:
            with _ACTIVE_LOCK:
                _ACTIVE_ACCOUNTS.discard(account_id)
            _QUEUE_SLOTS.release()


def enqueue_account_plan_check(
    *,
    account_id: int,
    email: str,
    access_token: str,
    trigger: str,
    proxy: str | None = None,
    timezone_offset_min: str = "-",
) -> dict:
    """把查询放入统一线程池；重复查询或队列满时不提交。"""
    account_id = int(account_id)
    email = str(email or "").strip()
    access_token = str(access_token or "").strip()
    if not access_token:
        return {"accepted": False, "busy": False, "error": "账号缺少 access_token"}
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "queue_full": True, "error": "套餐查询队列已满，请稍后重试"}

    run_id = uuid.uuid4().hex
    with _ACTIVE_LOCK:
        if account_id in _ACTIVE_ACCOUNTS or not db.claim_account_plan_check(acc_id=account_id, trigger=trigger, run_id=run_id):
            _QUEUE_SLOTS.release()
            return {"accepted": False, "busy": True, "error": "该账号正在查套餐或查资格"}
        _ACTIVE_ACCOUNTS.add(account_id)

    try:
        _EXECUTOR.submit(
            _run_plan_check,
            account_id=account_id,
            email=email,
            access_token=access_token,
            trigger=str(trigger or "manual"),
            proxy=proxy,
            timezone_offset_min=str(timezone_offset_min or "-"),
            run_id=run_id,
        )
    except Exception as exc:
        _QUEUE_SLOTS.release()
        with _ACTIVE_LOCK:
            _ACTIVE_ACCOUNTS.discard(account_id)
        result = {
            "ok": False,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"套餐查询入队失败: {type(exc).__name__}: {str(exc)[:160]}",
        }
        db.update_account_plan_check(acc_id=account_id, result=result, run_id=run_id)
        db.update_plan_run_lifecycle(account_id, run_id, finish=True)
        return {"accepted": False, "busy": False, "error": result["error"]}

    return {
        "accepted": True,
        "busy": False,
        "account_id": account_id,
        "email": email,
        "status": "queued",
        "trigger": str(trigger or "manual"),
    }


def queue_settings() -> dict:
    return {
        "workers": _WORKERS,
        "queue_limit": _QUEUE_LIMIT,
        "min_interval": _float_setting("PLAN_CHECK_MIN_INTERVAL", 0.4, 0.0, 30.0),
        "jitter": _float_setting("PLAN_CHECK_JITTER", 0.3, 0.0, 30.0),
    }
