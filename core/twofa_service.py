# -*- coding: utf-8 -*-
"""账号 2FA/TOTP 后台设置队列。"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from config import email as _email_cfg
from config import twofa as _twofa_cfg
from core import db
from core.account_export import setup_2fa
from core.session import BrowserSession

logger = logging.getLogger(__name__)


def _int_setting(name: str, default: int, lower: int, upper: int) -> int:
    try:
        value = int(getattr(_twofa_cfg, name, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


_WORKERS = _int_setting("TWOFA_WORKERS", 4, 1, 16)
_QUEUE_LIMIT = _int_setting("TWOFA_QUEUE_LIMIT", 200, _WORKERS, 5000)
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="twofa")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)
_RUNNING: set[int] = set()
_LOCK = threading.Lock()
_LOG_DIR = Path(__file__).resolve().parent.parent / "注册日志"


def log_path(email: str) -> Path:
    safe = str(email or "").replace("/", "_").replace("\\", "_").replace(":", "_")
    return _LOG_DIR / f"twofa-{safe}.log"


def password_log_path(email: str) -> Path:
    safe = str(email or "").replace("/", "_").replace("\\", "_").replace(":", "_")
    return _LOG_DIR / f"password-{safe}.log"


def _normalize_proxy(proxy: str | None) -> str | None:
    """
    2FA 入口只接受真实代理地址。

    注册流程里有些 `proxy_used` 字段保存的是环境标签，例如 `skyvern:jp`、
    `browser_use:jp`，这类不是 curl_cffi 可用代理，会导致 Unsupported proxy syntax。
    """
    text = str(proxy or "").strip()
    if not text:
        return None
    low = text.lower()
    if low.startswith(("http://", "https://", "socks5://", "socks5h://", "socks4://", "socks4a://")):
        return text
    return None


def is_running(acc_id: int) -> bool:
    with _LOCK:
        return int(acc_id) in _RUNNING


def _append_log(email: str, line: str, *, clear: bool = False) -> None:
    p = log_path(email)
    p.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%H:%M:%S")
    mode = "w" if clear else "a"
    with p.open(mode, encoding="utf-8") as f:
        f.write(f"{stamp} [INFO] {line}\n")


class _TaskLogFilter(logging.Filter):
    """Only stage summaries reach handlers during security work.

    Legacy auth helpers log tokens, OTP and response bodies. Suppress those
    records for this task thread, without affecting other workers.
    """
    def __init__(self, thread_id):
        super().__init__()
        self.thread_id = thread_id

    def filter(self, record):
        if record.thread != self.thread_id or record.name in {__name__, "core.password_setup"}:
            return True
        # Preserve existing OTP/progress logs explicitly requested by the user,
        # without passing through token, cookie, proxy or full-response logs.
        message = str(record.msg)
        if record.name == "core.account_export":
            return any(marker in message for marker in (
                "提交重认证 OTP:", "激活 enrollment, code=", "阶段1：", "阶段2：",
                "自动等待邮箱重认证 OTP", "已收到邮箱重认证 OTP", "TOTP 激活完成",
            ))
        if record.name == "core.generic_api_mail_client":
            return any(marker in message for marker in ("暂未从取码接口", "首次锁定 OTP=", "已锁定候选 OTP=", "settle 完成"))
        return False


def _close_session(session):
    if session is not None:
        try:
            session.session.close()
        except Exception:
            pass


def _run_twofa(*, account_id: int, email: str, access_token: str, proxy: str | None,
               trigger: str, password_enabled: bool = False, totp_enabled: bool = True,
               configured_password: str = "") -> dict:
    from core.password_setup import choose_password, setup_password, safe_error, PasswordResultUnknown

    session = None
    fh = None
    password_fh = None
    password_active = password_enabled
    filters = []
    root_logger = logging.getLogger()
    thread_id = threading.get_ident()
    log_filter = _TaskLogFilter(thread_id)
    outcomes = {}
    try:
        if not db.mark_account_security_setup(account_id, "running"):
            return {"ok": False, "status": "failed", "error": "账号已删除"}
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
        if totp_enabled:
            fh = logging.FileHandler(str(log_path(email)), encoding="utf-8")
            fh.setFormatter(formatter)
            fh.addFilter(lambda record: record.thread == thread_id and not password_active)
            root_logger.addHandler(fh)
        if password_enabled:
            password_fh = logging.FileHandler(str(password_log_path(email)), encoding="utf-8")
            password_fh.setFormatter(formatter)
            password_fh.addFilter(lambda record: record.thread == thread_id and password_active)
            root_logger.addHandler(password_fh)
        handlers = set(root_logger.handlers)
        for obj in logging.Logger.manager.loggerDict.values():
            if isinstance(obj, logging.Logger):
                handlers.update(obj.handlers)
        for handler in handlers:
            handler.addFilter(log_filter)
            filters.append(handler)
        logger.info("[账号安全] 开始后台任务 account_id=%s trigger=%s", account_id, trigger)

        def new_session():
            return BrowserSession(proxy=_normalize_proxy(proxy), fingerprint_seed=f"account:{email.strip().lower()}")

        if password_enabled:
            db.update_account_password_setup(account_id, {"status": "running"})
            try:
                account = db.get_account(account_id)
                if not account:
                    raise RuntimeError("account removed")
                existing = db._extract_registration_password(account)
                if existing:
                    result = {"ok": True, "status": "skipped", "message": "已有已保存登录密码，跳过重复设置"}
                elif not _email_cfg.USE_EMAIL_SERVICE:
                    result = {"ok": False, "status": "failed", "error": "设置密码需要开启自动邮箱收码"}
                else:
                    password = choose_password(configured_password)
                    session = new_session()

                    def confirmed(value):
                        if not db.update_account_password_setup(account_id, {
                            "ok": True, "status": "success", "password": value,
                            "message": "服务端已确认密码，正在刷新会话",
                        }):
                            raise RuntimeError("account removed before persistence")

                    logger.info("[密码] 开始协议设置登录密码")
                    result = setup_password(session, email, password,
                                            totp_secret=str(account.get("totp_secret") or ""), on_confirmed=confirmed)
                    if result.get("access_token"):
                        access_token = result["access_token"]
                db.update_account_password_setup(account_id, result)
            except Exception as exc:
                result = {"ok": False, "status": "unknown" if isinstance(exc, PasswordResultUnknown) else "failed",
                          "error": safe_error(exc)}
                db.update_account_password_setup(account_id, result)
            outcomes["password"] = {k: result.get(k) for k in ("ok", "status", "message", "error") if k in result}
            logger.info("[密码] 完成：status=%s %s", result["status"], result.get("error") or result.get("message", ""))
            if not result.get("ok") or result.get("session_refresh_error"):
                _close_session(session)
                session = None
            password_active = False

        if totp_enabled:
            try:
                account = db.get_account(account_id)
                if not account:
                    raise RuntimeError("account removed")
                if account.get("totp_secret"):
                    result = {"ok": True, "status": "success", "message": "账号已有 TOTP 密钥，跳过重复设置"}
                else:
                    if not db.mark_account_totp_setup_running(account_id):
                        raise RuntimeError("2FA task state changed")
                    if not _email_cfg.USE_EMAIL_SERVICE:
                        from core.password_setup import PasswordSetupError
                        raise PasswordSetupError("开启 2FA 需要开启自动邮箱收码")
                    session = session or new_session()
                    logger.info("[2FA] 开始协议设置（密码阶段已结束）")
                    secret = setup_2fa(session, email, access_token=access_token,
                                       login_password=db._extract_registration_password(account))
                    result = {"ok": True, "status": "success", "totp_secret": secret, "message": "2FA 设置完成"}
                db.update_account_totp_secret(account_id, result)
            except Exception as exc:
                result = {"ok": False, "status": "failed", "error": safe_error(exc)}
                db.update_account_totp_secret(account_id, result)
            outcomes["totp"] = {k: result.get(k) for k in ("ok", "status", "message", "error") if k in result}
            logger.info("[2FA] 完成：status=%s %s", result["status"], result.get("error") or result.get("message", ""))
        ok = all(r.get("ok") for r in outcomes.values())
        return {"ok": ok, "status": "success" if ok else "failed", **outcomes}
    except Exception as exc:
        error = safe_error(exc)
        for enabled, name, update in ((password_enabled, "password", db.update_account_password_setup),
                                      (totp_enabled, "totp", db.update_account_totp_secret)):
            if enabled and name not in outcomes:
                update(account_id, {"ok": False, "status": "failed", "error": error})
        logger.error("[账号安全] 任务异常：%s", error)
        return {"ok": False, "status": "failed", "error": error}
    finally:
        _close_session(session)
        for handler in filters:
            handler.removeFilter(log_filter)
        if fh is not None:
            root_logger.removeHandler(fh)
            fh.close()
        if password_fh is not None:
            root_logger.removeHandler(password_fh)
            password_fh.close()
        try:
            db.mark_account_security_setup(account_id, "finished")
        finally:
            with _LOCK:
                _RUNNING.discard(int(account_id))
            _QUEUE_SLOTS.release()


def queue_settings() -> dict:
    with _LOCK:
        running = len(_RUNNING)
    return {
        "workers": _WORKERS,
        "queue_limit": _QUEUE_LIMIT,
        "running": running,
    }


def enqueue_account_totp_setup(*, account_id: int, email: str, access_token: str,
                               trigger: str = "manual", proxy: str | None = None) -> dict:
    """Compatibility entry point: manual actions never enable password setup."""
    if not bool(getattr(_email_cfg, "USE_EMAIL_SERVICE", False)):
        return {"accepted": False, "busy": False, "error": "启用 2FA 需要先开启 USE_EMAIL_SERVICE 自动收取邮箱验证码"}
    return enqueue_account_security_setup(account_id=account_id, email=email, access_token=access_token,
                                           trigger=trigger, proxy=proxy, password_enabled=False, totp_enabled=True)


def enqueue_account_security_setup(*, account_id: int, email: str, access_token: str,
                                   password_enabled: bool, totp_enabled: bool,
                                   trigger: str = "registration_auto", proxy: str | None = None) -> dict:
    from config import register as register_cfg
    from core.password_setup import safe_error

    account_id = int(account_id)
    email = str(email or "").strip()
    access_token = str(access_token or "").strip()
    if not email or not access_token:
        return {"accepted": False, "busy": False, "error": "缺少 email 或 access_token"}
    if not password_enabled and not totp_enabled:
        return {"accepted": False, "busy": False, "skipped": True}
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "queue_full": True, "error": "账号安全队列已满，请稍后重试"}
    claimed = False
    try:
        with _LOCK:
            if account_id in _RUNNING:
                _QUEUE_SLOTS.release()
                return {"accepted": False, "busy": True, "error": "该账号已有安全设置任务"}
            claimed = db.claim_account_security_setup(account_id, password_enabled=password_enabled,
                                                       totp_enabled=totp_enabled, trigger=trigger)
            if not claimed:
                _QUEUE_SLOTS.release()
                return {"accepted": False, "busy": True, "error": "账号不存在或已有安全设置任务"}
            _RUNNING.add(account_id)
        if totp_enabled:
            _append_log(email, f"[2FA] 已入队 account_id={account_id}" + ("；等待密码阶段结束" if password_enabled else ""), clear=True)
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        if password_enabled:
            password_log_path(email).write_text(
                f"{datetime.now():%H:%M:%S} [INFO] [密码] 已入队，等待后台协议设置\n", encoding="utf-8")
        future = _EXECUTOR.submit(
            _run_twofa, account_id=account_id, email=email, access_token=access_token, proxy=proxy,
            trigger=trigger, password_enabled=password_enabled, totp_enabled=totp_enabled,
            configured_password=str(getattr(register_cfg, "REGISTER_PASSWORD", "") or ""),
        )
        return {"accepted": True, "busy": False, "future": future, "log_path": str(password_log_path(email) if password_enabled and not totp_enabled else log_path(email))}
    except Exception as exc:
        _QUEUE_SLOTS.release()
        with _LOCK:
            _RUNNING.discard(account_id)
        error = safe_error(exc)
        if claimed:
            if password_enabled:
                db.update_account_password_setup(account_id, {"status": "failed", "error": error})
            if totp_enabled:
                db.update_account_totp_secret(account_id, {"status": "failed", "error": error})
            db.mark_account_security_setup(account_id, "failed")
        return {"accepted": False, "busy": False, "error": error}
