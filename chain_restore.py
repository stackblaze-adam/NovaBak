"""
chain_restore.py — Materialize CBT backup chains for restore.
"""

import json
import os
import shutil

from delta_store import apply_extents_to_file, read_delta_file
from backup_manifest import (
    load_chain,
    load_manifest,
    point_rel,
    walk_chain_to_point,
)
from logger_util import log_info


def is_chain_point(storage, source_rel_dir):
    manifest_path = f"{source_rel_dir.rstrip('/')}/manifest.json"
    return storage.exists(manifest_path)


def resolve_restore_source(storage, source_rel_dir):
    """Materialize chain points; return (rel_dir_for_import, temp_dir_or_none)."""
    if not is_chain_point(storage, source_rel_dir):
        return source_rel_dir, None
    out_rel = materialize_chain_point(storage, source_rel_dir)
    return out_rel, out_rel


def materialize_chain_point(storage, point_rel_dir):
    """Build flat restore folder from full + delta chain. Returns output rel path."""
    manifest_path = f"{point_rel_dir.rstrip('/')}/manifest.json"
    manifest = _load_json(storage, manifest_path)
    if not manifest:
        raise RuntimeError(f"No manifest at {point_rel_dir}")

    vm_name = point_rel_dir.split("/")[0]
    point_id = manifest["point_id"]
    chain = load_chain(storage, vm_name)
    point_path = walk_chain_to_point(chain, point_id) if chain else []

    if not point_path:
        point_path = [{
            "id": point_id,
            "type": manifest["type"],
            "parent": manifest.get("parent_id"),
        }]

    out_rel = f"{point_rel_dir.rstrip('/')}/_materialized"
    storage.makedirs(out_rel)

    for disk in manifest["disks"]:
        device_key = disk["device_key"]
        flat_name = disk["flat_basename"]
        desc_name = disk["descriptor_basename"]
        capacity = disk["capacity_bytes"]
        out_flat = f"{out_rel}/{flat_name}"

        _materialize_disk(storage, vm_name, point_path, device_key, flat_name, capacity, out_flat)

        for pt in reversed(point_path):
            pt_dir = point_rel(vm_name, pt["id"])
            desc_src = f"{pt_dir}/{desc_name}"
            if storage.exists(desc_src):
                _copy_storage_file(storage, desc_src, f"{out_rel}/{desc_name}")
                break

    vmx_name = manifest.get("vmx_file")
    if vmx_name:
        for pt in reversed(point_path):
            pt_dir = point_rel(vm_name, pt["id"])
            vmx_src = f"{pt_dir}/{vmx_name}"
            if storage.exists(vmx_src):
                _copy_storage_file(storage, vmx_src, f"{out_rel}/{vmx_name}")
                break

    log_info(f"[CBT] Materialized chain point {point_id} → {out_rel}")
    return out_rel


def _materialize_disk(storage, vm_name, point_path, device_key, flat_name, capacity, out_flat):
    base_local = _resolve_local_path(storage, out_flat)
    if not base_local:
        raise RuntimeError("Chain materialize requires local/NFS storage")

    os.makedirs(os.path.dirname(base_local) or ".", exist_ok=True)
    with open(base_local, "wb") as out_f:
        if capacity > 0:
            out_f.seek(capacity - 1)
            out_f.write(b"\x00")

    for pt in point_path:
        pt_dir = point_rel(vm_name, pt["id"])
        man = _load_json(storage, f"{pt_dir}/manifest.json")
        if not man:
            continue
        disk = next((d for d in man["disks"] if d["device_key"] == device_key), None)
        if not disk:
            continue

        if disk["file_type"] in ("full", "synthetic_full"):
            full_src = f"{pt_dir}/{flat_name}"
            if storage.exists(full_src):
                _copy_flat_full(storage, full_src, base_local)
        elif disk["file_type"] == "incremental":
            delta_file = disk.get("delta_file") or f"{flat_name}.delta.nvbd"
            delta_src = f"{pt_dir}/{delta_file}"
            if storage.exists(delta_src):
                _apply_delta(storage, delta_src, base_local, capacity)


def _copy_flat_full(storage, src_rel, dest_local):
    src_local = _resolve_local_path(storage, src_rel)
    if src_local and os.path.isfile(src_local):
        shutil.copy2(src_local, dest_local)
        return
    with storage.open_read(src_rel) as src, open(dest_local, "wb") as dst:
        shutil.copyfileobj(src, dst)


