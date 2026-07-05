"""
nfc_transport.py — Live VM backup via Snapshot ExportSnapshot + HttpNfcLease

Streams snapshot-backed disks over HTTPS (VMware NFC / streamVmdk) without
CopyVirtualDisk temp folders on ESXi. No VDDK required.

Flow: snapshot → ExportSnapshot → stream device URLs → remove snapshot
"""

import os
import threading
import time

import requests
from pyVmomi import vim

from logger_util import log_info, log_warn, log_error
import vsphere_context

CHUNK_SIZE = 1024 * 1024


class _LeaseProgressUpdater(threading.Thread):
    """Keeps HttpNfcLease alive by reporting progress periodically."""

    def __init__(self, lease, interval_sec=15):
        super().__init__(daemon=True)
        self._lease = lease
        self._interval = interval_sec
        self._running = True
        self.progress_pct = 0

    def set_progress(self, pct):
        self.progress_pct = min(max(int(pct), 0), 99)

    def stop(self):
        self._running = False

    def run(self):
        while self._running:
            try:
                state = self._lease.state
                if state in (vim.HttpNfcLease.State.done, vim.HttpNfcLease.State.error):
                    return
                self._lease.HttpNfcLeaseProgress(self.progress_pct)
            except Exception as e:
                log_warn(f"[NFC] Lease progress update: {e}")
                return
            time.sleep(self._interval)


def _nfc_headers():
    return {"Accept": "application/x-vnd.vmware-streamVmdk"}


def _wait_lease_ready(lease, timeout_sec=600, is_cancelled_func=None):
    """Block until lease is ready, done, or error."""
    start = time.time()
    while True:
        if is_cancelled_func and is_cancelled_func():
            raise RuntimeError("Backup cancelled by user")
        state = lease.state
        if state == vim.HttpNfcLease.State.ready:
            return
        if state == vim.HttpNfcLease.State.done:
            return
        if state == vim.HttpNfcLease.State.error:
            err = getattr(lease.info, "error", None) or lease.state
            raise RuntimeError(f"HttpNfcLease error: {err}")
        if time.time() - start > timeout_sec:
            raise RuntimeError(f"HttpNfcLease timeout after {timeout_sec}s (state={state})")
        time.sleep(2)


def _stream_url_to_storage(url, storage, dest_rel_path, cookies, total_size_hint=0,
                           progress_callback=None, progress_base=0, progress_total=100,
                           speed_callback=None, is_cancelled_func=None, lease_updater=None):
    """Stream an NFC device URL into StorageProvider."""
    log_info(f"[NFC] Streaming {url[:80]}... → {dest_rel_path}")
    resp = requests.get(
        url, stream=True, cookies=cookies, headers=_nfc_headers(), verify=False, timeout=7200,
    )
    if resp.status_code != 200:
        body = (resp.text or "")[:300]
        raise RuntimeError(f"NFC HTTP {resp.status_code}: {body}")

    total_size = int(resp.headers.get("Content-Length", 0)) or total_size_hint
    bytes_written = 0
    speed_window_bytes = 0
    speed_window_start = time.time()

    storage.makedirs(os.path.dirname(dest_rel_path) or dest_rel_path)

    with storage.open_write(dest_rel_path) as f:
        is_local = hasattr(storage, "base_path")
        zero_chunk = b"\x00" * CHUNK_SIZE if is_local else None

        for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
            if is_cancelled_func and is_cancelled_func():
                raise RuntimeError("Backup cancelled by user")
            if not chunk:
                continue

            if is_local and len(chunk) == CHUNK_SIZE and chunk == zero_chunk and hasattr(f, "seek"):
                f.seek(CHUNK_SIZE, os.SEEK_CUR)
            elif is_local and not chunk.strip(b"\x00") and hasattr(f, "seek"):
                f.seek(len(chunk), os.SEEK_CUR)
            else:
                f.write(chunk)

            bytes_written += len(chunk)
            speed_window_bytes += len(chunk)

            now = time.time()
            if now - speed_window_start >= 2.0:
                if speed_callback and speed_window_bytes:
                    mbps = (speed_window_bytes / (1024 * 1024)) / (now - speed_window_start)
                    speed_callback(round(mbps, 1))
                speed_window_bytes = 0
                speed_window_start = now

            if total_size > 0 and progress_callback:
                pct = progress_base + (bytes_written * progress_total / total_size)
                progress_callback(min(int(pct), 99))
            if lease_updater and total_size > 0:
                lease_updater.set_progress(
                    progress_base + (bytes_written * progress_total / max(total_size, 1))
                )

        if is_local and bytes_written > 0 and hasattr(f, "truncate"):
            try:
                f.truncate(bytes_written)
            except Exception:
                pass

    log_info(f"[NFC] Wrote {bytes_written / (1024 * 1024):.1f} MB → {dest_rel_path}")
    return bytes_written


