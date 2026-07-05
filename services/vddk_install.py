"""Auto-install VMware VDDK when ESXi hosts are added."""

import glob
import os
import shutil
import tarfile

from config_env import DATA_DIR
from logger_util import log_info, log_warn, log_error

# Bundled tarball locations (read-only mount in Docker)
VDDK_VENDOR_DIRS = [
    "/app/vendor/vddk",
    "/opt/NovaBak/vendor/vddk",
    os.path.join(DATA_DIR, "vendor", "vddk"),
]


def get_vddk_libdir(config=None):
    if config is not None:
        libdir = getattr(config, "vddk_libdir", None)
        if libdir and libdir.strip():
            return libdir.strip()
    env = os.environ.get("VDDK_LIBDIR")
    if env:
        return env
    return os.path.join(DATA_DIR, "vddk", "vmware-vix-disklib-distrib")


def find_vddk_tarball():
    """Return newest VMware-vix-disklib-*.tar.gz in vendor directories."""
    candidates = []
    for vendor_dir in VDDK_VENDOR_DIRS:
        if not os.path.isdir(vendor_dir):
            continue
        candidates.extend(glob.glob(os.path.join(vendor_dir, "VMware-vix-disklib-*.tar.gz")))
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def is_vddk_installed(libdir=None):
    libdir = libdir or get_vddk_libdir()
    for sub in ("lib64", "lib32"):
        if os.path.isfile(os.path.join(libdir, sub, "libvixDiskLib.so")):
            return True
    return False


def install_vddk_from_tarball(tarball_path, libdir=None):
    """
    Extract VDDK tarball to libdir.
    Returns (ok: bool, message: str).
    """
    libdir = libdir or get_vddk_libdir()
    if not tarball_path or not os.path.isfile(tarball_path):
        return False, f"VDDK tarball not found: {tarball_path}"

    log_info(f"[VDDK] Installing from {tarball_path} → {libdir}")
    tmp = libdir + ".tmp"
    try:
        if os.path.isdir(tmp):
            shutil.rmtree(tmp)
        os.makedirs(tmp, exist_ok=True)

        with tarfile.open(tarball_path, "r:gz") as tf:
            tf.extractall(tmp)

        distrib = None
        for name in os.listdir(tmp):
            if name == "vmware-vix-disklib-distrib":
                distrib = os.path.join(tmp, name)
                break
        if not distrib:
            return False, "vmware-vix-disklib-distrib not found in tarball"

        so_path = os.path.join(distrib, "lib64", "libvixDiskLib.so")
        if not os.path.isfile(so_path):
            so_path = os.path.join(distrib, "lib32", "libvixDiskLib.so")
        if not os.path.isfile(so_path):
            return False, "libvixDiskLib.so missing in tarball"

        if os.path.isdir(libdir):
            shutil.rmtree(libdir)
        shutil.move(distrib, libdir)
        shutil.rmtree(tmp, ignore_errors=True)

        version = os.path.basename(tarball_path).replace("VMware-vix-disklib-", "").replace(".x86_64.tar.gz", "")
        log_info(f"[VDDK] Installed version {version} at {libdir}")
        return True, f"VDDK {version} installed at {libdir}"

    except Exception as e:
        log_error(f"[VDDK] Install failed: {e}")
        shutil.rmtree(tmp, ignore_errors=True)
        return False, str(e)


def ensure_vddk_installed(config=None, force=False):
    """
    Install VDDK from vendor tarball if not already present.
    Returns (installed: bool, message: str).
    """
    libdir = get_vddk_libdir(config)

    if not force and is_vddk_installed(libdir):
        return True, f"VDDK already installed at {libdir}"

    tarball = find_vddk_tarball()
    if not tarball:
        return False, (
            "No VDDK tarball in vendor/vddk/. "
            "Place VMware-vix-disklib-*.tar.gz in /opt/NovaBak/vendor/vddk/ on the server."
        )

    ok, msg = install_vddk_from_tarball(tarball, libdir)
    return ok, msg


def ensure_vddk_on_host_add(db):
    """
    Called when an ESXi host is added: install VDDK and enable NBD transport.
    Returns dict with vddk status for API/UI feedback.
    """
    from models import Config

    config = db.query(Config).first()
    ok, msg = ensure_vddk_installed(config)

    if config:
        if ok:
            config.vddk_libdir = get_vddk_libdir(config)
            if getattr(config, "backup_transport", "legacy") in ("legacy", None, ""):
                config.backup_transport = "nbd"
                log_info("[VDDK] Set backup_transport=nbd after VDDK install")
        db.commit()

    if ok:
        log_info(f"[VDDK] Host-add bootstrap: {msg}")
    else:
        log_warn(f"[VDDK] Host-add bootstrap skipped: {msg}")

    return {"vddk_installed": ok, "vddk_message": msg}
