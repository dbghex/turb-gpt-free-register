# -*- coding: utf-8 -*-
"""
接码平台客户端。

用于 Codex OAuth "全新 session" 流程过 OpenAI 的 /phone-verification 手机号验证：
    1. acquire_number()       getNumber 取一个手机号（返回 激活ID + 号码）
    2. wait_for_sms_code()    轮询 getStatus 直到拿到短信验证码
    3. complete() / cancel()  setStatus 标记完成(6) / 取消(8)

当前支持：
    - GrizzlySMS：GET 文本接口，文档 https://api.grizzlysms.com
    - HeroSMS：SMS-Activate 兼容接口，国家/服务/价格在任务入队时锁定
    - SMSBower：GET handler_api 兼容接口，文档 https://smsbower.app/cn/api?page=client
    - L：本地 JSON 管理接口，文档 L_API.md
    - H：本地 JSON 管理接口，文档 H_API.md

价格相关：每取一个号、收到短信都会计费，所以：
    - 取号后若收不到短信，必须 cancel(8) 释放，避免白扣钱；
    - 成功拿到码后 complete(6) 正式完成激活。
"""
import json
import logging
import re
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path
from urllib.parse import urljoin

from curl_cffi.requests import Session as CurlSession

# 注意：用 `from config import codex` 而不是 `from config.codex import X`，
# 这样 WebUI 调 config.reload_all() 后，本模块通过 codex.X 读到的是最新值。
from config import codex as _cfg
from config import IMPERSONATE

logger = logging.getLogger(__name__)

# GrizzlySMS 规则：号码取出后 2 分钟内不允许取消（防薅号）。
# 这里留 5 秒缓冲，时间到了再发 setStatus=8。
_MIN_CANCEL_DELAY = 125

# Hero/Grizzly 的待取消激活会落到这个轻量 ledger。文件只记录平台、激活 ID、
# 时间和错误信息，不记录手机号、API key 或本地服务授权码。
_PENDING_CANCELLATIONS_PATH = (
    Path(__file__).resolve().parent.parent / "run" / "sms_pending_cancellations.json"
)
_FREE_CANCEL_WINDOW = 20 * 60

# 记录每个 activation_id 的取号时间，供 cancel() 判断是否要等。
# 用模块级 dict 而不是改 acquire_number 返回值，保持向后兼容。
_ACQUIRED_AT: dict[str, float] = {}


