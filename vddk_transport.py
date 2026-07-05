"""
vddk_transport.py — VDDK/NBD live backup transport

Streams snapshot-backed virtual disks over NBD using nbdkit-vddk-plugin.
Works against standalone ESXi and vCenter (see vsphere_context.py).

See: https://libguestfs.org/nbdkit-vddk-plugin.1.html
"""

import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import time

from logger_util import log_info, log_warn, log_error
import vsphere_context

# Cached ESXi/vCenter SSL thumbprints for the process lifetime
_thumbprint_cache = {}

SNAPSHOT_SETTLE_SECS = 3


class VddkNotAvailableError(Exception):
    """Raised when VDDK/NBD dependencies are missing or misconfigured."""


def get_vddk_libdir(config):
    from services.vddk_install import get_vddk_libdir as _libdir
    return _libdir(config)


def is_available(config=None):
    """Return True if nbdkit-vddk plugin and VDDK library are present."""
    if shutil.which("nbdkit") is None:
        return False
    if not _nbdkit_vddk_plugin_present():
        return False
    libdir = get_vddk_libdir(config)
    for sub in ("lib64", "lib32"):
        if os.path.isfile(os.path.join(libdir, sub, "libvixDiskLib.so")):
            return True
    return False


def _nbdkit_vddk_plugin_present():
    import glob as _glob
    patterns = [
        "/usr/lib/x86_64-linux-gnu/nbdkit/plugins/nbdkit-vddk-plugin.so",
        "/usr/lib/*/nbdkit/plugins/nbdkit-vddk-plugin.so",
        "/usr/local/lib/nbdkit/plugins/nbdkit-vddk-plugin.so",
    ]
    for pat in patterns:
        if _glob.glob(pat):
            return True
    return False


def availability_message(config=None):
    """Human-readable reason when is_available() is False."""
    if shutil.which("nbdkit") is None:
        return "nbdkit not found in PATH"
    if not _nbdkit_vddk_plugin_present():
        return "nbdkit-vddk-plugin not installed (rebuild worker image)"
    libdir = get_vddk_libdir(config)
    if not is_vddk_lib_installed(libdir):
        return f"VDDK library missing under {libdir} (add ESXi host to auto-install from vendor/vddk/)"
    return "unknown"


def is_vddk_lib_installed(libdir):
    for sub in ("lib64", "lib32"):
        if os.path.isfile(os.path.join(libdir, sub, "libvixDiskLib.so")):
            return True
    return False


def ensure_vddk_runtime_dirs():
    """VDDK creates cache dirs under /tmp/vmware-root; ensure writable."""
    for path in ("/tmp/vmware-root", os.path.expanduser("~/.vmware")):
        try:
            os.makedirs(path, mode=0o1777, exist_ok=True)
        except OSError:
            pass


def get_server_thumbprint(host, port=443):
    """Fetch and cache the ESXi/vCenter SSL certificate thumbprint."""
    cache_key = f"{host}:{port}"
    if cache_key in _thumbprint_cache:
        return _thumbprint_cache[cache_key]

    try:
        with socket.create_connection((host, port), timeout=15) as sock:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                der = ssock.getpeercert(binary_form=True)
    except OSError as e:
        raise VddkNotAvailableError(f"Cannot reach {host}:{port} for thumbprint: {e}") from e

    import hashlib
    digest = hashlib.sha1(der).hexdigest().upper()
    thumbprint = ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))
    _thumbprint_cache[cache_key] = thumbprint
    return thumbprint


def _stream_disk_via_nbdcopy(cmd_prefix, dest_path, timeout_secs=7200):
    """Run nbdkit with nbdcopy to write a flat disk image to dest_path."""
    nbdcopy = shutil.which("nbdcopy")
    if not nbdcopy:
        raise VddkNotAvailableError("nbdcopy not found in PATH (install libnbd-bin)")

    os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
    run_cmd = cmd_prefix + ["--run", f'{nbdcopy} "$uri" "{dest_path}"']
    log_info(f"[NBD] Streaming disk → {dest_path}")
    proc = subprocess.run(
        run_cmd,
        capture_output=True,
        text=True,
        timeout=timeout_secs,
    )
    if proc.returncode != 0:
        stderr = (proc.stderr or proc.stdout or "").strip()[:2000]
        raise RuntimeError(f"nbdcopy failed (exit {proc.returncode}): {stderr}")
    return os.path.getsize(dest_path) if os.path.isfile(dest_path) else 0


def _resolve_local_dest(storage, dest_rel_path):
    """NBD writes via nbdcopy to a local path; resolve from StorageProvider."""
    base = storage.get_base_path()
    if base.startswith("s3://"):
        return None, "NBD transport currently requires local or NFS backup storage (not S3)"
    if hasattr(storage, "base_path"):
        full = os.path.join(storage.base_path, dest_rel_path)
        return full, None
    if os.path.isabs(dest_rel_path):
        return dest_rel_path, None
    if os.path.isdir(base.rstrip("/")):
        return os.path.join(base.rstrip("/"), dest_rel_path), None
    return None, f"Cannot resolve local path for storage base: {base}"