def _apply_delta(storage, delta_rel, dest_local, capacity):
    delta_local = _resolve_local_path(storage, delta_rel)
    if delta_local and os.path.isfile(delta_local):
        extents = read_delta_file(delta_local)
    else:
        with storage.open_read(delta_rel) as f:
            extents = read_delta_file(f)
    with open(dest_local, "r+b") as out_f:
        apply_extents_to_file(out_f, extents, capacity)


def _copy_storage_file(storage, src_rel, dst_rel):
    dst_local = _resolve_local_path(storage, dst_rel)
    src_local = _resolve_local_path(storage, src_rel)
    if dst_local and src_local:
        os.makedirs(os.path.dirname(dst_local) or ".", exist_ok=True)
        shutil.copy2(src_local, dst_local)
        return
    with storage.open_read(src_rel) as src, storage.open_write(dst_rel) as dst:
        shutil.copyfileobj(src, dst)


def _resolve_local_path(storage, rel_path):
    base = storage.get_base_path()
    if base.startswith("s3://"):
        return None
    if hasattr(storage, "base_path"):
        return os.path.join(storage.base_path, rel_path)
    if os.path.isabs(rel_path):
        return rel_path
    if os.path.isdir(base.rstrip("/")):
        return os.path.join(base.rstrip("/"), rel_path)
    return None


def _load_json(storage, rel_path):
    if not storage.exists(rel_path):
        return None
    with storage.open_read(rel_path) as f:
        raw = f.read()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw)


def create_synthetic_full(storage, vm_name, chain):
    """Merge latest chain into a new synthetic full point and trim old points."""
    from backup_manifest import (
        build_disk_entry,
        build_manifest,
        new_point_id,
        point_rel,
        save_chain,
        save_manifest,
    )
    import datetime

    if not chain or not chain.get("latest"):
        return chain

    latest_id = chain["latest"]
    latest_rel = point_rel(vm_name, latest_id)
    latest_manifest = _load_json(storage, f"{latest_rel}/manifest.json")
    if not latest_manifest:
        return chain

    mat_rel = materialize_chain_point(storage, latest_rel)
    new_id = new_point_id()
    new_rel = point_rel(vm_name, new_id)
    storage.makedirs(new_rel)

    for fn in storage.list_files(mat_rel):
        _copy_storage_file(storage, f"{mat_rel}/{fn}", f"{new_rel}/{fn}")

    new_disks = []
    for d in latest_manifest["disks"]:
        new_disks.append(build_disk_entry(
            d["device_key"],
            d["descriptor_basename"],
            d["flat_basename"],
            d["capacity_bytes"],
            d.get("change_id"),
            "synthetic_full",
        ))

    manifest = build_manifest(
        new_id,
        "synthetic_full",
        None,
        new_disks,
        vmx_file=latest_manifest.get("vmx_file"),
        synthetic_from=latest_id,
    )
    save_manifest(storage, vm_name, new_id, manifest)

    for pt in chain.get("points", []):
        if pt["id"] != new_id:
            old_rel = point_rel(vm_name, pt["id"])
            if storage.exists(old_rel):
                storage.delete_dir(old_rel)

    new_chain = {
        "version": chain.get("version", 1),
        "vm_name": vm_name,
        "chain_id": chain.get("chain_id"),
        "points": [{
            "id": new_id,
            "type": "synthetic_full",
            "parent": None,
            "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        }],
        "latest": new_id,
    }
    save_chain(storage, vm_name, new_chain)
    log_info(f"[CBT] Synthetic full created: {new_id} (from {latest_id})")
    return new_chain


def apply_chain_retention(storage, vm_name, chain, retention_count, full_interval):
    """Optionally rotate to synthetic full, then prune old chain points."""
    from backup_manifest import point_rel, save_chain
    from backup_manifest import count_incrementals_since_full

    if not chain or not chain.get("points"):
        return chain

    if count_incrementals_since_full(chain) >= full_interval:
        chain = create_synthetic_full(storage, vm_name, chain)

    points = chain.get("points", [])
    if retention_count < 1:
        retention_count = 1
    if len(points) <= retention_count:
        return chain

    to_remove = points[: len(points) - retention_count]
    for pt in to_remove:
        rel = point_rel(vm_name, pt["id"])
        if storage.exists(rel):
            log_info(f"[CBT] Retention: removing point {pt['id']}")
            storage.delete_dir(rel)

    chain["points"] = points[-retention_count:]
    chain["latest"] = chain["points"][-1]["id"]
    save_chain(storage, vm_name, chain)
    return chain
