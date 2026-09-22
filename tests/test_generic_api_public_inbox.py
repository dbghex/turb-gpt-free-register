# -*- coding: utf-8 -*-
import json
import unittest
from unittest.mock import Mock, patch

from core.generic_api_mail_client import (
    GenericApiEmailAccount,
    _public_inbox_latest_code_url,
    _public_inbox_page_api_url,
    _fetch_public_inbox_page_otp,
    GenericApiMailError,
    fetch_latest_otp,
)


class _Response:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload


class _Session:
    def __init__(self):
        self.urls = []
        self.proxies = {}
        self.trust_env = True

    def get(self, url, **_kwargs):
        self.urls.append(url)
        return _Response({
            "mailbox": {"address": "inbox-0141-d678@071898.7bcb28.221wx.com"},
            "messages": [{
                "id": "msg_01",
                "receivedAt": "2026-09-06T12:00:00.000Z",
                "subject": "ChatGPT の一時的な認証コード",
                # 该站点日文邮件 verificationCodes 可能为空，但页面 preview 已有验证码。
                "verificationCodes": [],
                "preview": "この一時検証コードを入力して続行してください: 739201 ChatGPT",
                "fromAddress": "service@example.com",
            }],
        })


class GenericApiPublicInboxTests(unittest.TestCase):
    def test_short_link_polling_reads_nested_korean_preview(self):
        email = "test@icloud.com"
        account = GenericApiEmailAccount(email, "https://mail.example/u/token")
        session = Mock()
        session.get.return_value = _Response({"success": True, "data": {
            "alias": email, "messages": [{"id": "1", "date": "2026-09-20T07:30:00Z",
            "subject": "ChatGPT 임시 인증 코드", "preview": "인증 코드: 037463 ChatGPT"}]}})
        with patch("core.generic_api_mail_client.get_account_context", return_value=account), \
             patch("core.generic_api_mail_client._new_http_session", return_value=session), \
             patch("core.generic_api_mail_client._proxy_cfg.pick_proxy", return_value=""):
            self.assertEqual(fetch_latest_otp(email, settle_seconds=0), "037463")
        self.assertTrue(session.get.call_args.args[0].startswith(
            "https://mail.example/api/public/inbox/token?limit=20&days=7&"))

    def test_short_link_filters_old_mail_and_reads_detail(self):
        url = _public_inbox_page_api_url("https://mail.example/u/token")
        session = Mock()
        session.get.side_effect = [
            _Response({"data": {"alias": "test@icloud.com", "messages": [
                {"id": "old", "date": "2026-09-20T07:27:25Z", "preview": "ChatGPT 111111"},
                {"id": "new", "date": "2026-09-20T07:30:00Z", "preview": ""}]}}),
            _Response({"data": {"text": "ChatGPT 인증 코드: 037463"}}),
        ]
        from datetime import datetime
        after = datetime.fromisoformat("2026-09-20T07:29:55+00:00").timestamp()
        result = _fetch_public_inbox_page_otp(session, url, "test@icloud.com", {}, after)
        self.assertEqual(result[0], "037463")
        self.assertEqual(result[1]["received_at"], "2026-09-20T07:30:00Z")
        self.assertEqual(session.get.call_args.args[0],
                         "https://mail.example/api/public/inbox/token/messages/new")
        session.get.side_effect = None
        session.get.return_value = _Response({"data": {"messages": [
            {"date": "2026-09-20T07:27:25Z", "preview": "ChatGPT 111111"}]}})
        self.assertIsNone(_fetch_public_inbox_page_otp(session, url, "test@icloud.com", {}, after))
        session.get.return_value = _Response({"data": {"alias": "other@icloud.com"}})
        with self.assertRaises(GenericApiMailError):
            _fetch_public_inbox_page_otp(session, url, "test@icloud.com", {})

    def test_public_link_is_converted_to_latest_code_api(self):
        self.assertEqual(
            _public_inbox_latest_code_url("https://mail.knm03.com/i/HTOJyWzFuVXC"),
            "https://mail.knm03.com/api/public/inboxes/HTOJyWzFuVXC/latest-code",
        )

    def test_direct_latest_code_api_is_also_accepted(self):
        url = "https://mail.knm03.com/api/public/inboxes/HTOJyWzFuVXC/latest-code"
        self.assertEqual(_public_inbox_latest_code_url(url), url)

    def test_fetch_latest_otp_uses_public_inbox_api(self):
        email = "inbox-0141-d678@071898.7bcb28.221wx.com"
        account = GenericApiEmailAccount(
            email=email,
            code_url="https://mail.knm03.com/i/HTOJyWzFuVXC",
        )
        session = _Session()
        with patch("core.generic_api_mail_client.get_account_context", return_value=account), \
             patch("core.generic_api_mail_client.requests.Session", return_value=session):
            code = fetch_latest_otp(
                email,
                after_ts=1788690000,
                max_wait=2,
                poll_interval=0.01,
                settle_seconds=0,
            )
        self.assertEqual(code, "739201")
        self.assertEqual(len(session.urls), 1)
        self.assertTrue(session.urls[0].startswith(
            "https://mail.knm03.com/api/public/inboxes/HTOJyWzFuVXC?"
        ))
        self.assertFalse(session.trust_env)

    def test_fetch_latest_otp_applies_proxy_pool_route(self):
        email = "inbox-0141-d678@071898.7bcb28.221wx.com"
        account = GenericApiEmailAccount(email=email, code_url="https://mail.knm03.com/i/token")
        session = _Session()
        proxy = "socks5://user:secret@127.0.0.1:7897"
        with patch("core.generic_api_mail_client.get_account_context", return_value=account), \
             patch("core.generic_api_mail_client._proxy_cfg.pick_proxy", return_value=proxy), \
             patch("core.generic_api_mail_client.requests.Session", return_value=session):
            code = fetch_latest_otp(email, max_wait=2, poll_interval=0.01, settle_seconds=0)
        self.assertEqual(code, "739201")
        self.assertEqual(session.proxies, {"http": proxy, "https": proxy})

    def test_proxy_connection_error_falls_back_to_direct(self):
        email = "inbox-0141-d678@071898.7bcb28.221wx.com"
        account = GenericApiEmailAccount(email=email, code_url="https://mail.knm03.com/i/token")
        proxy_session = _Session()
        direct_session = _Session()

        def proxy_failure(_url, **_kwargs):
            import requests
            raise requests.ConnectionError("proxy reset")

        proxy_session.get = proxy_failure
        with patch("core.generic_api_mail_client.get_account_context", return_value=account), \
             patch("core.generic_api_mail_client._proxy_cfg.pick_proxy", return_value="socks5://127.0.0.1:7897"), \
             patch("core.generic_api_mail_client.requests.Session", side_effect=[proxy_session, direct_session]):
            code = fetch_latest_otp(email, max_wait=2, poll_interval=0.01, settle_seconds=0)
        self.assertEqual(code, "739201")
        self.assertEqual(proxy_session.proxies["https"], "socks5://127.0.0.1:7897")
        self.assertEqual(direct_session.proxies, {})
        self.assertFalse(direct_session.trust_env)


if __name__ == "__main__":
    unittest.main()
