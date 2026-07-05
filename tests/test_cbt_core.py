import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

import cbt_core


class TestCbtCore(unittest.TestCase):
    def test_should_take_full_no_chain(self):
        config = SimpleNamespace(cbt_enabled=True, cbt_full_interval=7)
        vm = SimpleNamespace(cbt_enabled=True)
        take, reason = cbt_core.should_take_full_backup(config, vm, None, MagicMock(get_base_path=lambda: "/mnt/backups"))
        self.assertTrue(take)
        self.assertIn("No existing chain", reason)

    def test_should_take_incremental(self):
        config = SimpleNamespace(cbt_enabled=True, cbt_full_interval=7)
        vm = SimpleNamespace(cbt_enabled=True)
        chain = {
            "points": [{"id": "f1", "type": "full", "parent": None}],
            "latest": "f1",
        }
        storage = MagicMock(get_base_path=lambda: "/mnt/backups")
        take, reason = cbt_core.should_take_full_backup(config, vm, chain, storage)
        self.assertFalse(take)

    def test_should_take_full_on_s3(self):
        config = SimpleNamespace(cbt_enabled=True, cbt_full_interval=7)
        vm = SimpleNamespace(cbt_enabled=True)
        chain = {"points": [{"id": "f1", "type": "full"}], "latest": "f1"}
        storage = MagicMock(get_base_path=lambda: "s3://bucket")
        take, _ = cbt_core.should_take_full_backup(config, vm, chain, storage)
        self.assertTrue(take)


if __name__ == "__main__":
    unittest.main()
