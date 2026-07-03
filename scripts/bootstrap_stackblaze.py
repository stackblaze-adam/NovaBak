#!/usr/bin/env python3
"""Bootstrap NovaBak for StackBlaze: storage, ESXi hosts, VM sync, schedules."""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import SessionLocal, VM, init_db
from services import backup_ops


DEFAULT_HOSTS = [
    ("esxi-3r3txk2", "3r3txk2.stackblaze.cloud"),
    ("esxi-12jxkb2", "12jxkb2.stackblaze.cloud"),
    ("esxi-5r1fq53", "5r1fq53.stackblaze.cloud"),
    ("esxi-90xq0m2", "90xq0m2.stackblaze.cloud"),
]

SKIP_VM_PREFIXES = ("tpl-", "template", "novabak")


def _should_protect(vm_name: str) -> bool:
    lower = vm_name.lower()
    if any(lower.startswith(p) for p in SKIP_VM_PREFIXES):
        return False
    if "novabak" in lower:
        return False
    return True


def bootstrap_local(
    esxi_user: str,
    esxi_password: str,
    nfs_path: str = "/mnt/backups",
    schedule_hour: int = 2,
    schedule_minute: int = 0,
    retention: int = 7,
    protect_all: bool = False,
):
    init_db()
    db = SessionLocal()
    try:
        print("Configuring NFS storage...")
        backup_ops.update_storage_config(
            db,
            {"storage_type": "NFS", "nfs_path": nfs_path},
        )
        ok, msg = backup_ops.test_storage(db)
        print(f"Storage test: ok={ok} — {msg}")
        if not ok:
            print("Warning: storage test failed; continuing anyway.", file=sys.stderr)

        host_ids = []
        for name, host_ip in DEFAULT_HOSTS:
            print(f"Registering host {name} ({host_ip})...")
            try:
                host = backup_ops.add_esxi_host(db, name, host_ip, esxi_user, esxi_password)
            except ValueError:
                from models import ESXiHost

                host = db.query(ESXiHost).filter(ESXiHost.name == name).first()
                if not host:
                    raise
                print(f"  Host '{name}' already exists (id={host.id})")
            host_ids.append(host.id)

            print(f"  Syncing VMs from {name}...")
            result = backup_ops.sync_vms_for_host(db, host.id)
            print(f"  Synced {len(result['synced_new'])} new VMs ({result['total_on_host']} on host)")

        vms = db.query(VM).order_by(VM.vm_name).all()
        enabled = 0
        for vm in vms:
            selected = protect_all or _should_protect(vm.vm_name)
            backup_ops.update_vm_job(
                db,
                vm.id,
                {
                    "is_selected": selected,
                    "is_job_active": selected,
                    "schedule_hour": schedule_hour,
                    "schedule_minute": schedule_minute,
                    "retention_count": retention,
                    "schedule_frequency": "daily",
                    "schedule_days": "0,1,2,3,4,5,6",
                },
            )
            if selected:
                enabled += 1
                print(f"  Enabled: {vm.vm_name}")

        print(f"\nBootstrap complete: {enabled}/{len(vms)} VMs enabled for backup.")
        return 0
    finally:
        db.close()


def bootstrap_api(base_url: str, api_key: str, **kwargs):
    import requests

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    session = requests.Session()
    session.headers.update(headers)

    r = session.put(
        f"{base_url.rstrip('/')}/api/v1/config/storage",
        json={"storage_type": "NFS", "nfs_path": kwargs.get("nfs_path", "/mnt/backups")},
    )
    r.raise_for_status()
    print("Storage configured.")

    r = session.post(f"{base_url.rstrip('/')}/api/v1/config/storage/test")
    print(f"Storage test: {r.json()}")

    esxi_user = kwargs["esxi_user"]
    esxi_password = kwargs["esxi_password"]
    for name, host_ip in DEFAULT_HOSTS:
        r = session.post(
            f"{base_url.rstrip('/')}/api/v1/hosts",
            json={"name": name, "host_ip": host_ip, "username": esxi_user, "password": esxi_password},
        )
        if r.status_code == 409:
            hosts = session.get(f"{base_url.rstrip('/')}/api/v1/hosts").json()
            host = next(h for h in hosts if h["name"] == name)
            host_id = host["id"]
        else:
            r.raise_for_status()
            host_id = r.json()["id"]
        sync = session.post(f"{base_url.rstrip('/')}/api/v1/hosts/{host_id}/sync-vms")
        sync.raise_for_status()
        print(f"{name}: {sync.json()}")

    vms = session.get(f"{base_url.rstrip('/')}/api/v1/vms").json()
    protect_all = kwargs.get("protect_all", False)
    enabled = 0
    for vm in vms:
        selected = protect_all or _should_protect(vm["vm_name"])
        if selected:
            session.patch(
                f"{base_url.rstrip('/')}/api/v1/vms/{vm['id']}",
                json={
                    "is_selected": True,
                    "is_job_active": True,
                    "schedule_hour": kwargs.get("schedule_hour", 2),
                    "schedule_minute": kwargs.get("schedule_minute", 0),
                    "retention_count": kwargs.get("retention", 7),
                    "schedule_frequency": "daily",
                    "schedule_days": "0,1,2,3,4,5,6",
                },
            ).raise_for_status()
            enabled += 1
            print(f"  Enabled: {vm['vm_name']}")
    print(f"\nBootstrap complete: {enabled}/{len(vms)} VMs enabled.")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Bootstrap NovaBak for StackBlaze")
    parser.add_argument("--local", action="store_true", help="Use direct DB access (run on server)")
    parser.add_argument("--api-url", default=os.environ.get("NOVABAK_URL", "https://127.0.0.1:8000"))
    parser.add_argument("--api-key", default=os.environ.get("NOVABAK_API_KEY", ""))
    parser.add_argument("--esxi-user", default=os.environ.get("ESXI_USER", ""))
    parser.add_argument("--esxi-password", default=os.environ.get("ESXI_PASSWORD", ""))
    parser.add_argument("--nfs-path", default=os.environ.get("NFS_PATH", "/mnt/backups"))
    parser.add_argument("--schedule-hour", type=int, default=int(os.environ.get("SCHEDULE_HOUR", "2")))
    parser.add_argument("--schedule-minute", type=int, default=int(os.environ.get("SCHEDULE_MINUTE", "0")))
    parser.add_argument("--retention", type=int, default=int(os.environ.get("RETENTION", "7")))
    parser.add_argument("--protect-all", action="store_true", help="Enable backup for every VM including templates")
    args = parser.parse_args()

    if not args.esxi_user or not args.esxi_password:
        print("ESXI_USER and ESXI_PASSWORD are required.", file=sys.stderr)
        return 1

    common = dict(
        esxi_user=args.esxi_user,
        esxi_password=args.esxi_password,
        nfs_path=args.nfs_path,
        schedule_hour=args.schedule_hour,
        schedule_minute=args.schedule_minute,
        retention=args.retention,
        protect_all=args.protect_all,
    )

    if args.local:
        return bootstrap_local(**common)
    if not args.api_key:
        print("NOVABAK_API_KEY or --api-key required for API mode.", file=sys.stderr)
        return 1
    return bootstrap_api(args.api_url, args.api_key, **common)


if __name__ == "__main__":
    raise SystemExit(main())
