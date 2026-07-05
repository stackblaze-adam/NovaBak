"""
backup_manifest.py — CBT backup chain and point manifest I/O.
"""

import datetime
import json
import os
import uuid

MANIFEST_VERSION = 1
CHAIN_VERSION = 1
CHAIN_DIR = "_chain"
POINTS_SUBDIR = "points"


def chain_root_rel(vm_name):
    return f"{vm_name}/{CHAIN_DIR}"


def chain_json_rel(vm_name):
    return f"{chain_root_rel(vm_name)}/chain.json"


def point_rel(vm_name, point_id):
    return f"{chain_root_rel(vm_name)}/{POINTS_SUBDIR}/{point_id}"


def manifest_rel(vm_name, point_id):
    return f"{point_rel(vm_name, point_id)}/manifest.json"


def new_point_id():
    return datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")


def new_chain_id():
    return str(uuid.uuid4())


def load_json_storage(storage, rel_path):
    if not storage.exists(rel_path):
        return None
    with storage.open_read(rel_path) as f:
        raw = f.read()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw)


def save_json_storage(storage, rel_path, data):
    storage.makedirs(os.path.dirname(rel_path))
    payload = json.dumps(data, indent=2, sort_keys=True)
    with storage.open_write(rel_path) as f:
        if isinstance(payload, str):
            try:
                f.write(payload)
            except TypeError:
                f.write(payload.encode("utf-8"))
        else:
            f.write(payload)


def load_chain(storage, vm_name):
    return load_json_storage(storage, chain_json_rel(vm_name))


def save_chain(storage, vm_name, chain):
    save_json_storage(storage, chain_json_rel(vm_name), chain)


def load_manifest(storage, vm_name, point_id):
    return load_json_storage(storage, manifest_rel(vm_name, point_id))


def save_manifest(storage, vm_name, point_id, manifest):
    save_json_storage(storage, manifest_rel(vm_name, point_id), manifest)


def create_empty_chain(vm_name):
    return {
        "version": CHAIN_VERSION,
        "vm_name": vm_name,
        "chain_id": new_chain_id(),
        "points": [],
        "latest": None,
    }


def add_point_to_chain(chain, point_entry):
    chain["points"].append(point_entry)
    chain["latest"] = point_entry["id"]
    return chain


def get_point_entry(chain, point_id):
    for p in chain.get("points", []):
        if p["id"] == point_id:
            return p
    return None


def get_latest_point(chain):
    if not chain or not chain.get("latest"):
        return None
    return get_point_entry(chain, chain["latest"])


def walk_chain_to_point(chain, point_id):
    """Return ordered list of point entries from first full to point_id."""
    by_id = {p["id"]: p for p in chain.get("points", [])}
    if point_id not in by_id:
        return []
    path = []
    cur = by_id[point_id]
    while cur:
        path.append(cur)
        parent = cur.get("parent")
        cur = by_id.get(parent) if parent else None
    path.reverse()
    return path


def count_incrementals_since_full(chain):
    """Count incremental points since the latest full/synthetic_full in chain."""
    if not chain:
        return 0
    count = 0
    for p in reversed(chain.get("points", [])):
        if p["type"] in ("full", "synthetic_full"):
            break
        if p["type"] == "incremental":
            count += 1
    return count


def build_manifest(point_id, backup_type, parent_id, disks, vmx_file=None, synthetic_from=None):
    return {
        "version": MANIFEST_VERSION,
        "point_id": point_id,
        "type": backup_type,
        "parent_id": parent_id,
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "disks": disks,
        "vmx_file": vmx_file,
        "synthetic_from": synthetic_from,
    }


def build_disk_entry(
    device_key,
    descriptor_basename,
    flat_basename,
    capacity_bytes,
    change_id,
    file_type,
    delta_file=None,
):
    entry = {
        "device_key": device_key,
        "descriptor_basename": descriptor_basename,
        "flat_basename": flat_basename,
        "capacity_bytes": capacity_bytes,
        "change_id": change_id,
        "file_type": file_type,
    }
    if delta_file:
        entry["delta_file"] = delta_file
    return entry