def _as_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def _as_int(value, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return default


def _public_api_base(value: str) -> str:
    """防止误把 URL 中内嵌的认证信息写入任务 DB 或取消 ledger。"""
    text = str(value or "")
    text = re.sub(r"(?i)(https?://)[^/@\s]+@", r"\1***@", text)
    return re.sub(
        r"(?i)([?&](?:api_?key|token|auth|password)=)[^&#\s]+",
        r"\1***",
        text,
    )


@dataclass(frozen=True)
class SmsTaskSettings:
    """一次任务完整生命周期使用的不可变接码配置。

    WebUI 可以继续热加载全局配置，但已经排队/运行的任务通过 ContextVar 始终
    使用创建任务时的这个对象，避免取号、等码、完成和延迟取消串到另一平台。
    """

    provider: str
    service: str
    country: str
    max_price: str
    max_retries: int
    code_wait: int
    poll_interval: int
    request_timeout: int
    api_base: str
    api_key: str
    phone_prefix: str = ""
    phone_acquire_mode: str = "reusable"
    fixed_price: bool = False
    operator: str = ""
    phone_exception: str = ""
    price_mode: str = "quote_buffer"
    price_buffer_percent: float = 15.0
    # 显式构造的 settings 视为调用方已锁定；from_runtime 会设为 False，
    # 使 quote_buffer 在任务入队时重新读取一次实时价格。
    price_resolved: bool = True
    smsbower_use_v2: bool = True
    smsbower_min_price: str = ""
    smsbower_provider_ids: str = ""
    smsbower_except_provider_ids: str = ""

    @classmethod
    def from_runtime(cls, provider: str | None = None) -> "SmsTaskSettings":
        selected = str(
            provider if provider is not None else getattr(_cfg, "SMS_PROVIDER", "grizzly")
            or "grizzly"
        ).strip().lower()
        common_service = str(getattr(_cfg, "SMS_SERVICE", "") or "").strip()
        common_country = str(getattr(_cfg, "SMS_COUNTRY", "") or "").strip()
        common_max_price = str(getattr(_cfg, "SMS_MAX_PRICE", "") or "").strip()

        api_base = str(getattr(_cfg, "SMS_API_BASE", "") or "").strip()
        api_key = str(getattr(_cfg, "SMS_API_KEY", "") or "").strip()
        service = common_service
        country = common_country
        max_price = common_max_price
        phone_prefix = ""
        phone_acquire_mode = "reusable"
        fixed_price = False
        operator = ""
        phone_exception = ""
        price_mode = "quote_buffer"
        try:
            price_buffer_percent = float(getattr(_cfg, "HERO_SMS_PRICE_BUFFER_PERCENT", 15) or 15)
        except (TypeError, ValueError):
            price_buffer_percent = 15.0

        if selected == "hero":
            api_base = str(getattr(_cfg, "HERO_SMS_API_BASE", api_base) or api_base).strip()
            api_key = str(getattr(_cfg, "HERO_SMS_API_KEY", "") or "").strip()
            service = str(getattr(_cfg, "HERO_SMS_SERVICE", "") or "").strip()
            country = str(getattr(_cfg, "HERO_SMS_COUNTRY", "") or "").strip()
            max_price = str(getattr(_cfg, "HERO_SMS_MAX_PRICE", "") or "").strip()
            fixed_price = _as_bool(getattr(_cfg, "HERO_SMS_FIXED_PRICE", False))
            operator = str(getattr(_cfg, "HERO_SMS_OPERATOR", "") or "").strip()
            phone_exception = str(getattr(_cfg, "HERO_SMS_PHONE_EXCEPTION", "") or "").strip()
            price_mode = str(
                getattr(_cfg, "HERO_SMS_PRICE_MODE", "quote_buffer") or "quote_buffer"
            ).strip().lower()
            if price_mode == "unlimited":
                max_price = ""
                fixed_price = False
        elif selected == "smsbower":
            api_base = str(getattr(_cfg, "SMSBOWER_API_BASE", "") or "").strip()
            api_key = str(getattr(_cfg, "SMSBOWER_API_KEY", "") or "").strip()
            phone_exception = str(getattr(_cfg, "SMSBOWER_PHONE_EXCEPTION", "") or "").strip()
        elif selected == "l":
            api_base = str(getattr(_cfg, "L_API_BASE", "") or "").strip()
            api_key = str(getattr(_cfg, "L_ADMIN_AUTH_CODE", "") or "").strip()
            phone_prefix = str(getattr(_cfg, "L_PHONE_PREFIX", "") or "").strip()
        elif selected == "h":
            api_base = str(getattr(_cfg, "H_API_BASE", "") or "").strip()
            api_key = str(getattr(_cfg, "H_ADMIN_AUTH_CODE", "") or "").strip()
            phone_prefix = str(getattr(_cfg, "H_PHONE_PREFIX", "") or "").strip()
            phone_acquire_mode = str(
                getattr(_cfg, "H_PHONE_ACQUIRE_MODE", "reusable") or "reusable"
            ).strip().lower()

        return cls(
            provider=selected,
            service=service,
            country=country,
            max_price=max_price,
            max_retries=_as_int(getattr(_cfg, "SMS_MAX_RETRIES", 10), 10, 1),
            code_wait=_as_int(getattr(_cfg, "SMS_CODE_WAIT", 120), 120, 1),
            poll_interval=_as_int(getattr(_cfg, "SMS_POLL_INTERVAL", 5), 5, 0),
            request_timeout=_as_int(getattr(_cfg, "SMS_REQUEST_TIMEOUT", 30), 30, 1),
            api_base=api_base,
            api_key=api_key,
            phone_prefix=phone_prefix,
            phone_acquire_mode=phone_acquire_mode,
            fixed_price=fixed_price,
            operator=operator,
            phone_exception=phone_exception,
            price_mode=price_mode,
            price_buffer_percent=price_buffer_percent,
            price_resolved=False,
            smsbower_use_v2=_as_bool(getattr(_cfg, "SMSBOWER_USE_V2", True)),
            smsbower_min_price=str(getattr(_cfg, "SMSBOWER_MIN_PRICE", "") or "").strip(),
            smsbower_provider_ids=str(getattr(_cfg, "SMSBOWER_PROVIDER_IDS", "") or "").strip(),
            smsbower_except_provider_ids=str(getattr(_cfg, "SMSBOWER_EXCEPT_PROVIDER_IDS", "") or "").strip(),
        )

    def public_snapshot(self) -> dict:
        """可安全写入任务 DB / 返回 WebUI 的快照（不含任何 secret）。"""
        return {
            "provider": self.provider,
            "service": self.service,
            "country": self.country,
            "max_price": self.max_price,
            "max_retries": self.max_retries,
            "code_wait": self.code_wait,
            "poll_interval": self.poll_interval,
            "request_timeout": self.request_timeout,
            "api_base": _public_api_base(self.api_base),
            "api_key_configured": bool(self.api_key),
            "phone_prefix": self.phone_prefix,
            "phone_acquire_mode": self.phone_acquire_mode,
            "fixed_price": self.fixed_price,
            "operator": self.operator,
            "phone_exception": self.phone_exception,
            "price_mode": self.price_mode,
            "price_buffer_percent": self.price_buffer_percent,
            "price_resolved": self.price_resolved,
        }


_TASK_SETTINGS: ContextVar[SmsTaskSettings | None] = ContextVar(
    "sms_task_settings", default=None
)


def bound_settings() -> SmsTaskSettings | None:
    """返回当前显式绑定的任务配置；未绑定时不隐式读取全局配置。"""
    return _TASK_SETTINGS.get()


def current_settings() -> SmsTaskSettings:
    """返回当前任务快照；独立调用场景则按调用时的运行配置生成一次快照。"""
    return bound_settings() or SmsTaskSettings.from_runtime()


@contextmanager
def bind_settings(settings: SmsTaskSettings | None = None):
    """在当前执行上下文中绑定接码配置，并在退出时可靠恢复上一层配置。"""
    selected = settings or current_settings()
    token = _TASK_SETTINGS.set(selected)
    try:
        yield selected
    finally:
        _TASK_SETTINGS.reset(token)


@dataclass(frozen=True)
class _Acquisition:
    settings: SmsTaskSettings
    acquired_at: float


_ACQUISITIONS: dict[tuple[str, str], _Acquisition] = {}
_ACQUISITIONS_LOCK = threading.RLock()
_LEDGER_LOCK = threading.RLock()
_RESUME_LOCK = threading.Lock()
_PENDING_RESUMED = False


class SmsProviderError(RuntimeError):
    """接码平台通用错误。"""


class SmsFatalProviderError(SmsProviderError):
    """平台/参数/余额等不可通过换号解决的错误。"""


class SmsConfigurationError(SmsFatalProviderError):
    """入队前即可发现的配置错误；Web API 应向用户返回 400。"""


class SmsRetryableProviderError(SmsProviderError):
    """可以在当前任务重试的暂时性平台错误。"""


class SmsPurchaseUnknownError(SmsFatalProviderError):
    """getNumber 请求超时或结果未知；禁止盲目再次购买。"""


class SmsNoNumbersError(SmsRetryableProviderError):
    """暂无可用号码（NO_NUMBERS），可换国家或稍后重试。"""


class SmsNoBalanceError(SmsFatalProviderError):
    """余额不足（NO_BALANCE），必须充值，重试无意义——上层应立即停止。"""


class SmsCodeTimeout(SmsRetryableProviderError):
    """单个号等短信超时（OpenAI 没发或没到达）。"""


def _settings_for_activation(
    activation_id: str | None = None,
    settings: SmsTaskSettings | None = None,
) -> SmsTaskSettings:
    """优先使用显式/上下文配置；跨线程时从激活记录恢复原配置。"""
    if settings is not None:
        return settings
    current = bound_settings()
    if activation_id:
        aid = str(activation_id).strip()
        if current is not None:
            with _ACQUISITIONS_LOCK:
                item = _ACQUISITIONS.get((current.provider, aid))
            if item is not None:
                return item.settings
        with _ACQUISITIONS_LOCK:
            matches = [item for (provider, key), item in _ACQUISITIONS.items() if key == aid]
        if len(matches) == 1:
            return matches[0].settings
    return current or current_settings()


def _remember_acquisition(activation_id: str, settings: SmsTaskSettings) -> float:
    now = time.time()
    aid = str(activation_id).strip()
    with _ACQUISITIONS_LOCK:
        _ACQUISITIONS[(settings.provider, aid)] = _Acquisition(settings, now)
        # 兼容旧代码/测试对该字典的读取。
        _ACQUIRED_AT[aid] = now
    if settings.provider in {"grizzly", "hero"}:
        _ledger_put(settings, aid, now)
    return now


def _forget_acquisition(activation_id: str, settings: SmsTaskSettings | None = None) -> None:
    aid = str(activation_id or "").strip()
    if not aid:
        return
    with _ACQUISITIONS_LOCK:
        selected = settings.provider if settings is not None else None
        keys = [key for key in _ACQUISITIONS if key[1] == aid and (selected is None or key[0] == selected)]
        for key in keys:
            _ACQUISITIONS.pop(key, None)
        _ACQUIRED_AT.pop(aid, None)
    if settings is not None and settings.provider in {"grizzly", "hero"}:
        _ledger_remove(settings, aid)


def _ledger_read() -> list[dict]:
    try:
        if not _PENDING_CANCELLATIONS_PATH.exists():
            return []
        raw = json.loads(_PENDING_CANCELLATIONS_PATH.read_text(encoding="utf-8"))
        return list(raw) if isinstance(raw, list) else []
    except Exception as exc:
        logger.warning("[SMS] 读取待取消 ledger 失败：%s", exc)
        return []


def _ledger_write(rows: list[dict]) -> None:
    try:
        _PENDING_CANCELLATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _PENDING_CANCELLATIONS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_PENDING_CANCELLATIONS_PATH)
    except Exception as exc:
        logger.warning("[SMS] 写入待取消 ledger 失败：%s", exc)


def _ledger_key(settings: SmsTaskSettings, activation_id: str) -> str:
    return f"{settings.provider}:{str(activation_id).strip()}"


def _ledger_put(settings: SmsTaskSettings, activation_id: str, acquired_at: float) -> None:
    with _LEDGER_LOCK:
        rows = _ledger_read()
        key = _ledger_key(settings, activation_id)
        old = next((row for row in rows if row.get("key") == key), None)
        if old is None:
            rows.append({
                "key": key,
                "provider": settings.provider,
                "activation_id": str(activation_id),
                "api_base": _public_api_base(settings.api_base),
                "service": settings.service,
                "country": settings.country,
                "acquired_at": float(acquired_at),
                "attempts": 0,
                "status": "pending",
                "last_error": "",
            })
        else:
            old.update({"status": "pending", "acquired_at": float(acquired_at)})
        _ledger_write(rows)


