# -*- coding: utf-8 -*-
"""Plus 试用提链后台队列。"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import quote, urlencode
from urllib.error import HTTPError
from urllib.request import Request, urlopen

try:
    from curl_cffi import requests as curl_requests
except Exception:  # WebUI 环境未装 curl_cffi 时使用标准库兜底
    curl_requests = None

from config import extract_link as cfg
from core import db

logger = logging.getLogger(__name__)


def _runtime_setting(name: str, default=None):
    """
    提链配置多数保存在 .env。服务模块会在 WebUI 启动时较早 import，
    因此每次实际读取时都重新加载 .env，避免“页面已保存但当前进程仍读到空值”。
    """
    try:
        from config.env_loader import load_env
        load_env(override=True)
    except Exception:
        pass
    raw = os.getenv(name)
    if raw is not None and str(raw).strip() != "":
        return str(raw).strip()
    return getattr(cfg, name, default)


def _int_setting(name: str, default: int, lower: int, upper: int) -> int:
    try:
        value = int(_runtime_setting(name, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


SUPPORTED_LINK_TYPES = {"pix", "upi", "kakao_pay", "kakao", "ideal", "gcash"}

_LINK_TYPE_MAP = {"pix": "upi", "upi": "upi", "kakao_pay": "kakao", "kakao": "kakao", "ideal": "ideal", "gcash": "gcash"}


def _link_type(value: str | None = None) -> str:
    t = str(value or _runtime_setting("EXTRACT_LINK_TYPE", "pix") or "pix").strip().lower()
    if t not in SUPPORTED_LINK_TYPES:
        raise ValueError("提链类型无效，仅支持 pix / upi / kakao_pay / kakao / ideal / gcash")
    return _LINK_TYPE_MAP[t]


def resolve_link_type(value: str | None = None) -> str:
    """返回请求实际使用的规范化提链类型。

    WebUI 的资格门禁与真正入队必须使用同一次解析结果，避免页面按 GCash
    放行、后台却因为配置变化而提交成其他支付方式。
    """
    return _link_type(value)


def _api_base() -> str:
    base = str(_runtime_setting("EXTRACT_LINK_API_BASE", "") or "").strip().rstrip("/")
    if not base:
        raise ValueError("EXTRACT_LINK_API_BASE 为空")
    return base


def _cdk(value: str | None = None) -> str:
    cdk = str(value or _runtime_setting("EXTRACT_LINK_CDK", "") or "").strip()
    if not cdk:
        raise ValueError("EXTRACT_LINK_CDK/CDK 为空")
    return cdk


_WORKERS = _int_setting("EXTRACT_LINK_WORKERS", 3, 1, 16)
_QUEUE_LIMIT = _int_setting("EXTRACT_LINK_QUEUE_LIMIT", 500, _WORKERS, 5000)
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="extract-link")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)


_ERROR_MESSAGES = {
    "zero_trial_ineligible": "GCash 不符合当前 0 元试用资格",
    "eligibility_proof_invalid": "0 元试用资格证明无效或已过期",
    "eligibility_check_unavailable": "服务端 0 元资格检测暂时不可用，请稍后重试",
    "gcash_payment_method_unavailable": "当前优惠结账不支持 GCash",
    "gcash_currency_mismatch": "GCash 结账币种不匹配",
    "link_type_unsupported": "提链服务不支持该支付方式",
    "link_type_disabled": "该支付方式当前已停用",
    "card_key_invalid": "CDK 无效或已失效",
    "card_key_exhausted": "CDK 可用次数已耗尽",
}


class ExtractLinkApiError(RuntimeError):
    """提链 API 错误，保留稳定码并提供可直接展示的消息。"""

    def __init__(self, *, status_code: int | None = None, code: str = "", detail: str = "", payload=None):
        self.status_code = status_code
        self.code = str(code or "").strip()
        self.detail = str(detail or "").strip()
        self.payload = payload
        super().__init__(self.user_message)

    @property
    def user_message(self) -> str:
        if self.code in _ERROR_MESSAGES:
            return _ERROR_MESSAGES[self.code]
        if self.detail:
            return self.detail
        if self.code:
            return self.code
        if self.status_code:
            return f"提链服务请求失败（HTTP {self.status_code}）"
        return "提链服务请求失败"


def _error_code_from_value(value) -> str:
    if isinstance(value, dict):
        for key in ("code", "error_code", "reason", "type"):
            item = value.get(key)
            if item and isinstance(item, (str, int)):
                return str(item).strip()
        for key in ("task", "failure", "error", "detail"):
            nested = value.get(key)
            code = _error_code_from_value(nested)
            if code:
                return code
    return ""


def _error_detail_from_value(value) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        parts = [_error_detail_from_value(item) for item in value]
        return "; ".join(part for part in parts if part)
    if not isinstance(value, dict):
        return ""
    for key in ("message", "detail", "reason", "error", "msg", "description"):
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            return item.strip()
        nested = _error_detail_from_value(item)
        if nested:
            return nested
    return ""


def _error_parts(data) -> tuple[str, str]:
    """从 FastAPI/任务状态的多种错误结构提取 code/detail。"""
    if data is None:
        return "", ""
    if isinstance(data, str):
        return data.strip(), data.strip()
    if isinstance(data, list):
        detail = _error_detail_from_value(data)
        return "", detail
    if not isinstance(data, dict):
        return "", str(data)

    candidates = [data]
    task = data.get("task")
    if isinstance(task, dict):
        candidates.append(task)
    for key in ("detail", "error", "failure", "reason"):
        value = data.get(key)
        if isinstance(value, dict):
            candidates.append(value)
        elif isinstance(value, (str, int, float)):
            candidates.append({"detail": value})
    for candidate in candidates:
        code = _error_code_from_value(candidate)
        detail = _error_detail_from_value(candidate)
        if code and code not in {detail, "error"}:
            return code, detail or code
    detail = _error_detail_from_value(data)
    if detail:
        return detail, detail
    return "", json.dumps(data, ensure_ascii=False)[:500]


def _api_error(*, status_code: int | None, payload) -> ExtractLinkApiError:
    code, detail = _error_parts(payload)
    # FastAPI commonly returns {"detail": "stable_code"}; use that code as
    # the stable identifier while retaining the original detail.
    if not code and detail:
        code = detail if detail in _ERROR_MESSAGES else ""
    return ExtractLinkApiError(status_code=status_code, code=code, detail=detail, payload=payload)


def _response_payload(resp) -> object:
    try:
        return resp.json()
    except Exception:
        text = getattr(resp, "text", "") or ""
        return {"detail": text[:500]} if text else {}


def _http_error_payload(exc: HTTPError) -> object:
    try:
        raw = exc.read().decode("utf-8", "replace")
        if raw:
            try:
                return json.loads(raw)
            except Exception:
                return {"detail": raw[:500]}
    except Exception:
        pass
    return {"detail": str(exc)}


def queue_settings() -> dict:
    return {"workers": _WORKERS, "queue_limit": _QUEUE_LIMIT}


def _session():
    if curl_requests is None:
        return None
    return curl_requests.Session()


def query_cdk(*, cdk: str | None = None) -> dict:
    base = _api_base()
    code = _cdk(cdk)
    timeout = _int_setting("EXTRACT_LINK_REQUEST_TIMEOUT", 30, 5, 300)
    s = _session()
    try:
        if s is None:
            body = json.dumps({"cdk": code}).encode("utf-8")
            req = Request(f"{base}/api/card-key/verify", data=body,
                          headers={"Accept": "application/json", "Content-Type": "application/json"}, method="POST")
            try:
                with urlopen(req, timeout=timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8", "replace") or "{}")
                return payload if isinstance(payload, dict) else {}
            except HTTPError as exc:
                if exc.code != 404:
                    raise _api_error(status_code=exc.code, payload=_http_error_payload(exc)) from exc
            except Exception:
                pass
            req = Request(f"{base}/api/cdk?{urlencode({'code': code})}", headers={"Accept": "application/json"})
            with urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace") or "{}")
            return payload if isinstance(payload, dict) else {}
        resp = s.post(f"{base}/api/card-key/verify", json={"cdk": code}, timeout=timeout)
        if resp.status_code == 404:
            resp = s.get(f"{base}/api/cdk?{urlencode({'code': code})}", timeout=timeout)
        try:
            payload = resp.json()
        except Exception:
            payload = {"error": (resp.text or "")[:300]}
        if resp.status_code < 200 or resp.status_code >= 300:
            raise _api_error(status_code=resp.status_code, payload=payload)
        return payload if isinstance(payload, dict) else {}
    finally:
        try:
            s.close()
        except Exception:
            pass


def _create_extract_job(*, token: str, link_type: str, cdk: str) -> dict:
    base = _api_base()
    timeout = _int_setting("EXTRACT_LINK_REQUEST_TIMEOUT", 30, 5, 300)
    payload = {"link_type": _link_type(link_type), "cdk": _cdk(cdk), "access_token": token,
               "device_id": "", "user_agent": "", "eligibility_proof": "", "options": {}}
    s = _session()
    try:
        if s is None:
            body = json.dumps(payload).encode("utf-8")
            req = Request(
                f"{base}/api/extractions",
                data=body,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urlopen(req, timeout=timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8", "replace") or "{}")
            except HTTPError as exc:
                raise _api_error(status_code=exc.code, payload=_http_error_payload(exc)) from exc
            if isinstance(data, dict) and not (data.get("task_id") or data.get("job_id") or data.get("id")) and isinstance(data.get("accepted"), list) and data["accepted"]:
                data.update(data["accepted"][0] if isinstance(data["accepted"][0], dict) else {})
            if not isinstance(data, dict) or not (data.get("task_id") or data.get("job_id") or data.get("id")):
                raise RuntimeError(f"提链服务未返回 task_id: {data}")
            data.setdefault("job_id", data.get("task_id") or data.get("id"))
            return data
        resp = s.post(f"{base}/api/extractions", json=payload, timeout=timeout)
        try:
            data = resp.json()
        except Exception:
            data = {"error": (resp.text or "")[:300]}
        if resp.status_code < 200 or resp.status_code >= 300:
            raise _api_error(status_code=resp.status_code, payload=data)
        if isinstance(data, dict) and not (data.get("task_id") or data.get("job_id") or data.get("id")) and isinstance(data.get("accepted"), list) and data["accepted"]:
            data.update(data["accepted"][0] if isinstance(data["accepted"][0], dict) else {})
        if not isinstance(data, dict) or not (data.get("task_id") or data.get("job_id") or data.get("id")):
            raise RuntimeError(f"提链服务未返回 task_id: {data}")
        data.setdefault("job_id", data.get("task_id") or data.get("id"))
        return data
    finally:
        try:
            s.close()
        except Exception:
            pass


def _iter_sse_events(*, job_id: str, cdk: str):
    """兼容旧 SSE 名称，实际轮询 ai.pupux.xyz extraction 状态接口。"""
    base = _api_base()
    timeout = _int_setting("EXTRACT_LINK_EVENT_TIMEOUT", 180, 30, 900)
    interval = min(5.0, max(0.5, _int_setting("EXTRACT_LINK_POLL_INTERVAL", 2, 1, 10)))
    s = _session()
    try:
        started = time.monotonic()
        while time.monotonic() - started < timeout:
            url = f"{base}/api/extractions/{quote(str(job_id), safe='')}"
            if s is None:
                req = Request(url, headers={"Accept": "application/json"})
                try:
                    with urlopen(req, timeout=min(30, timeout)) as resp:
                        data = json.loads(resp.read().decode("utf-8", "replace") or "{}")
                except HTTPError as exc:
                    raise _api_error(status_code=exc.code, payload=_http_error_payload(exc)) from exc
            else:
                resp = s.get(url, timeout=min(30, timeout))
                data = _response_payload(resp)
                if resp.status_code < 200 or resp.status_code >= 300:
                    raise _api_error(status_code=resp.status_code, payload=data)
            if not isinstance(data, dict): data = {"raw": data}
            if isinstance(data.get("task"), dict):
                data = data["task"]
            status = str(data.get("status") or data.get("state") or "").lower()
            if status in {"failed", "error", "cancelled", "canceled"}:
                yield "error", data; return
            result = data.get("result") or data.get("payload")
            if status in {"success", "succeeded", "completed", "done", "finished"}:
                yield "result", {"result": result if isinstance(result, dict) else data}; yield "done", data; return
            msg = data.get("message") or data.get("detail")
            if msg: yield "log", {"message": msg}
            time.sleep(interval)
        raise TimeoutError("提链任务轮询超时")
    finally:
        try:
            s.close()
        except Exception:
            pass


def _extract_error_message(data) -> str:
    """尽量从提链服务返回的任意错误结构中提取用户可读原因。"""
    code, detail = _error_parts(data)
    if code in _ERROR_MESSAGES:
        return _ERROR_MESSAGES[code]
    return detail or code


def _format_failure_reason(exc: Exception, logs: list[str] | None = None, last_event: dict | None = None) -> str:
    reason = str(exc).strip()
    if isinstance(exc, ExtractLinkApiError):
        reason = exc.user_message
    elif reason:
        reason = f"提链请求失败：{reason}"
    if (not str(exc).strip()) and logs:
        reason = str(logs[-1])
    if last_event and "提链事件流结束但未返回 result" in reason:
        extracted = _extract_error_message(last_event.get("data"))
        if extracted:
            reason = f"提链事件流结束但未返回 result；最后事件 {last_event.get('event')}: {extracted}"
    return reason[:500]


def _normalize_result(result: dict, *, link_type: str) -> dict:
    """把 pupux extraction/workbench 结果统一成 WebUI 现有字段。"""
    src = dict(result or {})
    out = dict(src)
    out.setdefault("long_url", src.get("payment_url") or src.get("upi_hosted_instructions_url")
                   or src.get("hosted_instructions_url") or src.get("checkout_url") or src.get("url"))
    out.setdefault("copy_paste", src.get("copy_paste") or src.get("copyPaste") or src.get("payment_url"))
    qr = src.get("qr_image") or src.get("qr")
    if isinstance(qr, dict) and qr.get("data"):
        media = qr.get("media_type") or "image/png"
        qr = f"data:{media};base64,{qr['data']}" if qr.get("encoding") == "base64" else qr.get("data")
    if qr:
        if str(qr).startswith("data:image/svg") or str(qr).lower().endswith(".svg"):
            out.setdefault("image_url_svg", qr)
        else:
            out.setdefault("image_url_png", qr)
    out.setdefault("payment_method", src.get("payment_method") or src.get("link_type") or link_type)
    out.setdefault("payment_link_type", src.get("payment_link_type") or src.get("link_type") or link_type)
    out.setdefault("expires_at", src.get("expires_at") or src.get("expiry") or src.get("expiresAt"))
    return out


def _run_extract(*, account_id: int, email: str, access_token: str, link_type: str, cdk: str, trigger: str) -> dict:
    logs: list[str] = []
    last_event = None
    try:
        if not db.mark_account_extract_running(account_id):
            return {"ok": False, "error": "账号已删除或提链状态已被重置"}
        job = _create_extract_job(token=access_token, link_type=link_type, cdk=cdk)
        job_id = str(job.get("job_id") or "")
        db.update_account_extract(account_id, {
            "ok": False,
            "status": "running",
            "job_id": job_id,
            "link_type": link_type,
            "message": "提链任务已创建，等待结果",
            "cdk_remaining": job.get("cdk_remaining"),
        })
        for event, data in _iter_sse_events(job_id=job_id, cdk=cdk):
            last_event = {"event": event, "data": data}
            if event == "log":
                msg = str((data or {}).get("message") or "")[:300]
                if msg:
                    logs.append(msg)
                    db.update_account_extract(account_id, {
                        "ok": False,
                        "status": "running",
                        "job_id": job_id,
                        "link_type": link_type,
                        "message": msg,
                    })
            elif event == "result":
                result = (data or {}).get("result") if isinstance(data, dict) else None
                if not isinstance(result, dict):
                    result = {}
                result = _normalize_result(result, link_type=link_type)
                final = {"ok": True, "status": "success", "job_id": job_id, "link_type": link_type, "result": result, "logs": logs}
                db.update_account_extract(account_id, final)
                logger.info("[提链] 成功: %s type=%s job=%s", email, link_type, job_id)
                return final
            elif event == "error":
                api_exc = _api_error(status_code=None, payload=data)
                if api_exc.code or api_exc.detail:
                    raise api_exc
                raise RuntimeError("提链任务失败")
            elif event == "done":
                break
        raise RuntimeError(f"提链事件流结束但未返回 result: {last_event}")
    except Exception as exc:
        reason = _format_failure_reason(exc, logs=logs, last_event=last_event)
        result = {
            "ok": False,
            "status": "failed",
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": reason,
            "message": reason,
        }
        if isinstance(exc, ExtractLinkApiError) and exc.code:
            result["error_code"] = exc.code
        try:
            db.update_account_extract(account_id, result)
        except Exception:
            logger.exception("[提链] 写入失败状态异常: account_id=%s", account_id)
        logger.exception("[提链] 失败: %s", email)
        return result
    finally:
        _QUEUE_SLOTS.release()


def enqueue_account_extract(*, account_id: int, email: str, access_token: str, trigger: str = "manual", link_type: str | None = None, cdk: str | None = None) -> dict:
    lt = _link_type(link_type)
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "error": "提链队列已满"}
    try:
        code = _cdk(cdk)
        if not db.claim_account_extract(account_id, trigger=trigger, link_type=lt):
            _QUEUE_SLOTS.release()
            return {"accepted": False, "busy": True, "error": "该账号正在提链中"}
        fut = _EXECUTOR.submit(_run_extract, account_id=account_id, email=email, access_token=access_token, link_type=lt, cdk=code, trigger=trigger)
        return {"accepted": True, "busy": False, "future": fut, "link_type": lt}
    except Exception:
        _QUEUE_SLOTS.release()
        raise
