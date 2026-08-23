# -*- coding: utf-8 -*-
"""通过 CloakBrowser + Playwright 适配层执行 ChatGPT 注册。"""
from __future__ import annotations

import logging
import asyncio
import threading
import time
from pathlib import Path

from config import cloakbrowser as _cfg
from config import twofa as _twofa_cfg
from core.account_export import save_account_data, post_register_dwell
from core.cloakbrowser_driver import build_cloak_driver
from core.email_provider import wait_for_otp, resolve_email_source
from core.humanize import delay as human_delay

# 复用 Roxy 注册流程里已维护好的页面操作函数。
from core.roxy_registration import (  # noqa: F401
    _maybe_accept, _submit_email_and_wait_next, _fill_password_page_if_present,
    _clear_otp_inputs, _type_otp, _click_continue, _wait_after_email_otp_submit,
    _click_resend_email_otp, _is_email_verification_page, _complete_profile_page,
    _fetch_chatgpt_session, _check_manual_stop,
)

logger = logging.getLogger(__name__)


def _run_cloak_registration_impl(email: str, name: str, birthday: str, proxy: str = None, otp_code: str = None, batch_dir: Path | None = None) -> dict:
    """CloakBrowser 自动化注册入口。"""
    driver = None
    opened = None
    create_acknowledged = False
    openai_password: str | None = None
    try:
        driver, opened = build_cloak_driver(proxy=proxy)
        logger.info("[Cloak注册] 开始：%s，profile=%s", email, opened.profile_id)

        otp_after_ts = time.time()
        logger.info("[Cloak注册] 打开登录页：https://chatgpt.com/auth/login")
        driver.get("https://chatgpt.com/auth/login")
        human_delay("navigate")
        _maybe_accept(driver)
        _check_manual_stop()

        next_state = _submit_email_and_wait_next(driver, email, attempts=3)
        _check_manual_stop()

        openai_password = None if next_state == "otp" else _fill_password_page_if_present(driver, email, timeout=25)
        _check_manual_stop()

        current_otp = otp_code
        max_otp_attempts = 3
        for otp_attempt in range(1, max_otp_attempts + 1):
            if current_otp is None:
                logger.info("[Cloak注册][OTP] 等待验证码：%s（第 %s/%s 次）", email, otp_attempt, max_otp_attempts)
                try:
                    current_otp = wait_for_otp(email, after_ts=otp_after_ts)
                except Exception as exc:
                    if otp_attempt >= max_otp_attempts:
                        raise
                    logger.warning(
                        "[Cloak注册][OTP] 一直未收到验证码，点击“重新发送电子邮件”后继续等待（下一轮 %s/%s）：%s: %s",
                        otp_attempt + 1,
                        max_otp_attempts,
                        type(exc).__name__,
                        str(exc)[:180],
                    )
                    otp_after_ts = time.time()
                    resend = _click_resend_email_otp(driver, timeout=25)
                    if resend.get("reason") == "already_left_otp":
                        logger.info("[Cloak注册][OTP] 等待取码期间已离开验证码页，按已登录账号继续")
                        break
                    human_delay("api")
                    current_otp = None
                    continue
            logger.info("[Cloak注册][OTP] 收到验证码：%s", current_otp)
            _clear_otp_inputs(driver)
            _type_otp(driver, current_otp)
            human_delay("otp_input")
            try:
                _click_continue(driver)
            except Exception as exc:
                logger.info("[Cloak注册][OTP] 未找到显式提交按钮，继续等待页面状态：%s", str(exc)[:120])

            outcome = _wait_after_email_otp_submit(driver, timeout=10)
            if outcome == "accepted":
                break
            # 二次确认，覆盖“等待窗口内刚好完成导航”的竞态。
            try:
                if not _is_email_verification_page(driver):
                    logger.info("[Cloak注册][OTP] 页面已离开验证码页，按已注册/已登录账号继续")
                    break
            except Exception:
                pass
            if otp_attempt >= max_otp_attempts:
                raise RuntimeError("邮箱验证码连续错误/过期，已达到最大重试次数")
            otp_after_ts = time.time()
            resend = _click_resend_email_otp(driver, timeout=25)
            if resend.get("reason") == "already_left_otp":
                logger.info("[Cloak注册][OTP] 重发前已进入登录态，按已注册账号继续")
                break
            human_delay("api")
            current_otp = None

        profile_submitted = _complete_profile_page(driver, name, birthday, timeout=60)
        registration_status = "created" if profile_submitted else "already_registered"
        if not profile_submitted:
            logger.info("[Cloak注册] 已检测到现有登录态，按已注册账号继续写入本地账号库：%s", email)
        if profile_submitted:
            create_acknowledged = True
            human_delay("post_auth")

        session_info = _fetch_chatgpt_session(driver, timeout=120)
        access_token = session_info["accessToken"]
        logger.info("[Cloak注册] 已拿到 accessToken：%s", email)

        if _twofa_cfg.ENABLE_2FA:
            logger.warning("[Cloak注册] 当前 CloakBrowser 自动化路径暂不执行 2FA 设置，已跳过")
        totp_secret = None

        codex_result = {
            "status": "skipped",
            "ok": True,
            "message": "ENABLE_CODEX_AUTO=False，跳过 Codex",
        }
        try:
            from config import codex as _codex_cfg
            if bool(getattr(_codex_cfg, "ENABLE_CODEX_AUTO", False)):
                from core.roxy_codex_oauth import run_roxy_codex_oauth
                logger.info("[Cloak注册][Codex] ENABLE_CODEX_AUTO=True，复用当前 CloakBrowser 窗口执行 Codex 授权")
                _check_manual_stop()
                codex_result = run_roxy_codex_oauth(
                    email,
                    reuse_existing_profile=True,
                    existing_driver=driver,
                    existing_opened=opened,
                    force=True,
                    clear_existing_state=True,
                )
            else:
                logger.info("[Cloak注册][Codex] ENABLE_CODEX_AUTO=False，注册后跳过 Codex OAuth")
        except Exception as exc:
            codex_result = {"status": "failed", "ok": False, "message": f"{type(exc).__name__}: {str(exc)[:180]}"}

        account_id = save_account_data(
            email=email,
            access_token=access_token,
            totp_secret=totp_secret,
            email_source=resolve_email_source(email),
            proxy_used=((opened.raw or {}).get("proxy") if opened else None) or proxy or None,
            batch_dir=batch_dir,
            extra={
                "user": session_info.get("user"),
                "account": session_info.get("account"),
                "expires": session_info.get("expires"),
                "cloakbrowser": {"profile_id": opened.profile_id, "open_result": opened.raw},
                "registration_password": openai_password,
                "registration_status": registration_status,
                "codex": codex_result,
            },
        )
        post_register_dwell(email, label="Cloak注册")
        codex_ok = codex_result.get("ok") or codex_result.get("status") == "skipped"
        return {"success": bool(codex_ok), "email": email, "account_id": account_id, "access_token": access_token, "totp_secret": totp_secret, "registration_status": registration_status, "message": "已注册账号已登录并写入本地账号库" if registration_status == "already_registered" else "新账号注册完成", "codex": codex_result, "error": None if codex_ok else f"Codex 未完成: {codex_result.get('message')}"}
    except Exception as exc:
        logger.error("[Cloak注册] 失败：%s: %s", type(exc).__name__, exc)
        logger.debug("[Cloak注册] 失败详情", exc_info=True)
        try:
            from core.email_provider import release_email
            release_email(email, status="failed" if create_acknowledged else "available", note=f"Cloak注册失败: {str(exc)[:180]}")
        except Exception:
            pass
        return {"success": False, "email": email, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
    finally:
        if driver and not bool(_cfg.CLOAK_KEEP_BROWSER_OPEN):
            try:
                driver.quit()
            except Exception:
                pass


def _has_running_asyncio_loop() -> bool:
    try:
        return asyncio.get_running_loop().is_running()
    except RuntimeError:
        return False


def _run_in_isolated_thread(fn, *args, **kwargs):
    """Playwright Sync API 必须运行在没有 asyncio loop 的线程中。"""
    result_box = {}
    error_box = {}
    thread_name = threading.current_thread().name

    def target():
        try:
            result_box["value"] = fn(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001
            error_box["error"] = exc

    thread = threading.Thread(target=target, name=thread_name, daemon=False)
    thread.start()
    thread.join()
    if "error" in error_box:
        raise error_box["error"]
    return result_box.get("value")


def run_cloak_registration(email: str, name: str, birthday: str, proxy: str = None, otp_code: str = None, batch_dir: Path | None = None) -> dict:
    """Cloak 注册入口；已有 asyncio loop 时隔离到专用线程。"""
    if _has_running_asyncio_loop():
        logger.info("[Cloak注册] 检测到 asyncio loop，切换到隔离线程执行 Sync Playwright")
        return _run_in_isolated_thread(
            _run_cloak_registration_impl,
            email=email,
            name=name,
            birthday=birthday,
            proxy=proxy,
            otp_code=otp_code,
            batch_dir=batch_dir,
        )
    return _run_cloak_registration_impl(
        email=email,
        name=name,
        birthday=birthday,
        proxy=proxy,
        otp_code=otp_code,
        batch_dir=batch_dir,
    )