def _ledger_update(settings: SmsTaskSettings, activation_id: str, **updates) -> None:
    with _LEDGER_LOCK:
        rows = _ledger_read()
        key = _ledger_key(settings, activation_id)
        row = next((item for item in rows if item.get("key") == key), None)
        if row is not None:
            row.update(updates)
            _ledger_write(rows)


def _ledger_remove(settings: SmsTaskSettings, activation_id: str) -> None:
    with _LEDGER_LOCK:
        rows = _ledger_read()
        key = _ledger_key(settings, activation_id)
        new_rows = [row for row in rows if row.get("key") != key]
        if len(new_rows) != len(rows):
            _ledger_write(new_rows)


def _settings_from_ledger(row: dict) -> SmsTaskSettings:
    """进程重启后用当前 .env 中对应 provider 的 secret 恢复取消请求。"""
    provider = str(row.get("provider") or "grizzly").strip().lower()
    settings = SmsTaskSettings.from_runtime(provider=provider)
    # base/service/country 是非 secret 快照，优先沿用取号时的值；key 始终只从当前
    # 环境读取，避免把密钥写进 ledger。
    return replace(
        settings,
        api_base=str(row.get("api_base") or settings.api_base),
        service=str(row.get("service") or settings.service),
        country=str(row.get("country") or settings.country),
    )


def resume_pending_cancellations() -> int:
    """恢复上次进程遗留的待取消激活；返回已派发线程数。"""
    global _PENDING_RESUMED
    with _RESUME_LOCK:
        if _PENDING_RESUMED:
            return 0
        _PENDING_RESUMED = True
    resumed = 0
    with _LEDGER_LOCK:
        rows = _ledger_read()
    for row in rows:
        if str(row.get("status") or "pending") not in {"pending", "retrying"}:
            continue
        aid = str(row.get("activation_id") or "").strip()
        if not aid:
            continue
        settings = _settings_from_ledger(row)
        acquired_at = float(row.get("acquired_at") or time.time())
        with _ACQUISITIONS_LOCK:
            _ACQUISITIONS.setdefault((settings.provider, aid), _Acquisition(settings, acquired_at))
            _ACQUIRED_AT.setdefault(aid, acquired_at)
        thread = threading.Thread(
            target=_do_cancel_sync,
            args=(aid, settings, None, acquired_at),
            name=f"sms-cancel-resume-{aid}",
            daemon=True,
        )
        thread.start()
        resumed += 1
    if resumed:
        logger.info("[SMS] 已恢复 %s 个待取消激活", resumed)
    return resumed


def _http(settings: SmsTaskSettings | None = None) -> CurlSession:
    selected = settings or current_settings()
    s = CurlSession(impersonate=IMPERSONATE)
    s.timeout = selected.request_timeout
    return s


def _provider(settings: SmsTaskSettings | None = None) -> str:
    return (settings or current_settings()).provider


def _request_grizzly(
    http: CurlSession,
    params: dict,
    settings: SmsTaskSettings | None = None,
) -> str:
    """
    发一个 GrizzlySMS API 请求，返回去空白的响应文本。
    统一识别公共错误码并抛对应异常。
    """
    selected = settings or current_settings()
    base_params = {"api_key": selected.api_key}
    base_params.update(params)
    resp = http.get(selected.api_base, params=base_params)
    if resp.status_code != 200:
        raise SmsRetryableProviderError(
            f"GrizzlySMS HTTP {resp.status_code}: {(resp.text or '')[:200]}"
        )
    text = (resp.text or "").strip()

    # 公共错误码（任何 action 都可能返回）
    if text == "BAD_KEY":
        raise SmsFatalProviderError("接码平台 API key 无效（BAD_KEY）")
    if text == "NO_BALANCE":
        raise SmsNoBalanceError("接码平台余额不足（NO_BALANCE），请充值")
    if text == "NO_NUMBERS":
        raise SmsNoNumbersError("接码平台暂无可用号码（NO_NUMBERS）")
    if text == "SERVICE_UNAVAILABLE_REGION":
        raise SmsFatalProviderError("接码平台地区受限（SERVICE_UNAVAILABLE_REGION），请换 IP")
    if text in ("BAD_ACTION", "BAD_SERVICE", "BAD_STATUS"):
        raise SmsFatalProviderError(f"接码平台请求参数错误：{text}")
    if text == "NO_ACTIVATION":
        raise SmsFatalProviderError("激活 ID 不存在（NO_ACTIVATION）")
    if text.startswith("The service is prohibited"):
        raise SmsFatalProviderError(f"该服务被平台禁售：{text}")

    return text


def _request_smsbower(http: CurlSession, params: dict, settings: SmsTaskSettings | None = None) -> str:
    """发 SMSBower handler_api 请求，返回去空白的响应文本。"""
    selected = settings or current_settings()
    api_key = selected.api_key
    if not api_key:
        raise SmsProviderError("SMSBower API Key 不能为空")
    base = selected.api_base
    if not base:
        raise SmsProviderError("SMSBOWER_API_BASE 不能为空")
    resp = http.get(base, params={"api_key": api_key, **params})
    text = (resp.text or "").strip()
    if resp.status_code != 200:
        raise SmsProviderError(f"SMSBower HTTP {resp.status_code}: {text[:200]}")
    if text in ("BAD_KEY", "BAD_ACTION", "BAD_SERVICE", "WRONG_SERVICE", "BAD_STATUS", "NO_ACTIVATION"):
        if text == "BAD_KEY":
            raise SmsProviderError("SMSBower API key 无效（BAD_KEY）")
        if text in ("BAD_SERVICE", "WRONG_SERVICE"):
            raise SmsProviderError(f"SMSBower 服务代码无效（{text}），OpenAI/ChatGPT 请填写 dr")
        if text == "NO_ACTIVATION":
            raise SmsProviderError("SMSBower 激活 ID 不存在（NO_ACTIVATION）")
        raise SmsProviderError(f"SMSBower 请求参数错误：{text}")
    if text in ("NO_NUMBERS", "NO_BALANCE", "NO_MONEY"):
        if text in ("NO_BALANCE", "NO_MONEY"):
            raise SmsNoBalanceError(f"SMSBower 余额不足（{text}），请充值")
        raise SmsNoNumbersError("SMSBower 暂无可用号码（NO_NUMBERS）")
    if text.startswith("The service is prohibited"):
        raise SmsProviderError(f"SMSBower 该服务被禁售：{text}")
    return text


def _smsbower_number_params(service: str | None, country: str | None, settings: SmsTaskSettings | None = None) -> dict:
    selected = settings or current_settings()
    service_code = str(service or selected.service or "").strip()
    if service_code.lower() in ("openai", "chatgpt"):
        service_code = "dr"
    params = {
        "action": "getNumberV2" if selected.smsbower_use_v2 else "getNumber",
        "service": service_code,
        "country": str(country or selected.country or "").strip(),
    }
    for key, value in (
        ("maxPrice", selected.max_price),
        ("minPrice", selected.smsbower_min_price),
        ("providerIds", selected.smsbower_provider_ids),
        ("exceptProviderIds", selected.smsbower_except_provider_ids),
        ("phoneException", selected.phone_exception),
    ):
        value = str(value or "").strip()
        if value:
            params[key] = value
    return params


