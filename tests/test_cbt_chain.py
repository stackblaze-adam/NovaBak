import os
import tempfile
import unittest

import backup_manifest as bm
from chain_restore import materialize_chain_point


class LocalStorageStub:
    def __init__(self, root):
        self.base_path = root

    def get_base_path(self):
        return self.base_path

    def exists(self, path):
        return os.path.exists(os.path.join(self.base_path, path))

    def makedirs(self, path):
        os.makedirs(os.path.join(self.base_path, path), exist_ok=True)

    def open_write(self, path):
        full = os.path.join(self.base_path, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        return open(full, "wb")

    def open_read(self, path):
        return open(os.path.join(self.base_path, path), "rb")

    def list_files(self, path):
        full = os.path.join(self.base_path, path)
        if not os.path.isdir(full):
            return []
        return sorted(os.listdir(full))

    def delete_dir(self, path):
        import shutil
        full = os.path.join(self.base_path, path)
        if os.path.isdir(full):
            shutil.rmtree(full)


class TestCbtChain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.storage = LocalStorageStub(self.tmp)
        self.vm = "testvm"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_delta(self, rel, extents):
        from delta_store import write_delta_file
        full = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        write_delta_file(full, extents)

    def test_materialize_full_plus_incremental(self):
        full_id = "20260701-120000"
        inc_id = "20260702-120000"
        full_rel = bm.point_rel(self.vm, full_id)
        inc_rel = bm.point_rel(self.vm, inc_id)

        # Full point
        self.storage.makedirs(full_rel)
        flat = "disk-flat.vmdk"
        full_path = os.path.join(self.tmp, full_rel, flat)
        with open(full_path, "wb") as f:
            f.write(b"\x00" * 512)
        with open(os.path.join(self.tmp, full_rel, "disk.vmdk"), "w") as f:
            f.write("# descriptor")
        bm.save_manifest(self.storage, self.vm, full_id, bm.build_manifest(
            full_id, "full", None,
            [bm.build_disk_entry(1000, "disk.vmdk", flat, 512, "cid-full", "full")],
            vmx_file="test.vmx",
        ))
        with open(os.path.join(self.tmp, full_rel, "test.vmx"), "w") as f:
            f.write("config")

        # Incremental point
        self.storage.makedirs(inc_rel)
        self._write_delta(f"{inc_rel}/{flat}.delta.nvbd", [(0, b"INCR")])
        with open(os.path.join(self.tmp, inc_rel, "disk.vmdk"), "w") as f:
            f.write("# descriptor")
        bm.save_manifest(self.storage, self.vm, inc_id, bm.build_manifest(
            inc_id, "incremental", full_id,
            [bm.build_disk_entry(1000, "disk.vmdk", flat, 512, "cid-inc", "incremental",
                                 delta_file=f"{flat}.delta.nvbd")],
            vmx_file="test.vmx",
        ))

        chain = bm.create_empty_chain(self.vm)
        chain = bm.add_point_to_chain(chain, {"id": full_id, "type": "full", "parent": None})
        chain = bm.add_point_to_chain(chain, {"id": inc_id, "type": "incremental", "parent": full_id})
        bm.save_chain(self.storage, self.vm, chain)

        out = materialize_chain_point(self.storage, inc_rel)
        mat_flat = os.path.join(self.tmp, out, flat)
        self.assertTrue(os.path.isfile(mat_flat))
        with open(mat_flat, "rb") as f:
            self.assertEqual(f.read(4), b"INCR")

    def test_count_incrementals(self):
        chain = bm.create_empty_chain(self.vm)
        chain = bm.add_point_to_chain(chain, {"id": "f1", "type": "full", "parent": None})
        chain = bm.add_point_to_chain(chain, {"id": "i1", "type": "incremental", "parent": "f1"})
        chain = bm.add_point_to_chain(chain, {"id": "i2", "type": "incremental", "parent": "i1"})
        self.assertEqual(bm.count_incrementals_since_full(chain), 2)


    def test_synthetic_full_resets_chain(self):
        from chain_restore import create_synthetic_full
        full_id = "20260701-120000"
        inc_id = "20260702-120000"
        full_rel = bm.point_rel(self.vm, full_id)
        inc_rel = bm.point_rel(self.vm, inc_id)
        flat = "disk-flat.vmdk"

        self.storage.makedirs(full_rel)
        with open(os.path.join(self.tmp, full_rel, flat), "wb") as f:
            f.write(b"\x00" * 512)
        bm.save_manifest(self.storage, self.vm, full_id, bm.build_manifest(
            full_id, "full", None,
            [bm.build_disk_entry(1000, "disk.vmdk", flat, 512, "cid-full", "full")],
        ))
        self.storage.makedirs(inc_rel)
        self._write_delta(f"{inc_rel}/{flat}.delta.nvbd", [(0, b"INCR")])
        bm.save_manifest(self.storage, self.vm, inc_id, bm.build_manifest(
            inc_id, "incremental", full_id,
            [bm.build_disk_entry(1000, "disk.vmdk", flat, 512, "cid-inc", "incremental",
                                 delta_file=f"{flat}.delta.nvbd")],
        ))
        chain = bm.create_empty_chain(self.vm)
        chain = bm.add_point_to_chain(chain, {"id": full_id, "type": "full", "parent": None})
        chain = bm.add_point_to_chain(chain, {"id": inc_id, "type": "incremental", "parent": full_id})
        bm.save_chain(self.storage, self.vm, chain)

        new_chain = create_synthetic_full(self.storage, self.vm, chain)
        self.assertEqual(len(new_chain["points"]), 1)
        self.assertEqual(new_chain["points"][0]["type"], "synthetic_full")
        self.assertFalse(self.storage.exists(inc_rel))


if __name__ == "__main__":
    unittest.main()
