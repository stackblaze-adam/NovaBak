import os
import tempfile
import unittest

from delta_store import (
    DeltaFormatError,
    apply_extents_to_file,
    merge_extents,
    read_delta_file,
    write_delta_file,
)


class TestDeltaStore(unittest.TestCase):
    def test_roundtrip(self):
        extents = [(0, b"aaa"), (4096, b"bbbb"), (8192, b"c")]
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            path = tmp.name
        try:
            write_delta_file(path, extents)
            loaded = read_delta_file(path)
            self.assertEqual(loaded, extents)
        finally:
            os.unlink(path)

    def test_apply_extents(self):
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            path = tmp.name
        try:
            with open(path, "wb") as f:
                f.seek(8191)
                f.write(b"\x00")
            write_delta_file(path + ".delta", [(0, b"XX"), (100, b"Y")])
            extents = read_delta_file(path + ".delta")
            with open(path, "r+b") as f:
                apply_extents_to_file(f, extents)
            with open(path, "rb") as f:
                data = f.read(101)
            self.assertEqual(data[0:2], b"XX")
            self.assertEqual(data[100:101], b"Y")
        finally:
            for p in (path, path + ".delta"):
                if os.path.exists(p):
                    os.unlink(p)

    def test_invalid_magic(self):
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(b"BAD\x00\x01\x00\x00\x00\x00")
            path = tmp.name
        try:
            with self.assertRaises(DeltaFormatError):
                read_delta_file(path)
        finally:
            os.unlink(path)

    def test_merge_extents(self):
        a = [(0, b"aaa"), (100, b"bbb")]
        b = [(50, b"XX"), (100, b"ccc")]
        merged = merge_extents([a, b])
        self.assertEqual(dict(merged)[0], b"aaa")
        self.assertEqual(dict(merged)[50], b"XX")
        self.assertEqual(dict(merged)[100], b"ccc")


if __name__ == "__main__":
    unittest.main()
