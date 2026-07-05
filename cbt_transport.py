"""
cbt_transport.py — CBT incremental backup orchestration (Phases 1–4).
"""

import datetime
import os
import shutil
import tempfile

import backup_manifest as bm
import cbt_core
import delta_store
import vsphere_context
from logger_util import log_info, log_warn, log_error


def cbt_supported_storage(storage):
    base = storage.get_base_path() if storage else ""
    return not str(base).startswith("s3://")


def plan_backup_dest(vm_name, use_cbt):
    if use_cbt:
        point_id = bm.new_point_id()
        return bm.point_rel(vm_name, point_id), point_id
    date_str = datetime.date.today().strftime("%Y-%m-%d")
    return f"{vm_name}/{date_str}", None


def export_cbt_backup(
    si,
    vm_name,
    storage,
    config,
    vm_record,
    host_ip,
    host_user,
    host_password,
    progress_callback=None,
    speed_callback=None,
    is_cancelled_func=None,
    connection_type=vsphere_context.CONN_AUTO,
    create_snapshot_func=None,
    remove_snapshot_func=None,
    download_http_func=None,
    download_http_range_func=None,
    action_callback=None,
):
    """
    Run a CBT-aware backup (full or incremental) into the chain layout.
    Returns (ok, message, dest_rel_dir).
    """
    from backup_engine import _find_snapshot_by_name, _get_vm

    if not cbt_supported_storage(storage):
        return False, "CBT requires local or NFS backup storage", None

    ok, msg = cbt_core.enable_cbt(si, vm_name)
    if not ok:
        log_warn(f"[CBT] {msg}; continuing without CBT enable")

    chain = bm.load_chain(storage, vm_name) or bm.create_empty_chain(vm_name)
    take_full, reason = cbt_core.should_take_full_backup(config, vm_record, chain, storage)
    backup_type = "full" if take_full else "incremental"
    log_info(f"[CBT] Backup type={backup_type} ({reason})")
    if action_callback:
        action_callback(f"CBT {backup_type} backup...")

    dest_rel_dir, point_id = plan_backup_dest(vm_name, use_cbt=True)
    parent_id = None if take_full else (chain.get("latest") if chain.get("points") else None)
    if backup_type == "incremental" and not parent_id:
        backup_type = "full"
        take_full = True
        log_info("[CBT] No parent for incremental; forcing full")

    vm = _get_vm(si, vm_name)
    if not vm:
        return False, f"VM {vm_name} not found", None

    disks = cbt_core.collect_cbt_disks(vm)
    if not disks:
        return False, f"No disks found for {vm_name}", None

    is_off = getattr(vm.runtime, "powerState", "poweredOff") == "poweredOff"
    snap_obj = None
    snap_name = None

    try:
        if progress_callback:
            progress_callback(2)

        if not is_off:
            if not create_snapshot_func:
                return False, "Snapshot function required for live CBT backup", None
            snap_obj, snap_name = create_snapshot_func(si, vm_name)
            if not snap_obj:
                return False, f"Snapshot failed: {snap_name}", None
            if snap_name and not getattr(snap_obj, "_moId", None):
                vm_ref = _get_vm(si, vm_name)
                resolved = _find_snapshot_by_name(vm_ref, snap_name)
                if resolved:
                    snap_obj = resolved

        storage.makedirs(dest_rel_dir)
        disk_keys = [d["device_key"] for d in disks]
        change_ids = cbt_core.get_snapshot_change_ids(snap_obj, disk_keys) if snap_obj else {}

        if backup_type == "incremental" and snap_obj:
            prev_manifest = bm.load_manifest(storage, vm_name, parent_id)
            if not prev_manifest:
                backup_type = "full"
                take_full = True
                parent_id = None

        disk_manifest_entries = []
        total_disks = len(disks)

        for idx, disk in enumerate(disks):
            if is_cancelled_func and is_cancelled_func():
                return False, "Backup cancelled by user", None

            disk_basename = os.path.basename(disk["rel_path"])
            flat_basename = disk_basename.replace(".vmdk", "-flat.vmdk")
            desc_rel = f"{dest_rel_dir}/{disk_basename}"
            flat_rel = f"{dest_rel_dir}/{flat_basename}"
            step_base = 5 + (85 * idx // max(total_disks, 1))
            step_end = 5 + (85 * (idx + 1) // max(total_disks, 1))

            if download_http_func:
                download_http_func(
                    si, disk["ds_name"], disk["rel_path"], storage, desc_rel,
                    progress_callback=progress_callback,
                    progress_base=step_base,
                    progress_total=2,
                    speed_callback=speed_callback,
                    is_cancelled_func=is_cancelled_func,
                    vm=vm,
                    connection_type=connection_type,
                )

            device_key = disk["device_key"]
            capacity = disk["capacity_bytes"]
            change_id = change_ids.get(device_key)

            if take_full or is_off:
                log_info(f"[CBT] Full disk {idx + 1}/{total_disks}: {flat_basename}")
                if is_off:
                    flat_ds_path = disk["rel_path"].replace(".vmdk", "-flat.vmdk")
                    download_http_func(
                        si, disk["ds_name"], flat_ds_path, storage, flat_rel,
                        progress_callback=progress_callback,
                        progress_base=step_base + 2,
                        progress_total=max(step_end - step_base - 2, 1),
                        speed_callback=speed_callback,
                        is_cancelled_func=is_cancelled_func,
                        vm=vm,
                        connection_type=connection_type,
                    )
                else:
                    import vddk_transport
                    vddk_transport.stream_snapshot_disk(
                        si, vm, snap_obj, disk,
                        host_ip, host_user, host_password,
                        storage, flat_rel, config=config,
                        connection_type=connection_type,
                        is_cancelled_func=is_cancelled_func,
                        progress_callback=progress_callback,
                        progress_base=step_base + 2,
                        progress_total=max(step_end - step_base - 2, 1),
                        speed_callback=speed_callback,
                    )
                disk_manifest_entries.append(bm.build_disk_entry(
                    device_key, disk_basename, flat_basename, capacity,
                    change_id, "full",
                ))
            else:
                prev_disk = _find_prev_disk_entry(storage, vm_name, parent_id, device_key)
                prev_change_id = prev_disk.get("change_id") if prev_disk else "*"
                areas = cbt_core.query_changed_areas(
                    si, snap_obj, device_key, prev_change_id, capacity,
                )
                log_info(
                    f"[CBT] Incremental disk {idx + 1}/{total_disks}: "
                    f"{len(areas)} changed area(s), {flat_basename}"
                )
                delta_name = f"{flat_basename}.delta.nvbd"
                delta_rel = f"{dest_rel_dir}/{delta_name}"
                extents = _capture_changed_extents(
                    si, vm, snap_obj, disk, areas, is_off,
                    host_ip, host_user, host_password, storage, config,
                    connection_type, download_http_range_func,
                    is_cancelled_func,
                )
                _write_delta_storage(storage, delta_rel, extents)
                disk_manifest_entries.append(bm.build_disk_entry(
                    device_key, disk_basename, flat_basename, capacity,
                    change_id, "incremental", delta_file=delta_name,
                ))

        if progress_callback:
            progress_callback(93)

        if remove_snapshot_func and snap_name:
            remove_snapshot_func(si, vm_name, snap_name, timeout_mins=60)
            snap_name = None

        vmx_file = None
        from backup_engine import _collect_vm_disk_layout
        _, _, vmx_ds, vmx_rel = _collect_vm_disk_layout(vm)
        if vmx_ds and vmx_rel and download_http_func:
            vmx_file = os.path.basename(vmx_rel)
            try:
                download_http_func(
                    si, vmx_ds, vmx_rel, storage, f"{dest_rel_dir}/{vmx_file}",
                    is_cancelled_func=is_cancelled_func, vm=vm,
                    connection_type=connection_type,
                )
            except Exception as e:
                log_warn(f"[CBT] VMX download warning: {e}")
                vmx_file = None

        manifest = bm.build_manifest(
            point_id, backup_type, parent_id, disk_manifest_entries, vmx_file=vmx_file,
        )
        bm.save_manifest(storage, vm_name, point_id, manifest)

        import datetime as dt
        chain = bm.add_point_to_chain(chain, {
            "id": point_id,
            "type": backup_type,
            "parent": parent_id,
            "timestamp": dt.datetime.utcnow().isoformat() + "Z",
        })
        bm.save_chain(storage, vm_name, chain)

        if progress_callback:
            progress_callback(100)

        return True, f"CBT {backup_type} backup completed: {dest_rel_dir}", dest_rel_dir

    except Exception as e:
        if remove_snapshot_func and snap_name:
            try:
                remove_snapshot_func(si, vm_name, snap_name, timeout_mins=30)
            except Exception:
                pass
        log_error(f"[CBT] Backup failed: {e}")
        return False, str(e), None


def _find_prev_disk_entry(storage, vm_name, parent_id, device_key):
    man = bm.load_manifest(storage, vm_name, parent_id)
    if not man:
        return None
    return next((d for d in man["disks"] if d["device_key"] == device_key), None)


def _write_delta_storage(storage, delta_rel, extents):
    local = _resolve_local(storage, delta_rel)
    if local:
        os.makedirs(os.path.dirname(local) or ".", exist_ok=True)
        delta_store.write_delta_file(local, extents)
        return
    import io
    buf = io.BytesIO()
    delta_store.write_delta_file(buf, extents)
    with storage.open_write(delta_rel) as f:
        f.write(buf.getvalue())


def _resolve_local(storage, rel_path):
    base = storage.get_base_path()
    if base.startswith("s3://"):
        return None
    if hasattr(storage, "base_path"):
        return os.path.join(storage.base_path, rel_path)
    return None


def _capture_changed_extents(
    si, vm, snap_obj, disk, areas, is_off,
    host_ip, host_user, host_password, storage, config,
    connection_type, download_http_range_func, is_cancelled_func,
):
    """Read changed block ranges and return list of (offset, data) for delta file."""
    extents = []
    for start, length in areas:
        if is_cancelled_func and is_cancelled_func():
            raise RuntimeError("Backup cancelled by user")
        if length <= 0:
            continue
        if is_off:
            data = _read_range_http(
                si, disk, start, length, download_http_range_func, vm, connection_type,
            )
        else:
            data = _read_range_nbd(
                si, vm, snap_obj, disk, start, length,
                host_ip, host_user, host_password, config, connection_type,
            )
        extents.append((start, data))
    return extents


def _read_range_http(si, disk, start, length, download_http_range_func, vm, connection_type):
    if not download_http_range_func:
        from backup_engine import _download_file_http_range
        download_http_range_func = _download_file_http_range
    flat_rel_path = disk["rel_path"].replace(".vmdk", "-flat.vmdk")
    return download_http_range_func(
        si, disk["ds_name"], flat_rel_path, start, length, vm=vm,
        connection_type=connection_type,
    )


def _read_range_nbd(
    si, vm, snap_obj, disk, start, length,
    host_ip, host_user, host_password, config, connection_type,
):
    import vddk_transport
    return vddk_transport.read_snapshot_extent(
        si, vm, snap_obj, disk, start, length,
        host_ip, host_user, host_password, config, connection_type,
    )
