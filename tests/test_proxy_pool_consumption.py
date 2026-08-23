import tempfile
import threading
import unittest
from pathlib import Path

import config.env_loader as env_loader
import config.proxy as proxy_cfg


class ProxyPoolConsumptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = env_loader._ENV_PATH
        self.old_pool = list(proxy_cfg.PROXY_POOL)
        env_loader._ENV_PATH = Path(self.tmp.name) / ".env"
        env_loader._ENV_PATH.write_text('PROXY_POOL="' + "\\n".join(f"host{i}:1000" for i in range(8)) + '"\n', encoding="utf-8")
        proxy_cfg.PROXY_POOL[:] = [f"host{i}:1000" for i in range(8)]

    def tearDown(self):
        env_loader._ENV_PATH = self.old_path
        proxy_cfg.PROXY_POOL[:] = self.old_pool
        self.tmp.cleanup()

    def test_concurrent_claims_are_unique_and_persistent(self):
        claimed, errors = [], []

        def claim_one():
            try:
                claimed.extend(proxy_cfg.take_registration_proxies(1))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=claim_one) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(claimed), 8)
        self.assertEqual(len(set(claimed)), 8)
        self.assertEqual(proxy_cfg.registration_proxy_pool_status()["available"], 0)
        self.assertEqual(env_loader.read_env_file()["PROXY_POOL"], "[]")

    def test_insufficient_pool_does_not_consume(self):
        with self.assertRaises(proxy_cfg.ProxyPoolInsufficientError):
            proxy_cfg.take_registration_proxies(9)
        self.assertEqual(proxy_cfg.registration_proxy_pool_status()["available"], 8)


if __name__ == "__main__":
    unittest.main()
