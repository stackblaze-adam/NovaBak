#!/usr/bin/env bash
# Install VMware VDDK for NovaBak NBD transport.
# Download VDDK 9.x Linux tarball from Broadcom (requires free developer account):
#   https://developer.broadcom.com/sdks/vmware-virtual-disk-development-kit-vddk/latest
#
# Usage:
#   sudo ./scripts/install-vddk.sh /path/to/VMware-vix-disklib-*.tar.gz

set -euo pipefail

TARBALL="${1:-}"
DESTDIR="${VDDK_DEST:-/opt/vmware-vix-disklib-distrib}"

if [[ -z "$TARBALL" || ! -f "$TARBALL" ]]; then
  echo "Usage: $0 /path/to/VMware-vix-disklib-*.tar.gz"
  exit 1
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

tar xzf "$TARBALL" -C "$TMP"
SRC=$(find "$TMP" -maxdepth 1 -type d -name 'vmware-vix-disklib-distrib' | head -1)
if [[ -z "$SRC" ]]; then
  echo "ERROR: vmware-vix-disklib-distrib not found in tarball"
  exit 1
fi

rm -rf "$DESTDIR"
mkdir -p "$(dirname "$DESTDIR")"
mv "$SRC" "$DESTDIR"

if [[ ! -f "$DESTDIR/lib64/libvixDiskLib.so" ]]; then
  echo "ERROR: libvixDiskLib.so missing under $DESTDIR/lib64"
  exit 1
fi

echo "VDDK installed to $DESTDIR"
echo "Set in NovaBak Settings → VDDK library path, or VDDK_LIBDIR=$DESTDIR"
