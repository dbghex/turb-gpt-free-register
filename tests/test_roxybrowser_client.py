# -*- coding: utf-8 -*-
import json
import unittest
from unittest.mock import patch

from core import roxybrowser_client


class _Response:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self.payload


class _Http:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def request(self, *_args, **_kwargs):
        self.calls += 1
        value = self.responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class RoxyBrowserClientRetryTests(unittest.TestCase):
    def _client(self, responses):
        client = roxybrowser_client.RoxyBrowserClient(
            api_base="http://127.0.0.1:50000",
            token="",
        )
        client.http = _Http(responses)
        return client

    def test_create_retries_explicit_pre_tls_failure(self):
        client = self._client([
            _Response({
                "code": 500,
                "msg": "Client network socket disconnected before secure TLS connection was established",
            }),
            _Response({"code": 0, "data": {"dirId": "profile-1"}}),
        ])

        with patch.object(roxybrowser_client._cfg, "ROXY_API_RETRIES", 3), \
             patch.object(roxybrowser_client.time, "sleep"):
            result = client.request("POST", "/browser/create", json_body={"workspaceId": "1"})

        self.assertEqual(result["data"]["dirId"], "profile-1")
        self.assertEqual(client.http.calls, 2)

    def test_create_does_not_retry_client_timeout_with_unknown_result(self):
        client = self._client([TimeoutError("request timed out")])

        with patch.object(roxybrowser_client._cfg, "ROXY_API_RETRIES", 3), \
             patch.object(roxybrowser_client.time, "sleep"):
            with self.assertRaises(TimeoutError):
                client.request("POST", "/browser/create", json_body={"workspaceId": "1"})

        self.assertEqual(client.http.calls, 1)

    def test_create_does_not_retry_business_error(self):
        client = self._client([
            _Response({"code": 400, "msg": "workspaceId is invalid"}),
        ])

        with patch.object(roxybrowser_client._cfg, "ROXY_API_RETRIES", 3), \
             patch.object(roxybrowser_client.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "workspaceId is invalid"):
                client.request("POST", "/browser/create", json_body={"workspaceId": "bad"})

        self.assertEqual(client.http.calls, 1)


if __name__ == "__main__":
    unittest.main()