def _request_smsbower_number(http: CurlSession, params: dict, settings: SmsTaskSettings | None = None) -> tuple[dict, str]:
    """按兼容性顺序取号，筛选无库存时放宽供应商条件。"""
    candidates: list[dict] = [dict(params)]
    if params.get("action") == "getNumberV2":
        candidates.append({**params, "action": "getNumber"})
    if params.get("providerIds"):
        without_provider = {key: value for key, value in params.items() if key != "providerIds"}
        candidates.append(without_provider)
        if params.get("action") == "getNumberV2":
            candidates.append({**without_provider, "action": "getNumber"})
    unique = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    for index, candidate in enumerate(unique):
        try:
            return candidate, _request_smsbower(http, candidate, settings)
        except SmsNoNumbersError:
            if index + 1 < len(unique):
                logger.warning("[SMSBower] 取号筛选无库存，放宽条件重试：action=%s providerIds=%s", candidate.get("action"), candidate.get("providerIds", "-"))
                continue
            raise
        except SmsProviderError as exc:
            if candidate.get("action") == "getNumberV2" and "BAD_ACTION" in str(exc) and index + 1 < len(unique):
                logger.warning("[SMSBower] getNumberV2 不被当前接口支持，回退兼容接口")
                continue
            raise
    raise SmsProviderError("SMSBower 没有可用的取号请求方案")


def _l_url(path: str, settings: SmsTaskSettings | None = None) -> str:
    base = (settings or current_settings()).api_base
    if not base:
        raise SmsFatalProviderError("L_API_BASE 不能为空")
    return urljoin(base.rstrip("/") + "/", path.lstrip("/"))


def _l_headers(settings: SmsTaskSettings | None = None) -> dict:
    token = (settings or current_settings()).api_key
    if not token:
        raise SmsFatalProviderError("L_ADMIN_AUTH_CODE 不能为空")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _post_l_json(
    http: CurlSession,
    path: str,
    payload: dict,
    settings: SmsTaskSettings | None = None,
) -> dict:
    selected = settings or current_settings()
    resp = http.post(
        _l_url(path, selected), headers=_l_headers(selected), data=json.dumps(payload)
    )
    text = (resp.text or "").strip()
    try:
        data = resp.json()
    except Exception:
        data = {}

    if resp.status_code != 200:
        msg = data.get("error") if isinstance(data, dict) else ""
        raise SmsRetryableProviderError(f"L HTTP {resp.status_code}: {(msg or text)[:200]}")
    if isinstance(data, dict) and data.get("error"):
        error = str(data.get("error") or "")
        raw = str(data.get("raw") or "")
        combined = f"{error} {raw}".strip()
        if "NO_BALANCE" in combined or "余额不足" in combined:
            raise SmsNoBalanceError(f"L 余额不足：{combined}")
        if "NO_NUMBERS" in combined or "暂无号码" in combined:
            raise SmsNoNumbersError(f"L 暂无可用号码：{combined}")
        raise SmsFatalProviderError(f"L 请求失败：{combined}")
    if not isinstance(data, dict):
        raise SmsFatalProviderError(f"L 响应不是 JSON 对象：{text[:200]}")
    return data


def _h_url(path: str, settings: SmsTaskSettings | None = None) -> str:
    base = (settings or current_settings()).api_base
    if not base:
        raise SmsFatalProviderError("H_API_BASE 不能为空")
    return urljoin(base.rstrip("/") + "/", path.lstrip("/"))


def _h_headers(settings: SmsTaskSettings | None = None) -> dict:
    token = (settings or current_settings()).api_key
    if not token:
        raise SmsFatalProviderError("H_ADMIN_AUTH_CODE 不能为空")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _post_h_json(
    http: CurlSession,
    path: str,
    payload: dict,
    settings: SmsTaskSettings | None = None,
) -> dict:
    selected = settings or current_settings()
    resp = http.post(
        _h_url(path, selected), headers=_h_headers(selected), data=json.dumps(payload)
    )
    text = (resp.text or "").strip()
    try:
        data = resp.json()
    except Exception:
        data = {}

    if resp.status_code != 200:
        msg = data.get("error") if isinstance(data, dict) else ""
        raise SmsRetryableProviderError(f"H HTTP {resp.status_code}: {(msg or text)[:200]}")
    if isinstance(data, dict) and data.get("error"):
        error = str(data.get("error") or "")
        raw = str(data.get("raw") or "")
        combined = f"{error} {raw}".strip()
        if "NO_BALANCE" in combined or "余额不足" in combined:
            raise SmsNoBalanceError(f"H 余额不足：{combined}")
        if "NO_NUMBERS" in combined or "暂无号码" in combined:
            raise SmsNoNumbersError(f"H 暂无可用号码：{combined}")
        raise SmsFatalProviderError(f"H 请求失败：{combined}")
    if not isinstance(data, dict):
        raise SmsFatalProviderError(f"H 响应不是 JSON 对象：{text[:200]}")
    return data


def _release_h_number(
    activation_id: str,
    http: CurlSession | None = None,
    settings: SmsTaskSettings | None = None,
) -> dict:
    """调用 H_API /api/admin/h/release 释放单个号码。"""
    activation_id = str(activation_id or "").strip()
    if not activation_id:
        raise SmsProviderError("H release 缺少 id")
    selected = _settings_for_activation(activation_id, settings)
    own_http = http is None
    http = http or _http(selected)
    try:
        data = _post_h_json(
            http, "/api/admin/h/release", {"id": activation_id}, selected
        )
        failed = data.get("failed") if isinstance(data, dict) else None
        if isinstance(failed, list) and failed:
            detail = json.dumps(failed, ensure_ascii=False)[:300]
            raise SmsProviderError(f"H release 失败 id={activation_id}: {detail}")
        released = data.get("released", data.get("updated", 0)) if isinstance(data, dict) else 0
        logger.info(f"[SMS:H] 已释放号码 id={activation_id}, released={released}")
        _forget_acquisition(activation_id, selected)
        return data
    finally:
        if own_http:
            http.close()


def release_h_numbers(
    ids: list[str],
    http: CurlSession | None = None,
    settings: SmsTaskSettings | None = None,
) -> dict:
    """批量释放 H 号码。"""
    ids = [str(x or "").strip() for x in (ids or []) if str(x or "").strip()]
    if not ids:
        raise SmsProviderError("H release 缺少 ids")
    selected = settings or current_settings()
    own_http = http is None
    http = http or _http(selected)
    try:
        data = _post_h_json(http, "/api/admin/h/release", {"ids": ids}, selected)
        released = data.get("released", data.get("updated", 0)) if isinstance(data, dict) else 0
        failed = data.get("failed") if isinstance(data, dict) else []
        logger.info(f"[SMS:H] 批量释放号码完成 released={released}, failed={len(failed) if isinstance(failed, list) else 0}")
        for activation_id in ids:
            _forget_acquisition(activation_id, selected)
        return data
    finally:
        if own_http:
            http.close()


