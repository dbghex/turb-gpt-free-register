# -*- coding: utf-8 -*-
"""HeroSMS SMS-Activate compatible API client.

The compatibility endpoint returns a mixture of plain text and JSON.  This
module keeps that protocol handling in one place so callers can work with
stable Python values and typed errors.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable
from urllib.parse import quote, quote_plus

try:  # curl_cffi is used by the application when installed.
    from curl_cffi.requests import Session as CurlSession
except Exception:  # pragma: no cover - lightweight test/install fallback
    from requests import Session as CurlSession


DEFAULT_API_BASE = "https://hero-sms.com/stubs/handler_api.php"
_ALLOWED_LANGUAGES = {"cn", "de", "en", "es", "fr", "pt", "ru", "id", "vi", "tr"}
_ALLOWED_ACTIVATION_STATUSES = {3, 6, 8}
_MISSING = object()


def redact_api_key(value: Any, api_key: str = "") -> str:
    """Return text safe for logs and user-facing error messages."""
    text = str(value or "")
    secrets = {str(api_key or "").strip()}
    if api_key:
        secrets.update({quote(str(api_key), safe=""), quote_plus(str(api_key))})
    for secret in sorted((item for item in secrets if item), key=len, reverse=True):
        text = text.replace(secret, "***")
    # Also protect a key embedded in an exception URL if it differs from the
    # configured value (for example after a redirect or mock server rewrite).
    text = re.sub(
        r"(?i)(api_key(?:%5B[^%]*%5D)?=)[^&\s'\"<>]+",
        r"\1***",
        text,
    )
    return text


def _redact_data(value: Any, api_key: str) -> Any:
    if isinstance(value, dict):
        return {
            str(key): ("***" if str(key).lower() == "api_key" else _redact_data(item, api_key))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_data(item, api_key) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_data(item, api_key) for item in value)
    if isinstance(value, str):
        return redact_api_key(value, api_key)
    return value


class HeroSmsError(RuntimeError):
    """Base error with fields that are safe to serialize to the Web UI."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "HERO_SMS_ERROR",
        retryable: bool = False,
        details: str = "",
        info: Any = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code or "HERO_SMS_ERROR").upper()
        self.retryable = bool(retryable)
        self.details = str(details or "")
        self.info = info if info is not None else {}
        self.http_status = http_status

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "retryable": self.retryable,
            "details": self.details,
            "info": self.info,
        }


class HeroSmsNoNumbersError(HeroSmsError):
    """No inventory is available; retrying later is useful."""


class HeroSmsNoBalanceError(HeroSmsError):
    """The HeroSMS account has insufficient balance."""


class HeroSmsAuthError(HeroSmsError):
    """The API key is absent or invalid."""


class HeroSmsValidationError(HeroSmsError):
    """The request contains an unsupported or missing parameter."""


class HeroSmsLimitError(HeroSmsError):
    """Purchases are blocked by an account or service restriction."""


class HeroSmsActivationError(HeroSmsError):
    """An activation cannot perform the requested lifecycle operation."""


class HeroSmsNetworkError(HeroSmsError):
    """The request failed before a usable response was received."""


class HeroSmsServerError(HeroSmsError):
    """HeroSMS returned an internal service failure."""


class HeroSmsProtocolError(HeroSmsError):
    """HeroSMS returned an unexpected success payload."""


