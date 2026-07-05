"""
delta_store.py — NovaBak incremental delta file format (NVBD1)

Binary layout:
  magic     4 bytes  b'NVBD'
  version   uint16   1
  num_ext   uint32
  repeat num_ext times:
    offset  uint64
    length  uint32
    data    length bytes
"""

import io
import struct

MAGIC = b"NVBD"
VERSION = 1
HEADER_FMT = "<4sHI"  # magic, version, num_extents
EXTENT_HDR_FMT = "<QI"  # offset, length
HEADER_SIZE = struct.calcsize(HEADER_FMT)
EXTENT_HDR_SIZE = struct.calcsize(EXTENT_HDR_FMT)


class DeltaFormatError(Exception):
    pass


def write_delta_file(path_or_file, extents):
    """
    Write delta file from list of (offset, data_bytes) tuples.
    path_or_file may be a filesystem path or a file-like object.
    """
    if isinstance(path_or_file, (str, bytes)):
        with open(path_or_file, "wb") as f:
            _write_delta_stream(f, extents)
    else:
        _write_delta_stream(path_or_file, extents)


def _write_delta_stream(f, extents):
    f.write(struct.pack(HEADER_FMT, MAGIC, VERSION, len(extents)))
    for offset, data in extents:
        if not data:
            continue
        f.write(struct.pack(EXTENT_HDR_FMT, int(offset), len(data)))
        f.write(data)


def read_delta_file(path_or_file):
    """Return list of (offset, data_bytes) from a delta file."""
    if isinstance(path_or_file, (str, bytes)):
        with open(path_or_file, "rb") as f:
            return _read_delta_stream(f)
    return _read_delta_stream(path_or_file)


def _read_delta_stream(f):
    header = f.read(HEADER_SIZE)
    if len(header) < HEADER_SIZE:
        raise DeltaFormatError("Truncated delta header")
    magic, version, num_ext = struct.unpack(HEADER_FMT, header)
    if magic != MAGIC:
        raise DeltaFormatError(f"Invalid delta magic: {magic!r}")
    if version != VERSION:
        raise DeltaFormatError(f"Unsupported delta version: {version}")

    extents = []
    for _ in range(num_ext):
        hdr = f.read(EXTENT_HDR_SIZE)
        if len(hdr) < EXTENT_HDR_SIZE:
            raise DeltaFormatError("Truncated extent header")
        offset, length = struct.unpack(EXTENT_HDR_FMT, hdr)
        data = f.read(length)
        if len(data) < length:
            raise DeltaFormatError("Truncated extent data")
        extents.append((offset, data))
    return extents


def apply_extents_to_file(f, extents, capacity_bytes=None):
    """Apply delta extents to an open file (must support seek/write)."""
    for offset, data in extents:
        f.seek(offset)
        f.write(data)
    if capacity_bytes is not None:
        f.seek(0, io.SEEK_END)
        end = f.tell()
        if end < capacity_bytes:
            f.seek(capacity_bytes - 1)
            f.write(b"\x00")


def merge_extents(extent_lists):
    """Merge multiple delta extent lists in order (later overwrites earlier at same offset)."""
    merged = {}
    order = []
    for extents in extent_lists:
        for offset, data in extents:
            if offset not in merged:
                order.append(offset)
            merged[offset] = data
    return [(off, merged[off]) for off in sorted(order)]