def _release_l_number(
    activation_id: str,
    http: CurlSession | None = None,
    settings: SmsTaskSettings | None = None,
) -> dict:
    """调用 L_API /api/admin/l/release 释放单个号码。"""
    activation_id = str(activation_id or "").strip()
    if not activation_id:
        raise SmsProviderError("L release 缺少 id")
    selected = _settings_for_activation(activation_id, settings)
    own_http = http is None
    http = http or _http(selected)
    try:
        data = _post_l_json(
            http, "/api/admin/l/release", {"id": activation_id}, selected
        )
        failed = data.get("failed") if isinstance(data, dict) else None
        if isinstance(failed, list) and failed:
            # 接口允许部分失败。单个释放时 failed 非空基本代表这个 id 释放失败。
            detail = json.dumps(failed, ensure_ascii=False)[:300]
            raise SmsProviderError(f"L release 失败 id={activation_id}: {detail}")
        released = data.get("released", data.get("updated", 0)) if isinstance(data, dict) else 0
        logger.info(f"[SMS:L] 已释放号码 id={activation_id}, released={released}")
        _forget_acquisition(activation_id, selected)
        return data
    finally:
        if own_http:
            http.close()


def release_l_numbers(
    ids: list[str],
    http: CurlSession | None = None,
    settings: SmsTaskSettings | None = None,
) -> dict:
    """批量释放 L 号码，供工具/后续批处理复用。"""
    ids = [str(x or "").strip() for x in (ids or []) if str(x or "").strip()]
    if not ids:
        raise SmsProviderError("L release 缺少 ids")
    selected = settings or current_settings()
    own_http = http is None
    http = http or _http(selected)
    try:
        data = _post_l_json(http, "/api/admin/l/release", {"ids": ids}, selected)
        released = data.get("released", data.get("updated", 0)) if isinstance(data, dict) else 0
        failed = data.get("failed") if isinstance(data, dict) else []
        logger.info(f"[SMS:L] 批量释放号码完成 released={released}, failed={len(failed) if isinstance(failed, list) else 0}")
        for activation_id in ids:
            _forget_acquisition(activation_id, selected)
        return data
    finally:
        if own_http:
            http.close()


def _normalize_phone_digits(value: str) -> str:
    """把平台返回/配置的号码片段规范化为纯数字，避免 +-849... 这类非法 E.164。"""
    return "".join(ch for ch in str(value or "").strip() if ch.isdigit())


def _normalize_l_phone(phone: str, settings: SmsTaskSettings | None = None) -> str:
    phone = _normalize_phone_digits(phone)
    prefix = _normalize_phone_digits((settings or current_settings()).phone_prefix)
    if prefix and phone and not phone.startswith(prefix):
        return f"{prefix}{phone}"
    return phone


def _normalize_h_phone(phone: str, settings: SmsTaskSettings | None = None) -> str:
    phone = _normalize_phone_digits(phone)
    prefix = _normalize_phone_digits((settings or current_settings()).phone_prefix)
    if prefix and phone and not phone.startswith(prefix):
        return f"{prefix}{phone}"
    return phone


def _h_phone_acquire_mode(settings: SmsTaskSettings | None = None) -> str:
    """
    H 取号模式：
      - reusable/reuse/prefer_reuse：优先复用，调用 /api/admin/h/take-reusable-phone
      - new/fresh/always_new：每次取新号，调用 /api/admin/h/take-phone
    """
    raw = (settings or current_settings()).phone_acquire_mode
    if raw in ("new", "fresh", "always_new", "take_phone", "take-phone", "每次取新号", "新号"):
        return "new"
    return "reusable"


def _hero_client(settings: SmsTaskSettings, http=None):
    try:
        from core.hero_sms_client import HeroSmsClient
    except ImportError as exc:
        raise SmsFatalProviderError("HeroSMS 客户端未安装或加载失败") from exc
    if not settings.api_base:
        raise SmsFatalProviderError("HERO_SMS_API_BASE 不能为空")
    if not settings.api_key:
        raise SmsFatalProviderError("HERO_SMS_API_KEY 不能为空")
    return HeroSmsClient(
        settings.api_base,
        settings.api_key,
        timeout=settings.request_timeout,
        http=http,
    )


def _raise_mapped_hero_error(exc: Exception):
    try:
        from core.hero_sms_client import HeroSmsError
    except ImportError:
        raise exc
    if not isinstance(exc, HeroSmsError):
        raise exc
    code = str(getattr(exc, "code", "") or "").strip().upper()
    message = str(exc)
    if code == "NO_NUMBERS":
        raise SmsNoNumbersError(message) from exc
    if code in {"NO_BALANCE", "LOW_BALANCE"}:
        raise SmsNoBalanceError(message) from exc
    if code in {"PURCHASE_RESULT_UNKNOWN", "PURCHASE_UNKNOWN"}:
        raise SmsPurchaseUnknownError(message) from exc
    fatal_codes = {
        "BAD_KEY", "NO_KEY", "BAD_SERVICE", "WRONG_COUNTRY", "BAD_ACTION",
        "BAD_STATUS", "BANNED", "CHANNELS_LIMIT", "WRONG_MAX_PRICE",
        "NO_ACTIVATION", "BAD_OPERATOR", "BAD_VALUE",
    }
    if code in fatal_codes or not bool(getattr(exc, "retryable", False)):
        raise SmsFatalProviderError(message) from exc
    raise SmsRetryableProviderError(message) from exc


def _hero_call(callable_):
    try:
        return callable_()
    except Exception as exc:
        _raise_mapped_hero_error(exc)


