import unittest

from config.proxy import normalize_proxy_url
from core.roxybrowser_client import _mask_proxy, _proxy_url_to_roxy_info


class ProxyFormatTests(unittest.TestCase):
    def test_normalizes_authenticated_proxy_without_scheme(self):
        value = "user:pass@sp.ipipbright.net:1000"
        self.assertEqual(normalize_proxy_url(value), "http://user:pass@sp.ipipbright.net:1000")

    def test_keeps_explicit_scheme(self):
        value = "socks5h://user:pass@sp.ipipbright.net:1000"
        self.assertEqual(normalize_proxy_url(value), value)

    def test_supports_legacy_host_port_user_password(self):
        value = "sp.ipipbright.net:1000:user:pass"
        self.assertEqual(normalize_proxy_url(value), "http://user:pass@sp.ipipbright.net:1000")

    def test_roxy_info_accepts_scheme_less_authenticated_proxy(self):
        info = _proxy_url_to_roxy_info("user:pass@sp.ipipbright.net:1000")
        self.assertEqual(info["protocol"], "HTTP")
        self.assertEqual(info["host"], "sp.ipipbright.net")
        self.assertEqual(info["port"], "1000")
        self.assertEqual(info["proxyUserName"], "user")
        self.assertEqual(info["proxyPassword"], "pass")

    def test_roxy_mask_hides_credentials_without_scheme(self):
        self.assertEqual(_mask_proxy("user:pass@sp.ipipbright.net:1000"), "http://***:***@sp.ipipbright.net:1000")


if __name__ == "__main__":
    unittest.main()
