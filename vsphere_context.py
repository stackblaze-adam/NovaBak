"""
vsphere_context.py — Standalone ESXi and vCenter connection helpers.

NovaBak may register either a standalone ESXi host or a vCenter Server.
Transport code uses this module for VM lookup, datacenter paths, moRefs,
and VDDK/NFC capability detection.
"""

from pyVmomi import vim

CONN_STANDALONE = "standalone"
CONN_VCENTER = "vcenter"
CONN_AUTO = "auto"


def detect_connection_type(si):
    """Return 'vcenter' or 'standalone' from a live ServiceInstance."""
    try:
        about = si.RetrieveContent().about
        if getattr(about, "apiType", "") == "VirtualCenter":
            return CONN_VCENTER
    except Exception:
        pass
    return CONN_STANDALONE


def resolve_connection_type(si, stored_type=CONN_AUTO):
    """Use stored host type unless set to auto-detect."""
    if stored_type and stored_type not in (CONN_AUTO, ""):
        return stored_type
    return detect_connection_type(si)


def connection_label(conn_type):
    if conn_type == CONN_VCENTER:
        return "vCenter"
    return "standalone ESXi"


def supports_nfc_export(si, stored_type=CONN_AUTO):
    """HttpNfcLease ExportSnapshot requires a vCenter connection."""
    return resolve_connection_type(si, stored_type) == CONN_VCENTER


def find_vm_by_name(si, vm_name):
    """
    Locate a VM by display name on standalone ESXi or vCenter.
    Standalone fast-path: ha-datacenter/vm/{name}. vCenter: inventory search.
    """
    content = si.RetrieveContent()
    vm = content.searchIndex.FindByInventoryPath(f"ha-datacenter/vm/{vm_name}")
    if vm:
        return vm

    container = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.VirtualMachine], True)
    try:
        matches = [v for v in container.view if v.name == vm_name]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError(
                f"Multiple VMs named {vm_name!r}; rename duplicates or use inventory paths"
            )
    finally:
        container.Destroy()
    return None


def get_datacenter_name(vm):
    """Datacenter name for HTTP folder URLs (dcPath=)."""
    obj = getattr(vm, "parent", None)
    while obj:
        if isinstance(obj, vim.Datacenter):
            return obj.name
        obj = getattr(obj, "parent", None)
    return "ha-datacenter"


def get_first_datacenter_name(si):
    """Fallback dcPath when no VM object is available (vCenter)."""
    content = si.RetrieveContent()
    container = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.Datacenter], True)
    try:
        if container.view:
            return container.view[0].name
    finally:
        container.Destroy()
    return "ha-datacenter"


def resolve_dc_path(si, vm=None, stored_type=CONN_AUTO):
    """Best dcPath for datastore HTTP folder access."""
    if vm is not None:
        return get_datacenter_name(vm)
    if resolve_connection_type(si, stored_type) == CONN_VCENTER:
        return get_first_datacenter_name(si)
    return "ha-datacenter"


def get_vm_moref(vm):
    mo_id = getattr(vm, "_moId", None)
    if not mo_id:
        raise ValueError("Cannot determine VM managed object reference")
    return mo_id


def get_snapshot_moref(snap_obj):
    mo_id = getattr(snap_obj, "_moId", None)
    if not mo_id:
        raise ValueError("Cannot determine snapshot managed object reference")
    return mo_id


def get_session_cookie(si):
    """vmware_soap_session cookie for nbdkit cookie= on vCenter."""
    cookie_str = getattr(si._stub, "cookie", "") or ""
    for part in cookie_str.split(";"):
        part = part.strip()
        if part.startswith("vmware_soap_session="):
            return part.split("=", 1)[1].strip('"')
    return None


def vddk_base_disk_path(disk):
    """
    VDDK opens the base .vmdk descriptor; snapshot moRef selects point-in-time.
    """
    import re
    rel = disk["rel_path"]
    base_rel = re.sub(r"-\d+\.vmdk$", ".vmdk", rel)
    return f"[{disk['ds_name']}] {base_rel}"


def vddk_disk_open_candidates(disk, conn_type=CONN_AUTO):
    """
    Disk paths to try with VDDK ConnectEx + snapshot moRef.
    Standalone ESXi: prefer post-snapshot active descriptor, then base.
    vCenter: prefer base + snapshot chain traversal, then active delta.
    """
    active = disk["ds_path"]
    base = vddk_base_disk_path(disk)
    if conn_type == CONN_STANDALONE:
        order = [active, base]
    else:
        order = [base, active]
    seen = set()
    out = []
    for p in order:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def build_nbdkit_vddk_cmd(
    si,
    vm,
    snap_obj,
    disk_ds_path,
    server_host,
    user,
    password_file,
    thumbprint,
    libdir,
    stored_type=CONN_AUTO,
    transports="nbdssl:nbd",
):
    """
    Build nbdkit VDDK command prefix.

    Standalone ESXi: vm=moref=107, snapshot=107-snapshot-19
    vCenter:         vm=moref=vm-16, snapshot=snapshot-12345, optional cookie=
    """
    cmd = [
        "nbdkit",
        "-v",
        "-r",
        "vddk",
        f"libdir={libdir}",
        f"server={server_host}",
        f"user={user}",
        f"password=+{password_file}",
        f"thumbprint={thumbprint}",
        f"vm=moref={get_vm_moref(vm)}",
        f"snapshot={get_snapshot_moref(snap_obj)}",
        f"transports={transports}",
        disk_ds_path,
    ]
    conn_type = resolve_connection_type(si, stored_type)
    cookie = get_session_cookie(si)
    if cookie:
        cmd.insert(-1, f"cookie={cookie}")
    return cmd, conn_type