def _find_snapshot_obj(si, vm_name, snap_name):
    vm = vsphere_context.find_vm_by_name(si, vm_name)
    if not vm or not vm.snapshot:
        return None

    def walk(tree):
        for s in tree:
            if s.name == snap_name:
                return s.snapshot
            found = walk(s.childSnapshotList)
            if found:
                return found
        return None

    return walk(vm.snapshot.rootSnapshotList)


def _write_minimal_descriptor(storage, desc_rel, flat_filename, size_bytes, adapter="lsilogic"):
    """Create a minimal VMDK descriptor pointing at the flat file."""
    sectors = max(size_bytes // 512, 1)
    content = (
        "# Disk DescriptorFile\n"
        "version=1\n"
        "encoding=\"UTF-8\"\n"
        f"CID=fffffffe\n"
        f"parentCID=ffffffff\n"
        "createType=\"vmfsFlat\"\n"
        "\n"
        f"RW {sectors} VMFS \"{flat_filename}\"\n"
        "\n"
        f"ddb.adapterType = \"{adapter}\"\n"
    )
    storage.makedirs(os.path.dirname(desc_rel) or desc_rel)
    with storage.open_write(desc_rel) as f:
        f.write(content.encode("utf-8"))


def export_live_nfc(
    si,
    vm_name,
    storage,
    dest_rel_dir,
    disk_descriptors,
    vmx_ds_name,
    vmx_rel_path,
    progress_callback=None,
    speed_callback=None,
    is_cancelled_func=None,
    create_snapshot_func=None,
    remove_snapshot_func=None,
    download_http_func=None,
    connection_type=vsphere_context.CONN_AUTO,
):
    """
    Live VM backup: snapshot → ExportSnapshot → NFC stream (no ESXi temp copy).
    Requires vCenter; standalone ESXi should use VDDK or staged stream instead.
    """
    from backup_engine import _get_session_cookies

    vm = vsphere_context.find_vm_by_name(si, vm_name)
    snap_name = None
    lease = None
    updater = None

    try:
        if progress_callback:
            progress_callback(2)

        snap_obj, snap_name = create_snapshot_func(si, vm_name)
        if not snap_obj or snap_obj is True:
            snap_obj = _find_snapshot_obj(si, vm_name, snap_name)
        if not snap_obj:
            return False, f"Snapshot creation failed: {snap_name}"

        if progress_callback:
            progress_callback(5)

        log_info(f"[NFC] ExportSnapshot for {vm_name} ({snap_name})...")
        lease = snap_obj.ExportSnapshot()

        _wait_lease_ready(lease, timeout_sec=900, is_cancelled_func=is_cancelled_func)
        if lease.state != vim.HttpNfcLease.State.ready:
            return False, f"ExportSnapshot lease not ready: {lease.state}"

        updater = _LeaseProgressUpdater(lease, interval_sec=15)
        updater.start()

        cookies = _get_session_cookies(si)
        storage.makedirs(dest_rel_dir)
        files_written = []

        device_urls = list(getattr(lease.info, "deviceUrl", None) or [])
        if not device_urls:
            raise RuntimeError("ExportSnapshot returned no device URLs")

        total_hint = sum(getattr(d, "fileSize", 0) or 0 for d in device_urls) or 1
        bytes_done = 0
        n_devices = len([d for d in device_urls if getattr(d, "targetId", None)])

        disk_idx = 0
        for device in device_urls:
            target_id = getattr(device, "targetId", None)
            url = getattr(device, "url", None)
            if not target_id or not url:
                log_warn(f"[NFC] Skipping device without targetId/url: {device}")
                continue

            if is_cancelled_func and is_cancelled_func():
                return False, "Backup cancelled by user"

            # Map stream export to NovaBak flat + descriptor naming
            if disk_idx < len(disk_descriptors):
                disk_base = os.path.basename(disk_descriptors[disk_idx]["rel_path"])
            else:
                disk_base = target_id if target_id.endswith(".vmdk") else f"{target_id}.vmdk"

            flat_name = disk_base.replace(".vmdk", "-flat.vmdk")
            flat_rel = f"{dest_rel_dir}/{flat_name}"
            desc_rel = f"{dest_rel_dir}/{disk_base}"

            step_base = 5 + (85 * disk_idx // max(n_devices, 1))
            step_end = 5 + (85 * (disk_idx + 1) // max(n_devices, 1))
            file_size = getattr(device, "fileSize", 0) or 0

            nbytes = _stream_url_to_storage(
                url, storage, flat_rel, cookies,
                total_size_hint=file_size,
                progress_callback=progress_callback,
                progress_base=step_base,
                progress_total=max(step_end - step_base, 1),
                speed_callback=speed_callback,
                is_cancelled_func=is_cancelled_func,
                lease_updater=updater,
            )
            bytes_done += nbytes
            files_written.append(flat_name)

            # Prefer HTTP descriptor from snapshot folder; else synthesize
            desc_ok = False
            if disk_idx < len(disk_descriptors) and download_http_func:
                disk = disk_descriptors[disk_idx]
                try:
                    download_http_func(
                        si, disk["ds_name"], disk["rel_path"], storage, desc_rel,
                        is_cancelled_func=is_cancelled_func,
                        vm=vm,
                        connection_type=connection_type,
                    )
                    desc_ok = True
                    files_written.append(disk_base)
                except Exception as e:
                    log_warn(f"[NFC] Descriptor HTTP fallback for {disk_base}: {e}")

            if not desc_ok:
                _write_minimal_descriptor(storage, desc_rel, flat_name, nbytes)
                files_written.append(disk_base)

            disk_idx += 1

        if progress_callback:
            progress_callback(93)

        updater.set_progress(100)
        try:
            lease.HttpNfcLeaseProgress(100)
            lease.HttpNfcLeaseComplete()
        except Exception as e:
            log_warn(f"[NFC] Lease complete: {e}")
        updater.stop()
        snap_name_for_cleanup = snap_name
        snap_name = None

        if remove_snapshot_func and snap_name_for_cleanup:
            remove_snapshot_func(si, vm_name, snap_name_for_cleanup, timeout_mins=60)

        if progress_callback:
            progress_callback(96)
        if vmx_ds_name and vmx_rel_path and download_http_func:
            vmx_filename = os.path.basename(vmx_rel_path)
            try:
                download_http_func(
                    si, vmx_ds_name, vmx_rel_path, storage, f"{dest_rel_dir}/{vmx_filename}",
                    is_cancelled_func=is_cancelled_func,
                    vm=vm,
                    connection_type=connection_type,
                )
                files_written.append(vmx_filename)
            except Exception as e:
                log_warn(f"[NFC] VMX download warning: {e}")

        if progress_callback:
            progress_callback(100)
        return True, f"Backup completed [nfc]: {len(files_written)} file(s) saved to storage"

    except Exception as e:
        if is_cancelled_func and is_cancelled_func():
            return False, "Backup cancelled by user"
        log_error(f"[NFC] Live backup failed: {e}")
        return False, str(e)
    finally:
        if updater:
            updater.stop()
        if lease:
            try:
                if lease.state not in (vim.HttpNfcLease.State.done, vim.HttpNfcLease.State.error):
                    lease.HttpNfcLeaseAbort()
            except Exception:
                pass
        if snap_name and remove_snapshot_func:
            try:
                remove_snapshot_func(si, vm_name, snap_name, timeout_mins=30)
            except Exception as ce:
                log_error(f"[NFC] Snapshot cleanup error: {ce}")
