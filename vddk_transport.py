"""
vddk_transport.py — VDDK/NBD live backup transport (Phase 1 skeleton)

Streams snapshot-backed virtual disks over NBD using nbdkit-vddk-plugin.
Requires VMware VDDK (proprietary, not bundled) and nbdkit with the vddk plugin.

See: https://libguestfs.org/nbdkit-vddk-plugin.1.html
"""

import os
import shutil
import socket
import ssl
import subprocess
import tempfile

from logger_util import log_info, log_warn, log_error

# Cached ESXi SSL thumbprints for the process lifetime
_thumbprint_cache = {}


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


def _vm_moref(vm):
    mo_id = getattr(vm, "_moId", None)
    if not mo_id:
        raise VddkNotAvailableError("Cannot determine VM managed object reference")
    return mo_id


def _snapshot_moref(snap_obj):
    mo_id = getattr(snap_obj, "_moId", None)
    if not mo_id:
        raise VddkNotAvailableError("Cannot determine snapshot managed object reference")
    return mo_id


def _build_nbdkit_cmd(server, user, password_file, thumbprint, vm_moref, snap_moref,
                      disk_ds_path, libdir, transports="nbdssl:nbd"):
    """Build nbdkit command prefix (without --run)."""
    return [
        "nbdkit",
        "-v",
        "vddk",
        f"libdir={libdir}",
        f"server={server}",
        f"user={user}",
        f"password=+{password_file}",
        f"thumbprint={thumbprint}",
        f"vm=moref={vm_moref}",
        f"snapshot=moref={snap_moref}",
        f"transports={transports}",
        disk_ds_path,
    ]


def _stream_disk_via_nbdcopy(cmd_prefix, dest_path, timeout_secs=7200):
    """Run nbdkit with nbdcopy to write a flat disk image to dest_path."""
    nbdcopy = shutil.which("nbdcopy")
    if not nbdcopy:
        raise VddkNotAvailableError("nbdcopy not found in PATH (install libnbd-bin)")

    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    run_cmd = cmd_prefix + ["--run", f'{nbdcopy} "$uri" "{dest_path}"']
    log_info(f"[NBD] Streaming disk → {dest_path}")
    proc = subprocess.run(
        run_cmd,
        capture_output=True,
        text=True,
        timeout=timeout_secs,
    )
    if proc.returncode != 0:
        stderr = (proc.stderr or proc.stdout or "").strip()[:500]
        raise RuntimeError(f"nbdcopy failed (exit {proc.returncode}): {stderr}")
    return os.path.getsize(dest_path) if os.path.isfile(dest_path) else 0


def _resolve_local_dest(storage, dest_rel_path):
    """NBD skeleton writes via nbdcopy to a local path; resolve from StorageProvider."""
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
    host_ip,
    host_user,
    host_password,
    storage,
    dest_rel_path,
    config=None,
    is_cancelled_func=None,
    progress_callback=None,
    progress_base=0,
    progress_total=100,
    speed_callback=None,
):
    """
    Stream one snapshot-backed disk descriptor to storage via NBD/VDDK.
    Returns bytes written.
    """
    if not is_available(config):
        raise VddkNotAvailableError(availability_message(config))

    dest_path, err = _resolve_local_dest(storage, dest_rel_path)
    if err:
        raise VddkNotAvailableError(err)

    libdir = get_vddk_libdir(config)
    thumbprint = get_server_thumbprint(host_ip)
    vm_moref = _vm_moref(vm)
    snap_moref = _snapshot_moref(snap_obj)
    disk_ds_path = disk["ds_path"]

    with tempfile.NamedTemporaryFile(mode="w", delete=False, prefix="vddk_pw_") as pw_file:
        pw_file.write(host_password)
        pw_path = pw_file.name

    try:
        cmd = _build_nbdkit_cmd(
            server=host_ip,
            user=host_user,
            password_file=pw_path,
            thumbprint=thumbprint,
            vm_moref=vm_moref,
            snap_moref=snap_moref,
            disk_ds_path=disk_ds_path,
            libdir=libdir,
        )
        if is_cancelled_func and is_cancelled_func():
            raise RuntimeError("Backup cancelled by user")
        if progress_callback:
            progress_callback(progress_base)
        nbytes = _stream_disk_via_nbdcopy(cmd, dest_path)
        if progress_callback:
            progress_callback(min(progress_base + progress_total, 99))
        return nbytes
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
    host_ip,
    host_user,
    host_password,
    config=None,
    progress_callback=None,
    speed_callback=None,
    is_cancelled_func=None,
    create_snapshot_func=None,
    remove_snapshot_func=None,
    download_vmx_func=None,
):
    """
    Live VM backup via VDDK/NBD (no CopyVirtualDisk temp on ESXi).

    create_snapshot_func / remove_snapshot_func / download_vmx_func are injected
    from backup_engine to avoid circular imports.
    """
    if not is_available(config):
        return False, f"NBD transport unavailable: {availability_message(config)}"

    vm = None
    from backup_engine import _get_vm  # local import to avoid cycle at module load
    vm = _get_vm(si, vm_name)
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
        if progress_callback:
            progress_callback(5)

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

            # Descriptor still fetched via HTTP (small, unlocked file)
            if download_vmx_func:
                download_vmx_func(
                    si, disk["ds_name"], disk["rel_path"], storage, desc_rel,
                    progress_callback=progress_callback,
                    progress_base=step_base,
                    progress_total=2,
                    speed_callback=speed_callback,
                    is_cancelled_func=is_cancelled_func,
                )
            files_downloaded.append(disk_basename)

            stream_snapshot_disk(
                si, vm, snap_obj, disk,
                host_ip, host_user, host_password,
                storage, flat_rel, config=config,
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
                )
                files_downloaded.append(vmx_filename)
            except Exception as e:
                log_warn(f"[NBD] VMX download warning: {e}")

        if progress_callback:
            progress_callback(100)
        return True, f"Backup completed [nbd]: {len(files_downloaded)} file(s) saved to storage"

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
