# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from webui import config_editor


class PlanCheckProxyConfigTests(unittest.TestCase):
    def test_dedicated_proxy_is_returned_as_plain_editable_value(self):
        proxy = "http://user:password@proxy.example:1000?country=JP"
        # get_config imports these functions locally, so patch their provider.
        with patch("config.env_loader.read_env_file", return_value={"PLAN_CHECK_PROXY": proxy}), \
             patch("config.env_loader.load_env"):
            field = next(x for x in config_editor.get_config() if x["key"] == "PLAN_CHECK_PROXY")

        self.assertEqual(field["value"], proxy)
        self.assertFalse(field.get("secret", False))
        self.assertFalse(field.get("write_only", False))


if __name__ == "__main__":
    unittest.main()