def _hero_status_text(value) -> str:
    """兼容客户端返回原始字符串或结构化状态。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        status = str(value.get("status") or value.get("state") or "").strip()
        code = str(value.get("code") or value.get("sms_code") or "").strip()
        if status == "STATUS_OK" and code:
            return f"STATUS_OK:{code}"
        if status == "STATUS_WAIT_RETRY" and code:
            return f"STATUS_WAIT_RETRY:{code}"
        return status
    return str(value or "").strip()


def _positive_price(value: str, label: str = "价格上限") -> Decimal:
    try:
        price = Decimal(str(value or "").strip())
    except (InvalidOperation, TypeError, ValueError):
        raise SmsConfigurationError(f"HeroSMS {label}必须是正数") from None
    if not price.is_finite() or price <= 0:
        raise SmsConfigurationError(f"HeroSMS {label}必须大于 0")
    return price


def prepare_task_settings(
    settings: SmsTaskSettings | None = None,
    *,
    http=None,
) -> SmsTaskSettings:
    """校验并冻结任务最终配置；推荐价缺失时只读查询 Hero 实时报价。"""
    selected = settings or current_settings()
    if selected.provider != "hero":
        return selected
    if not selected.api_base:
        raise SmsConfigurationError("未配置 HeroSMS API 地址")
    if not selected.api_key:
        raise SmsConfigurationError("未配置 HeroSMS API Key")
    if not selected.service:
        raise SmsConfigurationError("未选择 HeroSMS 服务")
    if not selected.country:
        raise SmsConfigurationError("未选择 HeroSMS 国家")

    mode = str(selected.price_mode or "quote_buffer").strip().lower()
    if mode == "unlimited":
        return replace(
            selected,
            price_mode=mode,
            max_price="",
            fixed_price=False,
            price_resolved=True,
        )
    if mode in {"manual", "custom"}:
        price = _positive_price(selected.max_price)
        return replace(
            selected,
            price_mode="custom",
            max_price=format(price, "f"),
            price_resolved=True,
        )
    if mode != "quote_buffer":
        raise SmsConfigurationError(f"不支持的 HeroSMS 价格模式：{mode}")
    if selected.price_resolved and selected.max_price:
        price = _positive_price(selected.max_price)
        return replace(
            selected,
            price_mode=mode,
            max_price=format(price, "f"),
            price_resolved=True,
        )

    try:
        buffer_percent = Decimal(str(selected.price_buffer_percent))
    except (InvalidOperation, TypeError, ValueError):
        raise SmsConfigurationError("HeroSMS 推荐价上浮比例必须是数字") from None
    if not buffer_percent.is_finite() or buffer_percent < 0:
        raise SmsConfigurationError("HeroSMS 推荐价上浮比例不能小于 0")

    client = _hero_client(selected, http=http)
    try:
        quotes = _hero_call(lambda: client.get_prices(selected.service, selected.country))
    finally:
        try:
            client.close()
        except Exception:
            pass
    available: list[Decimal] = []
    for quote in quotes or []:
        if not isinstance(quote, dict):
            continue
        try:
            count = max(
                int(quote.get("count", 0) or 0),
                int(quote.get("physicalCount", 0) or 0),
            )
        except (TypeError, ValueError):
            count = 0
        if count <= 0:
            continue
        try:
            cost = Decimal(str(quote.get("cost")))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if cost.is_finite() and cost > 0:
            available.append(cost)
    if not available:
        raise SmsNoNumbersError(
            f"HeroSMS 当前国家/服务没有可用库存：service={selected.service}, country={selected.country}"
        )
    recommended = (
        min(available) * (Decimal("1") + buffer_percent / Decimal("100"))
    ).quantize(Decimal("0.0001"), rounding=ROUND_CEILING)
    logger.info(
        "[SMS:Hero] 已锁定任务价格上限：最低价=%s, 上浮=%s%%, maxPrice=%s",
        format(min(available), "f"), format(buffer_percent, "f"), format(recommended, ".4f"),
    )
    return replace(
        selected,
        price_mode=mode,
        max_price=format(recommended, ".4f"),
        price_resolved=True,
    )


# ============================================================
# 取号
# ============================================================

def acquire_number(
    http: CurlSession | None = None,
    service: str | None = None,
    country: str | None = None,
    *,
    settings: SmsTaskSettings | None = None,
) -> tuple[str, str]:
    """
    取一个手机号（getNumber）。

    Returns:
        (activation_id, phone_number) —— phone_number 不带 + 前缀（如 16195366483）

    Raises:
        SmsNoNumbersError / SmsNoBalanceError / SmsProviderError
    """
    resume_pending_cancellations()
    candidate = settings or current_settings()
    if service is not None or country is not None:
        candidate = replace(
            candidate,
            service=str(service if service is not None else candidate.service).strip(),
            country=str(country if country is not None else candidate.country).strip(),
            price_resolved=False if candidate.provider == "hero" else candidate.price_resolved,
        )
    selected = prepare_task_settings(candidate, http=http)
    own_http = http is None
    http = http or _http(selected)
    try:
        if selected.provider == "smsbower":
            params, text = _request_smsbower_number(http, _smsbower_number_params(service, country, selected), selected)
            if params["action"] == "getNumberV2":
                try:
                    data = json.loads(text)
                except Exception:
                    data = None
                if isinstance(data, dict):
                    activation_id = str(data.get("activationId") or data.get("id") or "").strip()
                    phone = str(data.get("phoneNumber") or data.get("phone") or "").strip()
                    if activation_id and phone:
                        _remember_acquisition(activation_id, selected)
                        return activation_id, phone
                raise SmsProviderError(f"SMSBower getNumberV2 响应格式异常：{text[:200]}")
            if not text.startswith("ACCESS_NUMBER:"):
                raise SmsProviderError(f"SMSBower getNumber 非预期响应：{text[:200]}")
            parts = text.split(":", 2)
            if len(parts) < 3:
                raise SmsProviderError(f"SMSBower getNumber 响应格式异常：{text[:200]}")
            activation_id, phone = parts[1].strip(), parts[2].strip()
            _remember_acquisition(activation_id, selected)
            return activation_id, phone

        if selected.provider == "l":
            payload = {
                "service": service or selected.service,
                "country": country or selected.country,
            }
            if selected.max_price:
                payload["maxPrice"] = selected.max_price

            data = _post_l_json(http, "/api/admin/l/take-phone", payload, selected)
            item = data.get("item") or {}
            activation_id = str(item.get("id") or "").strip()
            raw_phone = str(item.get("phone") or "")
            raw_prefix = selected.phone_prefix
            phone = _normalize_l_phone(raw_phone, selected)
            if raw_phone.strip() != phone or raw_prefix.strip():
                logger.info(
                    f"[SMS:L] 号码规范化：raw_phone={raw_phone!r}, "
                    f"prefix={raw_prefix!r}, normalized=+{phone}"
                )
            if not activation_id or not phone:
                raise SmsFatalProviderError(f"L take-phone 响应缺少 item.id/item.phone：{str(data)[:200]}")
            _remember_acquisition(activation_id, selected)
            logger.info(f"[SMS:L] 取号成功：id={activation_id}, phone=+{phone}")
            return activation_id, phone

        if selected.provider == "h":
            # H_API 使用 projectId + country；统一复用 SMS_SERVICE / SMS_COUNTRY，
            # 避免接码平台之间出现重复的“服务/国家”配置。
            project_id = str(service or selected.service).strip()
            h_country = str(country or selected.country).strip()
            if not project_id:
                raise SmsFatalProviderError("H projectId 不能为空：请填写 SMS_SERVICE")
            if not h_country:
                raise SmsFatalProviderError("H country 不能为空：请填写 SMS_COUNTRY")
            payload = {
                "projectId": project_id,
                "country": h_country,
            }
            mode = _h_phone_acquire_mode(selected)
            api_path = "/api/admin/h/take-phone" if mode == "new" else "/api/admin/h/take-reusable-phone"
            data = _post_h_json(http, api_path, payload, selected)
            item = data.get("item") or {}
            activation_id = str(item.get("id") or "").strip()
            raw_phone = str(item.get("phone") or "")
            raw_prefix = selected.phone_prefix
            phone = _normalize_h_phone(raw_phone, selected)
            if raw_phone.strip() != phone or raw_prefix.strip():
                logger.info(
                    f"[SMS:H] 号码规范化：raw_phone={raw_phone!r}, "
                    f"prefix={raw_prefix!r}, normalized=+{phone}"
                )
            if not activation_id or not phone:
                raise SmsFatalProviderError(f"H {api_path.rsplit('/', 1)[-1]} 响应缺少 item.id/item.phone：{str(data)[:200]}")
            _remember_acquisition(activation_id, selected)
            logger.info(
                f"[SMS:H] 取号成功：mode={mode}, api={api_path}, id={activation_id}, phone=+{phone}, "
                f"reused={bool(data.get('reused'))}, duplicate={bool(data.get('duplicate'))}"
            )
            return activation_id, phone

        if selected.provider == "hero":
            hero_service = str(service or selected.service).strip()
            hero_country = str(country or selected.country).strip()
            if not hero_service:
                raise SmsFatalProviderError("HERO_SMS_SERVICE 不能为空")
            if not hero_country:
                raise SmsFatalProviderError("HERO_SMS_COUNTRY 不能为空")
            client = _hero_client(selected, http=http)
            activation_id, raw_phone = _hero_call(lambda: client.get_number(
                hero_service,
                hero_country,
                max_price=selected.max_price or None,
                fixed_price=selected.fixed_price,
                operator=selected.operator,
                phone_exception=selected.phone_exception,
            ))
            activation_id = str(activation_id or "").strip()
            phone = _normalize_phone_digits(raw_phone)
            if not activation_id or not phone:
                raise SmsFatalProviderError("HeroSMS getNumber 响应缺少 activation_id/phone")
            _remember_acquisition(activation_id, selected)
            logger.info(
                "[SMS:Hero] 取号成功：id=%s, phone=+%s, service=%s, country=%s",
                activation_id, phone, hero_service, hero_country,
            )
            return activation_id, phone

        params = {
            "action": "getNumber",
            "service": service or selected.service,
            "country": country or selected.country,
        }
        if selected.max_price:
            params["maxPrice"] = selected.max_price

        text = _request_grizzly(http, params, selected)
        # 成功格式：ACCESS_NUMBER:激活ID:号码
        if not text.startswith("ACCESS_NUMBER:"):
            raise SmsFatalProviderError(f"getNumber 非预期响应：{text[:200]}")
        parts = text.split(":")
        if len(parts) < 3:
            raise SmsFatalProviderError(f"getNumber 响应格式异常：{text[:200]}")
        activation_id = parts[1].strip()
        phone = _normalize_phone_digits(parts[2])
        _remember_acquisition(activation_id, selected)
        logger.info(f"[SMS] 取号成功：activation_id={activation_id}, phone=+{phone}")
        return activation_id, phone
    finally:
        if own_http:
            http.close()


# ============================================================
# 取短信验证码
# ============================================================

def wait_for_sms_code(
    activation_id: str,
    http: CurlSession | None = None,
    max_wait: int | None = None,
    poll_interval: int | None = None,
    *,
    settings: SmsTaskSettings | None = None,
) -> str:
    """
    轮询 getStatus 直到拿到短信验证码。

    Returns:
        验证码字符串

    Raises:
        SmsCodeTimeout —— 超时没收到（上层可换号重试）
        SmsProviderError —— 激活被取消等
    """
    selected = _settings_for_activation(activation_id, settings)
    own_http = http is None
    http = http or _http(selected)
    deadline = time.time() + (max_wait if max_wait is not None else selected.code_wait)
    interval = poll_interval if poll_interval is not None else selected.poll_interval
    try:
        provider = selected.provider
        total_wait = max_wait if max_wait is not None else selected.code_wait
        logger.info(f"[SMS] 等待短信验证码 activation_id={activation_id}，最长 {total_wait}s...")
        round_no = 0
        while time.time() < deadline:
            try:
                from core.registration_service import check_stop_requested
                check_stop_requested()
            except ImportError:
                pass
            round_no += 1
            elapsed = max(0, int(total_wait - max(0, deadline - time.time())))
            remaining_before = max(0, int(deadline - time.time()))
            logger.info(
                f"[SMS] 第 {round_no} 轮获取验证码 activation_id={activation_id}，"
                f"已等 {elapsed}s，剩余约 {remaining_before}s"
            )
            if provider == "l":
                data = _post_l_json(http, "/api/admin/l/fetch-code", {"id": activation_id}, selected)
                code = str(data.get("code") or "").strip()
                raw = str(data.get("raw") or "").strip()
                status = str((data.get("item") or {}).get("status") or "").strip()
                if code:
                    logger.info(f"[SMS:L] 第 {round_no} 轮收到验证码：{code}")
                    return code
                remaining = max(0, int(deadline - time.time()))
                logger.info(
                    f"[SMS:L] 第 {round_no} 轮未收到验证码，状态={status or raw or 'WAIT'}，"
                    f"{interval}s 后重试（剩余 {remaining}s）"
                )
                time.sleep(interval)
                continue

            if provider == "h":
                data = _post_h_json(http, "/api/admin/h/fetch-code", {"id": activation_id}, selected)
                code = str(data.get("code") or "").strip()
                raw = str(data.get("raw") or "").strip()
                status = str((data.get("item") or {}).get("status") or "").strip()
                if code:
                    logger.info(f"[SMS:H] 第 {round_no} 轮收到验证码：{code}")
                    return code
                remaining = max(0, int(deadline - time.time()))
                logger.info(
                    f"[SMS:H] 第 {round_no} 轮未收到验证码，状态={status or raw or 'WAIT'}，"
                    f"{interval}s 后重试（剩余 {remaining}s）"
                )
                time.sleep(interval)
                continue

            if provider == "hero":
                client = _hero_client(selected, http=http)
                try:
                    status = _hero_status_text(_hero_call(lambda: client.get_status(activation_id)))
                except SmsRetryableProviderError as exc:
                    remaining = max(0, int(deadline - time.time()))
                    logger.warning(
                        "[SMS:Hero] 第 %s 轮查询临时失败：%s，%ss 后重试（剩余 %ss）",
                        round_no, exc, interval, remaining,
                    )
                    time.sleep(interval)
                    continue
                if status.startswith("STATUS_OK:"):
                    code = status.split(":", 1)[1].strip()
                    if code:
                        logger.info("[SMS:Hero] 第 %s 轮收到验证码：%s", round_no, code)
                        return code
                if status == "STATUS_CANCEL":
                    raise SmsFatalProviderError("HeroSMS 激活已被取消（STATUS_CANCEL）")
                remaining = max(0, int(deadline - time.time()))
                logger.info(
                    "[SMS:Hero] 第 %s 轮未收到验证码，状态=%s，%ss 后重试（剩余 %ss）",
                    round_no, status or "WAIT", interval, remaining,
                )
                time.sleep(interval)
                continue

            if provider == "smsbower":
                text = _request_smsbower(http, {"action": "getStatus", "id": activation_id}, selected)
                if text.startswith("STATUS_OK:"):
                    return text.split(":", 1)[1].strip().strip("'")
                if text == "STATUS_CANCEL":
                    raise SmsProviderError("SMSBower 激活已被取消（STATUS_CANCEL）")
                time.sleep(interval)
                continue

            text = _request_grizzly(http, {"action": "getStatus", "id": activation_id}, selected)

            if text.startswith("STATUS_OK:"):
                code = text.split(":", 1)[1].strip()
                logger.info(f"[SMS] 第 {round_no} 轮收到验证码：{code}")
                return code
            if text == "STATUS_CANCEL":
                raise SmsFatalProviderError("激活已被取消（STATUS_CANCEL）")
            # STATUS_WAIT_CODE / STATUS_WAIT_RETRY:* / STATUS_WAIT_RESEND → 继续等
            remaining = max(0, int(deadline - time.time()))
            logger.info(f"[SMS] 第 {round_no} 轮未收到验证码，状态={text}，{interval}s 后重试（剩余 {remaining}s）")
            time.sleep(interval)

        raise SmsCodeTimeout(f"等待短信超时（>{total_wait}s），activation_id={activation_id}")
    finally:
        if own_http:
            http.close()


# ============================================================
# 改状态
# ============================================================

def set_status(
    activation_id: str,
    status: int,
    http: CurlSession | None = None,
    *,
    settings: SmsTaskSettings | None = None,
) -> str:
    """
    设置激活状态（setStatus）。
        1 = 号码已就绪（短信已发出）
        3 = 等下一条短信（重发）
        6 = 完成激活
        8 = 取消激活
    """
    selected = _settings_for_activation(activation_id, settings)
    own_http = http is None
    http = http or _http(selected)
    try:
        if selected.provider in {"l", "h"}:
            logger.debug(
                "[SMS:%s] 忽略状态设置 id=%s, status=%s",
                selected.provider.upper(), activation_id, status,
            )
            return "OK"
        if selected.provider == "smsbower":
            if int(status) == 1:
                return "OK"
            return _request_smsbower(http, {"action": "setStatus", "status": str(status), "id": activation_id}, selected)
        if selected.provider == "hero":
            if int(status) == 1:
                logger.debug("[SMS:Hero] 忽略 status=1 id=%s", activation_id)
                return "OK"
            if int(status) not in {3, 6, 8}:
                raise SmsFatalProviderError(f"HeroSMS 不支持 status={status}，仅支持 3/6/8")
            result = _hero_call(
                lambda: _hero_client(selected, http=http).set_status(activation_id, int(status))
            )
            return str(result or "OK")
        return _request_grizzly(
            http,
            {"action": "setStatus", "status": str(status), "id": activation_id},
            selected,
        )
    finally:
        if own_http:
            http.close()


def complete(
    activation_id: str,
    http: CurlSession | None = None,
    *,
    settings: SmsTaskSettings | None = None,
) -> None:
    """标记激活完成（status=6）。失败只告警不抛，避免影响主流程。"""
    selected = _settings_for_activation(activation_id, settings)
    if selected.provider == "l":
        logger.info(f"[SMS:L] 已完成 id={activation_id}")
        _forget_acquisition(activation_id, selected)
        return
    if selected.provider == "h":
        # H 成功 fetch-code 后后台会自动按多次收码策略重取；这里不 release。
        logger.info(f"[SMS:H] 已完成 id={activation_id}")
        _forget_acquisition(activation_id, selected)
        return
    if selected.provider == "smsbower":
        try:
            set_status(activation_id, 6, http=http, settings=selected)
        except Exception as exc:
            logger.warning(f"[SMSBower] 标记完成失败（不影响结果）：{exc}")
        finally:
            _forget_acquisition(activation_id, selected)
        return
    try:
        set_status(activation_id, 6, http=http, settings=selected)
        logger.info("[SMS:%s] 已标记完成 activation_id=%s", selected.provider, activation_id)
    except Exception as exc:
        logger.warning(f"[SMS] 标记完成失败（不影响结果）：{exc}")
    finally:
        # OpenAI 已接受验证码后绝不能让进程重启恢复逻辑再取消这个激活；即便
        # 平台 status=6 回执失败，也从待取消 ledger 移除并交由平台自然结算。
        _forget_acquisition(activation_id, selected)


def _do_cancel_sync(
    activation_id: str,
    settings: SmsTaskSettings,
    http_factory=None,
    acquired_at: float | None = None,
) -> None:
    """使用取号时捕获的配置执行延迟取消，失败按 5/15/30 秒重试。"""
    if acquired_at is None:
        with _ACQUISITIONS_LOCK:
            item = _ACQUISITIONS.get((settings.provider, str(activation_id)))
        acquired_at = item.acquired_at if item is not None else time.time()

    elapsed = time.time() - acquired_at
    if elapsed < _MIN_CANCEL_DELAY:
        wait = _MIN_CANCEL_DELAY - elapsed
        logger.info(
            "[SMS:%s] 取消等待平台最短持有时间：activation_id=%s，还需等 %.0fs...",
            settings.provider, activation_id, wait,
        )
        time.sleep(wait)

    if settings.provider == "hero" and time.time() - acquired_at >= _FREE_CANCEL_WINDOW:
        reason = "FREE_CANCELLATION_EXPIRED: 已超过 20 分钟免费取消窗口"
        _ledger_update(settings, activation_id, status="manual_required", last_error=reason)
        logger.warning("[SMS:Hero] %s，activation_id=%s", reason, activation_id)
        return

    # 后台线程不能复用外部 http session（curl_cffi 非线程安全），自己创建。
    factory = http_factory or _http
    try:
        http = factory(settings)
    except TypeError:
        # 兼容旧测试注入的无参工厂。
        http = factory()
    delays = (5, 15, 30)
    try:
        with bind_settings(settings):
            for attempt in range(1, len(delays) + 2):
                _ledger_update(
                    settings,
                    activation_id,
                    status="retrying",
                    attempts=attempt,
                    last_error="",
                )
                try:
                    set_status(activation_id, 8, http=http, settings=settings)
                    logger.info(
                        "[SMS:%s] 已取消 activation_id=%s", settings.provider, activation_id
                    )
                    _forget_acquisition(activation_id, settings)
                    return
                except Exception as exc:
                    text = f"{type(exc).__name__}: {exc}"
                    upper = text.upper()
                    if settings.provider == "hero" and any(code in upper for code in (
                        "OTP_RECEIVED", "NEW_OTP_RECEIVED", "FREE_CANCELLATION_EXPIRED",
                    )):
                        _ledger_update(
                            settings,
                            activation_id,
                            status="manual_required",
                            attempts=attempt,
                            last_error=text[:500],
                        )
                        logger.warning(
                            "[SMS:Hero] 激活已不可自动退款，停止取消重试：id=%s, %s",
                            activation_id, text,
                        )
                        return
                    _ledger_update(
                        settings,
                        activation_id,
                        status="retrying",
                        attempts=attempt,
                        last_error=text[:500],
                    )
                    if attempt <= len(delays):
                        delay = delays[attempt - 1]
                        logger.warning(
                            "[SMS:%s] 取消失败（%s），%ss 后重试...",
                            settings.provider, text, delay,
                        )
                        time.sleep(delay)
                        continue
                    _ledger_update(
                        settings,
                        activation_id,
                        status="manual_required",
                        attempts=attempt,
                        last_error=text[:500],
                    )
                    logger.warning(
                        "[SMS:%s] 取消最终失败，需到平台手动处理：activation_id=%s, %s",
                        settings.provider, activation_id, text,
                    )
    finally:
        try:
            http.close()
        except Exception:
            pass


def cancel(
    activation_id: str,
    http: CurlSession | None = None,
    background: bool = True,
    *,
    settings: SmsTaskSettings | None = None,
) -> None:
    """
    取消激活（status=8），释放号码避免白扣费。

    GrizzlySMS 规则：号码取出后约 2 分钟内不允许取消。本函数默认 background=True，
    把"等 2 分钟+取消"放到后台守护线程里执行，主流程立刻返回继续走（如换下一个号），
    避免被这 2 分钟阻塞。

    background=False 时同步等够时间再返回（少数场景需要确认取消完成时用）。

    失败只告警不抛，不影响主流程。
    """
    selected = _settings_for_activation(activation_id, settings)
    if selected.provider == "l":
        try:
            _release_l_number(activation_id, http=http, settings=selected)
        except Exception as exc:
            logger.warning(f"[SMS:L] 释放号码失败（不影响主流程）：id={activation_id}, {type(exc).__name__}: {exc}")
        return
    if selected.provider == "h":
        try:
            _release_h_number(activation_id, http=http, settings=selected)
        except Exception as exc:
            logger.warning(f"[SMS:H] 释放号码失败（不影响主流程）：id={activation_id}, {type(exc).__name__}: {exc}")
        return
    if selected.provider == "smsbower":
        try:
            set_status(activation_id, 8, http=http, settings=selected)
        except Exception as exc:
            logger.warning(f"[SMSBower] 释放号码失败（不影响主流程）：{exc}")
        finally:
            _forget_acquisition(activation_id, selected)
        return

    if not background:
        _do_cancel_sync(activation_id, selected)
        return

    t = threading.Thread(
        target=_do_cancel_sync,
        args=(activation_id, selected),
        name=f"sms-cancel-{activation_id}",
        daemon=True,
    )
    t.start()
    logger.debug(f"[SMS] 取消任务已派后台：activation_id={activation_id}")
