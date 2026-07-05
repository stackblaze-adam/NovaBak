"""Unit tests for backup guardrails and transport routing helpers."""

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import backup_engine
import vddk_transport


class TestInfraVmExclusion(unittest.TestCase):
    def test_infra_vm_skipped_when_enabled(self):
        config = SimpleNamespace(exclude_infra_vms=True)
        self.assertTrue(backup_engine._is_infra_vm("VMware vCenter Server", config))
        self.assertTrue(backup_engine._is_infra_vm("vCLS-abc", config))

    def test_infra_vm_allowed_when_disabled(self):
        config = SimpleNamespace(exclude_infra_vms=False)
        self.assertFalse(backup_engine._is_infra_vm("VMware vCenter Server", config))


class TestDatastoreMultiplier(unittest.TestCase):
    def test_nbd_uses_1x_for_live(self):
        config = SimpleNamespace(backup_transport="nbd", datastore_est_multiplier=2.0)
        self.assertEqual(backup_engine._effective_datastore_multiplier(config, "poweredOn"), 1.0)

    def test_legacy_uses_config_multiplier(self):
        config = SimpleNamespace(backup_transport="legacy", datastore_est_multiplier=2.0)
        self.assertEqual(backup_engine._effective_datastore_multiplier(config, "poweredOn"), 2.0)

    def test_powered_off_always_1x(self):
        config = SimpleNamespace(backup_transport="legacy", datastore_est_multiplier=2.0)
        self.assertEqual(backup_engine._effective_datastore_multiplier(config, "poweredOff"), 1.0)


class TestRepoCapacity(unittest.TestCase):
    def test_repo_full_skips_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = SimpleNamespace(base_path=tmp, get_base_path=lambda: tmp)
            config = SimpleNamespace(repo_min_free_gb=999999)
            si = MagicMock()
            vm = SimpleNamespace(storage_gb=10, layoutEx=None)
            with patch.object(backup_engine, "_get_vm", return_value=vm):
                ok, msg = backup_engine._check_repo_capacity_for_vm(storage, si, "test-vm", config)
            self.assertFalse(ok)
            self.assertTrue(msg.startswith("[SKIP]"))

    def test_repo_ok_when_enough_space(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = SimpleNamespace(base_path=tmp, get_base_path=lambda: tmp)
            config = SimpleNamespace(repo_min_free_gb=1)
            si = MagicMock()
            vm = SimpleNamespace(storage_gb=1, layoutEx=None)
            with patch.object(backup_engine, "_get_vm", return_value=vm):
                ok, msg = backup_engine._check_repo_capacity_for_vm(storage, si, "test-vm", config)
            self.assertTrue(ok)


class TestVddkAvailability(unittest.TestCase):
    def test_unavailable_without_nbdkit(self):
        config = SimpleNamespace(vddk_libdir="/nonexistent")
        with patch.object(vddk_transport.shutil, "which", return_value=None):
            self.assertFalse(vddk_transport.is_available(config))
            self.assertIn("nbdkit", vddk_transport.availability_message(config))


if __name__ == "__main__":
    unittest.main()
