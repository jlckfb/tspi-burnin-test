#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
BUILD_DIR="${PROJECT_DIR}/build/deb"
DIST_DIR="${PROJECT_DIR}/dist"

# shellcheck source=scripts/config_common.sh
source "${SCRIPT_DIR}/config_common.sh"
burnin_load_config

PACKAGE_VERSION="0.2.$(date -u +%Y%m%d%H%M%S)"
PKG_NAME="tspi-burnin-agent"
PKG_ROOT="${BUILD_DIR}/${PKG_NAME}_${PACKAGE_VERSION}_all"
DEB_PATH="${DIST_DIR}/${PKG_NAME}_${PACKAGE_VERSION}_all.deb"

rm -rf "${BUILD_DIR}"
mkdir -p \
  "${PKG_ROOT}/DEBIAN" \
  "${PKG_ROOT}/opt/tspi-burnin/agent" \
  "${PKG_ROOT}/etc/tspi-burnin" \
  "${PKG_ROOT}/etc/systemd/system" \
  "${PKG_ROOT}/etc/sysctl.d" \
  "${PKG_ROOT}/var/lib/tspi-burnin" \
  "${DIST_DIR}"

python3 -m py_compile "${PROJECT_DIR}/agent/tspi_burnin_agent.py"

install -m 0755 "${PROJECT_DIR}/agent/tspi_burnin_agent.py" "${PKG_ROOT}/opt/tspi-burnin/agent/tspi_burnin_agent.py"
install -m 0644 "${PROJECT_DIR}/systemd/tspi-burnin-agent.service" "${PKG_ROOT}/etc/systemd/system/tspi-burnin-agent.service"
install -m 0644 "${PROJECT_DIR}/systemd/tspi-burnin-iperf3.service" "${PKG_ROOT}/etc/systemd/system/tspi-burnin-iperf3.service"

burnin_emit_agent_config "${PKG_ROOT}/etc/tspi-burnin/config.toml"

cat >"${PKG_ROOT}/etc/sysctl.d/99-tspi-burnin.conf" <<'EOF'
kernel.panic = 10
kernel.panic_on_oops = 1
net.ipv4.conf.all.arp_filter = 1
net.ipv4.conf.default.arp_filter = 1
net.ipv4.conf.all.arp_ignore = 1
net.ipv4.conf.default.arp_ignore = 1
net.ipv4.conf.all.arp_announce = 2
net.ipv4.conf.default.arp_announce = 2
EOF

cat >"${PKG_ROOT}/DEBIAN/control" <<EOF
Package: ${PKG_NAME}
Version: ${PACKAGE_VERSION}
Section: net
Priority: optional
Architecture: all
Maintainer: TaishanPi Burn-in Maintainers <maintainers@example.com>
Depends: python3, iperf3, iw, bluez, network-manager, iproute2, iputils-ping
Description: TaishanPi 3M RK3576 WiFi and Bluetooth burn-in agent
 Board-side agent and iperf3 service for AP6256 WiFi/BT burn-in tests.
EOF

cat >"${PKG_ROOT}/DEBIAN/conffiles" <<'EOF'
/etc/tspi-burnin/config.toml
/etc/sysctl.d/99-tspi-burnin.conf
EOF

cat >"${PKG_ROOT}/DEBIAN/postinst" <<'EOF'
#!/usr/bin/env bash
set -e
systemctl daemon-reload || true
sysctl --system >/dev/null 2>&1 || true
systemctl enable tspi-burnin-iperf3.service >/dev/null 2>&1 || true
systemctl restart tspi-burnin-iperf3.service >/dev/null 2>&1 || true
systemctl enable tspi-burnin-agent.service >/dev/null 2>&1 || true
systemctl restart tspi-burnin-agent.service >/dev/null 2>&1 || true
exit 0
EOF

cat >"${PKG_ROOT}/DEBIAN/prerm" <<'EOF'
#!/usr/bin/env bash
set -e
if [[ "${1:-}" = "remove" || "${1:-}" = "deconfigure" ]]; then
  systemctl stop tspi-burnin-agent.service >/dev/null 2>&1 || true
  systemctl stop tspi-burnin-iperf3.service >/dev/null 2>&1 || true
  systemctl disable tspi-burnin-agent.service >/dev/null 2>&1 || true
  systemctl disable tspi-burnin-iperf3.service >/dev/null 2>&1 || true
fi
exit 0
EOF

cat >"${PKG_ROOT}/DEBIAN/postrm" <<'EOF'
#!/usr/bin/env bash
set -e
systemctl daemon-reload >/dev/null 2>&1 || true
exit 0
EOF

chmod 0755 "${PKG_ROOT}/DEBIAN/postinst" "${PKG_ROOT}/DEBIAN/prerm" "${PKG_ROOT}/DEBIAN/postrm"
find "${PKG_ROOT}" -type d -exec chmod 0755 {} +
dpkg-deb --build --root-owner-group "${PKG_ROOT}" "${DEB_PATH}" >/dev/null
dpkg-deb --info "${DEB_PATH}" >/dev/null

(
  cd "${DIST_DIR}"
  sha256sum "$(basename "${DEB_PATH}")" > SHA256SUMS
)

echo "Built: ${DEB_PATH}"
echo "SHA256: $(cut -d' ' -f1 "${DIST_DIR}/SHA256SUMS")"
echo
echo "Install on board:"
echo "  sudo apt install -y ./$(basename "${DEB_PATH}")"