def stream_snapshot_disk(
    si,
    vm,
    snap_obj,
    disk,
    server_host,
    host_user,
    host_password,
    storage,
    dest_rel_path,
    config=None,
    connection_type=vsphere_context.CONN_AUTO,
    is_cancelled_func=None,
    progress_callback=None,
    progress_base=0,
    progress_total=100,
    speed_callback=None,
):
    """
    Stream one snapshot-backed disk descriptor to storage via NBD/VDDK.
    Tries multiple disk open paths (active delta vs base) before failing.
    """
    if not is_available(config):
        raise VddkNotAvailableError(availability_message(config))

    ensure_vddk_runtime_dirs()
    dest_path, err = _resolve_local_dest(storage, dest_rel_path)
    if err:
        raise VddkNotAvailableError(err)

    libdir = get_vddk_libdir(config)
    thumbprint = get_server_thumbprint(server_host)
    conn_type = vsphere_context.resolve_connection_type(si, connection_type)
    candidates = vsphere_context.vddk_disk_open_candidates(disk, conn_type)

    with tempfile.NamedTemporaryFile(mode="w", delete=False, prefix="vddk_pw_") as pw_file:
        pw_file.write(host_password)
        pw_path = pw_file.name

    last_err = None
    try:
        for disk_ds_path in candidates:
            if is_cancelled_func and is_cancelled_func():
                raise RuntimeError("Backup cancelled by user")
            cmd, _ = vsphere_context.build_nbdkit_vddk_cmd(
                si=si,
                vm=vm,
                snap_obj=snap_obj,
                disk_ds_path=disk_ds_path,
                server_host=server_host,
                user=host_user,
                password_file=pw_path,
                thumbprint=thumbprint,
                libdir=libdir,
                stored_type=connection_type,
            )
            log_info(
                f"[NBD] VDDK via {vsphere_context.connection_label(conn_type)}: "
                f"vm=moref={vsphere_context.get_vm_moref(vm)} "
                f"snapshot={vsphere_context.get_snapshot_moref(snap_obj)} "
                f"disk={disk_ds_path}"
            )
            try:
                if progress_callback:
                    progress_callback(progress_base)
                nbytes = _stream_disk_via_nbdcopy(cmd, dest_path)
                if progress_callback:
                    progress_callback(min(progress_base + progress_total, 99))
                return nbytes
            except RuntimeError as e:
                last_err = e
                log_warn(f"[NBD] VDDK open failed for {disk_ds_path}: {str(e)[:300]}")
        raise last_err or RuntimeError("VDDK failed for all disk open candidates")
    finally:
        try:
            os.unlink(pw_path)
        except OSError:
            pass