class HeroSmsWrongMaxPriceError(HeroSmsValidationError):
    """The configured maximum price is below HeroSMS's accepted minimum."""

    def __init__(self, *args: Any, minimum_price: float | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.minimum_price = minimum_price


class HeroSmsPurchaseUnknownError(HeroSmsError):
    """getNumber timed out, so repeating it could purchase a second number."""


# Short aliases make the purchase ambiguity explicit at call sites while the
# prefixed name remains consistent with the rest of the exception hierarchy.
PurchaseUnknown = HeroSmsPurchaseUnknownError
PurchaseUnknownError = HeroSmsPurchaseUnknownError


def _decimal(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise InvalidOperation
    return Decimal(str(value).strip())


def _number(value: Any, *, field: str, positive: bool = False) -> Decimal:
    try:
        result = _decimal(value)
    except (InvalidOperation, ValueError, TypeError):
        raise HeroSmsValidationError(
            f"HeroSMS {field} 必须是数字",
            code="INVALID_PARAMETER",
            details=f"{field} must be numeric",
        ) from None
    if not result.is_finite() or (positive and result <= 0):
        raise HeroSmsValidationError(
            f"HeroSMS {field} 必须大于 0",
            code="INVALID_PARAMETER",
            details=f"{field} must be greater than zero",
        )
    return result


def _query_number(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _timeout_error(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    if "timeout" in exc.__class__.__name__.lower():
        return True
    code = getattr(exc, "code", None)
    if code == 28:  # libcurl CURLE_OPERATION_TIMEDOUT
        return True
    message = str(exc).lower()
    return "timed out" in message or "timeout" in message or "time-out" in message


def _response_payload(response: Any) -> Any:
    text_value = getattr(response, "text", _MISSING)
    if text_value is not _MISSING and text_value is not None:
        if isinstance(text_value, bytes):
            text_value = text_value.decode("utf-8", errors="replace")
        text = str(text_value).strip()
        if text:
            try:
                return json.loads(text)
            except (json.JSONDecodeError, TypeError, ValueError):
                return text

    json_method = getattr(response, "json", None)
    if callable(json_method):
        try:
            return json_method()
        except Exception:
            pass
    return ""


def _clean_code(value: Any) -> str:
    return str(value or "").strip().upper().replace("-", "_").replace(" ", "_")


class HeroSmsClient:
    """Small synchronous client for HeroSMS's compatibility endpoint.

    ``http`` is injectable and only needs a ``get(url, params=..., timeout=...)``
    method, which keeps unit tests and Web API integration independent from a
    concrete HTTP library.
    """

    def __init__(
        self,
        api_base: str,
        api_key: str,
        timeout: float = 30,
        http: Any = None,
        *,
        session: Any = None,
    ) -> None:
        if http is not None and session is not None:
            raise ValueError("http 和 session 不能同时传入")
        self.api_base = str(api_base or DEFAULT_API_BASE).strip()
        self.api_key = str(api_key or "").strip()
        try:
            self.timeout = float(timeout)
        except (TypeError, ValueError):
            raise ValueError("HeroSMS timeout 必须是数字") from None
        if self.timeout <= 0:
            raise ValueError("HeroSMS timeout 必须大于 0")
        self.http = http or session or CurlSession()
        self._owns_http = http is None and session is None

    def close(self) -> None:
        if self._owns_http:
            close = getattr(self.http, "close", None)
            if callable(close):
                close()

    def __enter__(self) -> "HeroSmsClient":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def _request(self, action: str, **params: Any) -> Any:
        if not self.api_base:
            raise HeroSmsValidationError(
                "HeroSMS API 地址不能为空",
                code="INVALID_CONFIG",
                details="api_base is required",
            )
        if not self.api_key:
            raise HeroSmsAuthError(
                "HeroSMS API Key 不能为空",
                code="NO_KEY",
                details="api_key is required",
            )

        query = {"api_key": self.api_key, "action": action}
        query.update({key: value for key, value in params.items() if value is not None and value != ""})
        try:
            response = self.http.get(self.api_base, params=query, timeout=self.timeout)
        except Exception as exc:
            safe_error = redact_api_key(exc, self.api_key)
            if action in {"getNumber", "getNumberV2"} and _timeout_error(exc):
                raise HeroSmsPurchaseUnknownError(
                    "HeroSMS 取号请求超时，购买结果未知；请勿自动重试，以免重复扣费",
                    code="PURCHASE_RESULT_UNKNOWN",
                    retryable=False,
                    details=safe_error,
                ) from None
            raise HeroSmsNetworkError(
                f"HeroSMS 网络请求失败：{safe_error}",
                code="NETWORK_ERROR",
                retryable=True,
                details=safe_error,
            ) from None

        payload = _response_payload(response)
        status_code = getattr(response, "status_code", 200)
        try:
            status_code = int(status_code)
        except (TypeError, ValueError):
            status_code = 200

        self._raise_protocol_error(payload, status_code=status_code)
        if not 200 <= status_code < 300:
            safe_payload = redact_api_key(payload, self.api_key)[:300]
            if status_code == 401:
                raise HeroSmsAuthError(
                    "HeroSMS API Key 无效",
                    code="BAD_KEY",
                    details=safe_payload,
                    http_status=status_code,
                )
            if status_code == 402:
                raise HeroSmsNoBalanceError(
                    "HeroSMS 余额不足，请充值",
                    code="NO_BALANCE",
                    details=safe_payload,
                    http_status=status_code,
                )
            if status_code == 403:
                raise HeroSmsLimitError(
                    "HeroSMS 拒绝了当前请求",
                    code="ACCESS_DENIED",
                    details=safe_payload,
                    http_status=status_code,
                )
            if status_code == 422:
                raise HeroSmsValidationError(
                    "HeroSMS 请求参数无效",
                    code="UNPROCESSABLE_ENTITY",
                    details=safe_payload,
                    http_status=status_code,
                )
            if status_code >= 500:
                raise HeroSmsServerError(
                    f"HeroSMS 服务异常（HTTP {status_code}）",
                    code="SERVER_ERROR",
                    retryable=True,
                    details=safe_payload,
                    http_status=status_code,
                )
            raise HeroSmsError(
                f"HeroSMS HTTP {status_code}: {safe_payload}",
                code="HTTP_ERROR",
                details=safe_payload,
                http_status=status_code,
            )
        return payload

    def _raise_protocol_error(self, payload: Any, *, status_code: int) -> None:
        code = ""
        details = ""
        info: Any = {}
        minimum_price: float | None = None

        if isinstance(payload, dict):
            status = str(payload.get("status", "")).strip().lower()
            title = payload.get("title") or payload.get("error") or payload.get("code")
            if title:
                code = _clean_code(title)
            elif status in {"false", "error", "failed", "failure", "0"}:
                message = payload.get("msg") or payload.get("message") or payload.get("details")
                lowered = str(message or "").lower()
                if "country" in lowered:
                    code = "WRONG_COUNTRY"
                elif "service" in lowered:
                    code = "BAD_SERVICE"
                else:
                    code = "API_ERROR"
            details = str(payload.get("details") or payload.get("msg") or payload.get("message") or "")
            info = payload.get("info") if payload.get("info") is not None else {}
            if code == "WRONG_MAX_PRICE" and isinstance(info, dict):
                try:
                    minimum_price = float(_decimal(info.get("min")))
                except (InvalidOperation, ValueError, TypeError):
                    minimum_price = None
        elif isinstance(payload, str):
            text = payload.strip()
            head, separator, tail = text.partition(":")
            possible_code = _clean_code(head)
            known_prefixes = {
                "BAD_ACTION",
                "BAD_KEY",
                "NO_KEY",
                "NO_BALANCE",
                "NO_NUMBERS",
                "BAD_SERVICE",
                "WRONG_SERVICE",
                "WRONG_COUNTRY",
                "BAD_STATUS",
                "WRONG_ACTIVATION_ID",
                "NO_ACTIVATION",
                "WRONG_MAX_PRICE",
                "BANNED",
                "CHANNELS_LIMIT",
                "SERVICE_NOT_AVAILABLE",
                "SERVICE_UNAVAILABLE_REGION",
                "EARLY_CANCEL_DENIED",
                "OTP_RECEIVED",
                "NEW_OTP_RECEIVED",
                "FREE_CANCELLATION_EXPIRED",
                "ACTIVATION_NOT_ACTIVE",
                "NOT_FOUND",
                "ERROR_SQL",
                "SERVER_ERROR",
                "UNPROCESSABLE_ENTITY",
            }
            if possible_code in known_prefixes:
                code = possible_code
                details = tail.strip() if separator else ""
                if code == "WRONG_MAX_PRICE" and details:
                    try:
                        minimum_price = float(_decimal(details))
                        info = {"min": minimum_price}
                    except (InvalidOperation, ValueError, TypeError):
                        pass
            elif text.lower().startswith("the service is prohibited"):
                code = "SERVICE_NOT_AVAILABLE"
                details = text

        if not code:
            return

        details = redact_api_key(details, self.api_key)
        info = _redact_data(info, self.api_key)
        common = {
            "code": code,
            "details": details,
            "info": info,
            "http_status": status_code,
        }
        if code == "NO_NUMBERS":
            raise HeroSmsNoNumbersError(
                "HeroSMS 暂无可用号码（NO_NUMBERS）",
                retryable=True,
                **common,
            )
        if code == "NO_BALANCE":
            raise HeroSmsNoBalanceError("HeroSMS 余额不足，请充值（NO_BALANCE）", **common)
        if code in {"BAD_KEY", "NO_KEY"}:
            raise HeroSmsAuthError("HeroSMS API Key 缺失或无效", **common)
        if code == "WRONG_MAX_PRICE":
            suffix = f"，平台最低价格为 {minimum_price:g}" if minimum_price is not None else ""
            raise HeroSmsWrongMaxPriceError(
                f"HeroSMS 价格上限低于平台允许值{suffix}",
                minimum_price=minimum_price,
                **common,
            )
        if code in {
            "BAD_ACTION",
            "BAD_SERVICE",
            "WRONG_SERVICE",
            "WRONG_COUNTRY",
            "BAD_STATUS",
            "WRONG_ACTIVATION_ID",
            "NO_ACTIVATION",
            "NOT_FOUND",
            "UNPROCESSABLE_ENTITY",
            "API_ERROR",
        }:
            raise HeroSmsValidationError(f"HeroSMS 请求参数错误：{code}", **common)
        if code in {
            "BANNED",
            "CHANNELS_LIMIT",
            "SERVICE_NOT_AVAILABLE",
            "SERVICE_UNAVAILABLE_REGION",
        }:
            raise HeroSmsLimitError(f"HeroSMS 当前不可购买：{code}", **common)
        if code in {
            "EARLY_CANCEL_DENIED",
            "OTP_RECEIVED",
            "NEW_OTP_RECEIVED",
            "FREE_CANCELLATION_EXPIRED",
            "ACTIVATION_NOT_ACTIVE",
        }:
            raise HeroSmsActivationError(f"HeroSMS 激活状态不允许此操作：{code}", **common)
        if code in {"ERROR_SQL", "SERVER_ERROR"}:
            raise HeroSmsServerError(
                f"HeroSMS 服务异常：{code}",
                retryable=True,
                **common,
            )
        raise HeroSmsError(f"HeroSMS 请求失败：{code}", **common)

    def get_balance(self) -> float:
        payload = self._request("getBalance")
        value: Any = None
        if isinstance(payload, str) and payload.startswith("ACCESS_BALANCE:"):
            value = payload.split(":", 1)[1]
        elif isinstance(payload, dict):
            value = payload.get("balance", payload.get("data"))
            if isinstance(value, dict):
                value = value.get("balance")
        try:
            balance = _decimal(value)
        except (InvalidOperation, ValueError, TypeError):
            raise self._unexpected("getBalance", payload) from None
        if not balance.is_finite():
            raise self._unexpected("getBalance", payload)
        return float(balance)

    def get_countries(self) -> list[dict[str, Any]]:
        payload = self._request("getCountries")
        rows: Any = payload
        if isinstance(payload, dict):
            rows = payload.get("countries", payload.get("data", payload))
            if isinstance(rows, dict):
                mapped = []
                for key, value in rows.items():
                    if not isinstance(value, dict):
                        continue
                    item = dict(value)
                    item.setdefault("id", key)
                    mapped.append(item)
                rows = mapped
        if not isinstance(rows, list):
            raise self._unexpected("getCountries", payload)

        countries: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict) or row.get("id") is None:
                continue
            item = dict(row)
            try:
                item["id"] = int(str(item["id"]).strip())
            except (TypeError, ValueError):
                item["id"] = str(item["id"]).strip()
            for key in ("rus", "eng", "chn"):
                item[key] = str(item.get(key) or "")
            for key in ("visible", "retry"):
                if key in item:
                    try:
                        item[key] = int(item[key])
                    except (TypeError, ValueError):
                        pass
            countries.append(item)
        return countries

    def get_services(self, country: Any = None, lang: str = "cn") -> list[dict[str, str]]:
        lang = str(lang or "cn").strip().lower()
        if lang not in _ALLOWED_LANGUAGES:
            raise HeroSmsValidationError(
                f"HeroSMS 不支持语言：{lang}",
                code="INVALID_PARAMETER",
                details="unsupported lang",
            )
        params: dict[str, Any] = {"lang": lang}
        if country is not None and str(country).strip() != "":
            params["country"] = str(country).strip()
        payload = self._request("getServicesList", **params)
        rows: Any = payload
        if isinstance(payload, dict):
            rows = payload.get("services", payload.get("data", payload))
        services: list[dict[str, str]] = []
        if isinstance(rows, dict):
            for code, value in rows.items():
                if isinstance(value, dict):
                    service_code = value.get("code", code)
                    name = value.get("name", value.get("title", service_code))
                else:
                    service_code, name = code, value
                if str(service_code or "").strip():
                    services.append({"code": str(service_code).strip(), "name": str(name or service_code).strip()})
        elif isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                code = str(row.get("code") or row.get("id") or "").strip()
                if code:
                    services.append({"code": code, "name": str(row.get("name") or row.get("title") or code).strip()})
        else:
            raise self._unexpected("getServicesList", payload)
        services.sort(key=lambda item: (item["name"].casefold(), item["code"].casefold()))
        return services

    def get_prices(self, service: str, country: Any) -> list[dict[str, Any]]:
        service = str(service or "").strip()
        country_text = str(country if country is not None else "").strip()
        if not service or not country_text:
            raise HeroSmsValidationError(
                "HeroSMS 查询价格必须选择服务和国家",
                code="INVALID_PARAMETER",
                details="service and country are required",
            )
        payload = self._request("getPrices", service=service, country=country_text)
        if isinstance(payload, dict) and "prices" in payload:
            payload = payload["prices"]
        quotes = self._normalize_prices(payload, requested_service=service, requested_country=country_text)
        if not quotes:
            raise self._unexpected("getPrices", payload)
        return quotes

    def get_number(
        self,
        service: str,
        country: Any,
        max_price: Any = None,
        fixed_price: bool = False,
        operator: str | Iterable[str] = "",
        phone_exception: str | Iterable[str] = "",
        ref: str = "",
    ) -> tuple[str, str]:
        service = str(service or "").strip()
        country_text = str(country if country is not None else "").strip()
        if not service or not country_text:
            raise HeroSmsValidationError(
                "HeroSMS 取号必须选择服务和国家",
                code="INVALID_PARAMETER",
                details="service and country are required",
            )
        params: dict[str, Any] = {"service": service, "country": country_text}
        if max_price is not None and str(max_price).strip() != "":
            parsed_price = _number(max_price, field="maxPrice", positive=True)
            params["maxPrice"] = _query_number(parsed_price)
            if fixed_price:
                params["fixedPrice"] = "true"
        elif fixed_price:
            raise HeroSmsValidationError(
                "HeroSMS 固定价格模式必须填写价格上限",
                code="INVALID_PARAMETER",
                details="fixedPrice requires maxPrice",
            )

        operator_text = self._csv(operator, field="operator")
        exception_text = self._csv(phone_exception, field="phoneException", maximum=20)
        if operator_text:
            params["operator"] = operator_text
        if exception_text:
            params["phoneException"] = exception_text
        if str(ref or "").strip():
            params["ref"] = str(ref).strip()

        payload = self._request("getNumber", **params)
        activation_id = ""
        phone = ""
        if isinstance(payload, str) and payload.startswith("ACCESS_NUMBER:"):
            parts = payload.split(":", 2)
            if len(parts) == 3:
                activation_id, phone = parts[1], parts[2]
        elif isinstance(payload, dict):
            activation_id = str(payload.get("activationId") or payload.get("activation_id") or payload.get("id") or "")
            phone = str(payload.get("phoneNumber") or payload.get("phone_number") or payload.get("phone") or "")
        activation_id = str(activation_id).strip()
        phone = re.sub(r"\D", "", str(phone))
        if not activation_id or not phone:
            raise self._unexpected("getNumber", payload)
        return activation_id, phone

    def get_number_v2(
        self,
        service: str,
        country: Any,
        max_price: Any = None,
        fixed_price: bool = False,
        operator: str | Iterable[str] = "",
        phone_exception: str | Iterable[str] = "",
        ref: str = "",
        provider_ids: str | Iterable[str] = "",
        *,
        provider_id: str = "",
    ) -> dict[str, Any]:
        """Purchase a number and retain HeroSMS's detailed V2 metadata.

        The existing :meth:`get_number` tuple contract remains unchanged.
        ``provider_id`` is accepted as a compatibility alias for clients based
        on the SMSBower protocol; Hero's wire parameter is ``providerIds``.
        """

        service = str(service or "").strip()
        country_text = str(country if country is not None else "").strip()
        if not service or not country_text:
            raise HeroSmsValidationError(
                "HeroSMS 取号必须选择服务和国家",
                code="INVALID_PARAMETER",
                details="service and country are required",
            )

        params: dict[str, Any] = {"service": service, "country": country_text}
        if max_price is not None and str(max_price).strip() != "":
            parsed_price = _number(max_price, field="maxPrice", positive=True)
            params["maxPrice"] = _query_number(parsed_price)
            if fixed_price:
                params["fixedPrice"] = "true"
        elif fixed_price:
            raise HeroSmsValidationError(
                "HeroSMS 固定价格模式必须填写价格上限",
                code="INVALID_PARAMETER",
                details="fixedPrice requires maxPrice",
            )

        operator_text = self._csv(operator, field="operator")
        exception_text = self._csv(phone_exception, field="phoneException", maximum=20)
        providers_text = self._csv(provider_ids or provider_id, field="providerIds")
        if operator_text:
            params["operator"] = operator_text
        if exception_text:
            params["phoneException"] = exception_text
        if providers_text:
            params["providerIds"] = providers_text
        if str(ref or "").strip():
            params["ref"] = str(ref).strip()

        payload = self._request("getNumberV2", **params)
        node: Any = payload
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            node = payload["data"]

        if isinstance(node, str) and node.startswith("ACCESS_NUMBER:"):
            parts = node.split(":", 2)
            node = {
                "activationId": parts[1] if len(parts) > 1 else "",
                "phoneNumber": parts[2] if len(parts) > 2 else "",
            }
        if not isinstance(node, dict):
            raise self._unexpected("getNumberV2", payload)

        result = dict(node)
        activation_id = str(
            result.get("activationId")
            or result.get("activation_id")
            or result.get("id")
            or ""
        ).strip()
        phone = re.sub(
            r"\D",
            "",
            str(
                result.get("phoneNumber")
                or result.get("phone_number")
                or result.get("phone")
                or ""
            ),
        )
        if not activation_id or not phone:
            raise self._unexpected("getNumberV2", payload)
        result["activationId"] = activation_id
        result["phoneNumber"] = phone

        if "activationCost" in result:
            try:
                result["activationCost"] = float(_decimal(result["activationCost"]))
            except (InvalidOperation, ValueError, TypeError):
                pass
        if "currency" in result:
            result["currency"] = str(result["currency"] or "").strip().upper()
        if "canGetAnotherSms" in result:
            raw_value = result["canGetAnotherSms"]
            if isinstance(raw_value, str):
                result["canGetAnotherSms"] = raw_value.strip().lower() in {
                    "1", "true", "yes", "on",
                }
            else:
                result["canGetAnotherSms"] = bool(raw_value)
        return result

    def get_number_detailed(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Compatibility spelling for :meth:`get_number_v2`."""
        return self.get_number_v2(*args, **kwargs)

    def get_number_details(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Compatibility spelling for :meth:`get_number_v2`."""
        return self.get_number_v2(*args, **kwargs)

    def get_status(self, activation_id: Any) -> str:
        activation_id = self._activation_id(activation_id)
        payload = self._request("getStatus", id=activation_id)
        if isinstance(payload, str) and payload:
            return payload.strip()
        if isinstance(payload, dict):
            status = str(payload.get("status") or payload.get("activationStatus") or "").strip()
            code = payload.get("code")
            if status:
                return f"{status}:{code}" if code not in (None, "") and ":" not in status else status
        raise self._unexpected("getStatus", payload)

    def set_status(self, activation_id: Any, status: Any) -> str:
        activation_id = self._activation_id(activation_id)
        try:
            status_value = int(status)
        except (TypeError, ValueError):
            status_value = -1
        if status_value not in _ALLOWED_ACTIVATION_STATUSES:
            raise HeroSmsValidationError(
                "HeroSMS status 只允许 3、6、8",
                code="BAD_STATUS",
                details="status must be one of 3, 6, 8",
            )
        payload = self._request("setStatus", id=activation_id, status=status_value)
        if isinstance(payload, str) and payload:
            return payload.strip()
        if isinstance(payload, dict):
            result = payload.get("status") or payload.get("result") or payload.get("message")
            if result:
                return str(result).strip()
        raise self._unexpected("setStatus", payload)

    def _activation_id(self, value: Any) -> str:
        activation_id = str(value or "").strip()
        if not activation_id:
            raise HeroSmsValidationError(
                "HeroSMS 激活 ID 不能为空",
                code="WRONG_ACTIVATION_ID",
                details="activation id is required",
            )
        return activation_id

    def _csv(self, value: str | Iterable[str], *, field: str, maximum: int | None = None) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            items = value.split(",")
        else:
            try:
                items = list(value)
            except TypeError:
                items = [value]
        cleaned = [str(item).strip() for item in items if str(item).strip()]
        if maximum is not None and len(cleaned) > maximum:
            raise HeroSmsValidationError(
                f"HeroSMS {field} 最多允许 {maximum} 项",
                code="INVALID_PARAMETER",
                details=f"{field} accepts at most {maximum} values",
            )
        return ",".join(cleaned)

    def _normalize_prices(
        self,
        payload: Any,
        *,
        requested_service: str,
        requested_country: str,
    ) -> list[dict[str, Any]]:
        quotes: list[dict[str, Any]] = []

        def add(value: dict[str, Any], service: Any, country: Any) -> None:
            if "cost" not in value and "price" not in value:
                return
            try:
                cost = float(_decimal(value.get("cost", value.get("price"))))
            except (InvalidOperation, ValueError, TypeError):
                return
            if not Decimal(str(cost)).is_finite():
                return
            try:
                count = int(value.get("count", value.get("quantity", 0)) or 0)
            except (TypeError, ValueError):
                count = 0
            try:
                physical_count = int(value.get("physicalCount", value.get("physical_count", count)) or 0)
            except (TypeError, ValueError):
                physical_count = count
            quotes.append(
                {
                    "country": str(country if country not in (None, "") else requested_country),
                    "service": str(service if service not in (None, "") else requested_service),
                    "cost": cost,
                    "count": count,
                    "physicalCount": physical_count,
                }
            )

        def walk(value: Any, country_hint: Any = None, service_hint: Any = None) -> None:
            if isinstance(value, list):
                for item in value:
                    walk(item, country_hint, service_hint)
                return
            if not isinstance(value, dict):
                return
            if "cost" in value or "price" in value:
                add(value, service_hint, country_hint)
                return
            if "data" in value and len(value) <= 3:
                walk(value["data"], country_hint, service_hint)
                return
            for key, child in value.items():
                if key in {"status", "message", "msg", "meta"}:
                    continue
                if not isinstance(child, (dict, list)):
                    continue
                if isinstance(child, dict) and ("cost" in child or "price" in child):
                    # With both filters Hero's documented example omits the
                    # country level and returns [{service: quote}].
                    if str(key) == requested_service or service_hint is not None:
                        add(child, key if service_hint is None else service_hint, country_hint)
                    elif str(key) == requested_country:
                        add(child, service_hint, key)
                    else:
                        add(child, key, country_hint)
                elif str(key) == requested_country or (country_hint is None and str(key).isdigit()):
                    walk(child, key, service_hint)
                elif str(key) == requested_service or service_hint is None:
                    walk(child, country_hint, key)
                else:
                    walk(child, country_hint, service_hint)

        walk(payload)
        unique: dict[tuple[str, str], dict[str, Any]] = {}
        for quote_row in quotes:
            unique[(quote_row["country"], quote_row["service"])] = quote_row
        return sorted(unique.values(), key=lambda item: (item["country"], item["service"]))

    def _unexpected(self, action: str, payload: Any) -> HeroSmsProtocolError:
        preview = redact_api_key(payload, self.api_key)[:300]
        return HeroSmsProtocolError(
            f"HeroSMS {action} 返回了无法识别的响应：{preview}",
            code="UNEXPECTED_RESPONSE",
            details=preview,
        )


__all__ = [
    "DEFAULT_API_BASE",
    "HeroSmsClient",
    "HeroSmsError",
    "HeroSmsNoNumbersError",
    "HeroSmsNoBalanceError",
    "HeroSmsAuthError",
    "HeroSmsValidationError",
    "HeroSmsLimitError",
    "HeroSmsActivationError",
    "HeroSmsNetworkError",
    "HeroSmsServerError",
    "HeroSmsProtocolError",
    "HeroSmsWrongMaxPriceError",
    "HeroSmsPurchaseUnknownError",
    "PurchaseUnknown",
    "PurchaseUnknownError",
    "redact_api_key",
]