def export_live_nbd(
    si,
    vm_name,
    storage,
    dest_rel_dir,
    disk_descriptors,
    vmx_ds_name,
    vmx_rel_path,
    server_host,
    host_user,
    host_password,
    config=None,
    connection_type=vsphere_context.CONN_AUTO,
    progress_callback=None,
    speed_callback=None,
    is_cancelled_func=None,
    create_snapshot_func=None,
    remove_snapshot_func=None,
    download_vmx_func=None,
):
    """
    Live VM backup via VDDK/NBD (no CopyVirtualDisk temp on ESXi).
    server_host is the registered endpoint (ESXi or vCenter FQDN/IP).
    """
    if not is_available(config):
        return False, f"NBD transport unavailable: {availability_message(config)}"

    from backup_engine import _find_snapshot_by_name, _collect_vm_disk_layout

    vm = vsphere_context.find_vm_by_name(si, vm_name)
    if not vm:
        return False, f"VM {vm_name} not found"

    snap_obj = None
    snap_name = None
    files_downloaded = []

    try:
        if progress_callback:
            progress_callback(2)
        snap_obj, snap_name = create_snapshot_func(si, vm_name)
        if not snap_obj:
            return False, f"Snapshot creation failed: {snap_name}"
        if snap_name and not getattr(snap_obj, "_moId", None):
            vm_refreshed = vsphere_context.find_vm_by_name(si, vm_name)
            resolved = _find_snapshot_by_name(vm_refreshed, snap_name)
            if resolved:
                snap_obj = resolved
            else:
                return False, f"Cannot resolve snapshot moRef for {snap_name}"

        log_info(f"[NBD] Waiting {SNAPSHOT_SETTLE_SECS}s for snapshot to settle...")
        time.sleep(SNAPSHOT_SETTLE_SECS)

        if progress_callback:
            progress_callback(5)

        vm = vsphere_context.find_vm_by_name(si, vm_name)
        _, disk_descriptors, _, _ = _collect_vm_disk_layout(vm)
        if not disk_descriptors:
            return False, f"No disks found for {vm_name} after snapshot"

        storage.makedirs(dest_rel_dir)
        total_disks = len(disk_descriptors)

        for idx, disk in enumerate(disk_descriptors):
            if is_cancelled_func and is_cancelled_func():
                return False, "Backup cancelled by user"

            disk_basename = os.path.basename(disk["rel_path"])
            flat_basename = disk_basename.replace(".vmdk", "-flat.vmdk")
            flat_rel = f"{dest_rel_dir}/{flat_basename}"
            desc_rel = f"{dest_rel_dir}/{disk_basename}"

            step_base = 5 + (85 * idx // max(total_disks, 1))
            step_end = 5 + (85 * (idx + 1) // max(total_disks, 1))

            log_info(f"[NBD] Disk {idx + 1}/{total_disks}: {disk_basename}")

            if download_vmx_func:
                download_vmx_func(
                    si, disk["ds_name"], disk["rel_path"], storage, desc_rel,
                    progress_callback=progress_callback,
                    progress_base=step_base,
                    progress_total=2,
                    speed_callback=speed_callback,
                    is_cancelled_func=is_cancelled_func,
                    vm=vm,
                )
            files_downloaded.append(disk_basename)

            stream_snapshot_disk(
                si, vm, snap_obj, disk,
                server_host, host_user, host_password,
                storage, flat_rel, config=config,
                connection_type=connection_type,
                is_cancelled_func=is_cancelled_func,
                progress_callback=progress_callback,
                progress_base=step_base + 2,
                progress_total=max(step_end - step_base - 2, 1),
                speed_callback=speed_callback,
            )
            files_downloaded.append(flat_basename)

        if progress_callback:
            progress_callback(93)
        if remove_snapshot_func and snap_name:
            remove_snapshot_func(si, vm_name, snap_name, timeout_mins=60)
            snap_name = None

        if progress_callback:
            progress_callback(96)
        if vmx_ds_name and vmx_rel_path and download_vmx_func:
            vmx_filename = os.path.basename(vmx_rel_path)
            try:
                download_vmx_func(
                    si, vmx_ds_name, vmx_rel_path, storage, f"{dest_rel_dir}/{vmx_filename}",
                    is_cancelled_func=is_cancelled_func,
                    vm=vm,
                )
                files_downloaded.append(vmx_filename)
            except Exception as e:
                log_warn(f"[NBD] VMX download warning: {e}")

        if progress_callback:
            progress_callback(100)
        conn = vsphere_context.resolve_connection_type(si, connection_type)
        return True, (
            f"Backup completed [nbd/{conn}]: {len(files_downloaded)} file(s) saved to storage"
        )

    except VddkNotAvailableError as e:
        return False, str(e)
    except Exception as e:
        if is_cancelled_func and is_cancelled_func():
            return False, "Backup cancelled by user"
        log_error(f"[NBD] Live backup failed: {e}")
        return False, str(e)
    finally:
        if snap_name and remove_snapshot_func:
            try:
                remove_snapshot_func(si, vm_name, snap_name, timeout_mins=30)
            except Exception as ce:
                log_error(f"[NBD] Snapshot cleanup error: {ce}")


def read_snapshot_extent(
    si,
    vm,
    snap_obj,
    disk,
    offset,
    length,
    server_host,
    host_user,
    host_password,
    config=None,
    connection_type=vsphere_context.CONN_AUTO,
):
    """Read a byte range from a snapshot-backed disk via NBD/VDDK."""
    if not is_available(config):
        raise VddkNotAvailableError(availability_message(config))

    ensure_vddk_runtime_dirs()
    libdir = get_vddk_libdir(config)
    thumbprint = get_server_thumbprint(server_host)
    conn_type = vsphere_context.resolve_connection_type(si, connection_type)
    candidates = vsphere_context.vddk_disk_open_candidates(disk, conn_type)

    with tempfile.NamedTemporaryFile(mode="w", delete=False, prefix="vddk_pw_") as pw_file:
        pw_file.write(host_password)
        pw_path = pw_file.name

    nbdsh = shutil.which("nbdsh")
    if not nbdsh:
        os.unlink(pw_path)
        raise VddkNotAvailableError("nbdsh not found in PATH (install libnbd-bin)")

    last_err = None
    try:
        for disk_ds_path in candidates:
            cmd, _ = vsphere_context.build_nbdkit_vddk_cmd(
                si=si,
                vm=vm,
                snap_obj=snap_obj,
                disk_ds_path=disk_ds_path,
                server_host=server_host,
                user=host_user,
                password_file=pw_path,
                thumbprint=thumbprint,
                libdir=libdir,
                stored_type=connection_type,
            )
            script = f"import sys; sys.stdout.buffer.write(h.pread({int(length)}, {int(offset)}))"
            run_cmd = cmd + ["--run", f'{nbdsh} -c "{script}"']
            proc = subprocess.run(run_cmd, capture_output=True, timeout=7200)
            if proc.returncode == 0:
                return proc.stdout
            last_err = (proc.stderr or proc.stdout or b"").decode("utf-8", errors="replace")[:500]
            log_warn(f"[NBD] extent read failed for {disk_ds_path}: {last_err}")
        raise RuntimeError(last_err or "VDDK extent read failed for all disk candidates")
    finally:
        try:
            os.unlink(pw_path)
        except OSError:
            pass
