#!/usr/bin/env bash
set -euo pipefail

BURNIN_CONFIG_COMMON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BURNIN_PROJECT_DIR="$(cd "${BURNIN_CONFIG_COMMON_DIR}/.." && pwd)"
BURNIN_CONFIG_FILE="${BURNIN_CONFIG_FILE:-${BURNIN_PROJECT_DIR}/config/tspi-burnin.env}"

burnin_load_config() {
  if [[ ! -f "${BURNIN_CONFIG_FILE}" ]]; then
    echo "Missing unified config: ${BURNIN_CONFIG_FILE}" >&2
    echo "Create it with: cp config/tspi-burnin.example.env config/tspi-burnin.env" >&2
    exit 1
  fi

  set -a
  # shellcheck disable=SC1090
  source "${BURNIN_CONFIG_FILE}"
  set +a

  : "${BURNIN_SERVER_URL:?BURNIN_SERVER_URL is required in ${BURNIN_CONFIG_FILE}}"
  : "${BURNIN_API_TOKEN:?BURNIN_API_TOKEN is required in ${BURNIN_CONFIG_FILE}}"

  export BURNIN_AGENT_BOARD_ID="${BURNIN_AGENT_BOARD_ID:-}"
  export BURNIN_AGENT_UPLINK_INTERFACE="${BURNIN_AGENT_UPLINK_INTERFACE:-end0}"
  export BURNIN_AGENT_REQUIRE_UPLINK_INTERFACE="${BURNIN_AGENT_REQUIRE_UPLINK_INTERFACE:-true}"
  export BURNIN_AGENT_WIFI_INTERFACE="${BURNIN_AGENT_WIFI_INTERFACE:-wlan0}"
  export BURNIN_AGENT_BT_CONTROLLER="${BURNIN_AGENT_BT_CONTROLLER:-hci0}"
  export BURNIN_IPERF3_PORT="${BURNIN_IPERF3_PORT:-5201}"
  export BURNIN_AGENT_HEARTBEAT_INTERVAL_SEC="${BURNIN_AGENT_HEARTBEAT_INTERVAL_SEC:-5}"
  export BURNIN_AGENT_METRICS_INTERVAL_SEC="${BURNIN_AGENT_METRICS_INTERVAL_SEC:-5}"
  export BURNIN_AGENT_LOG_FLUSH_INTERVAL_SEC="${BURNIN_AGENT_LOG_FLUSH_INTERVAL_SEC:-2}"
  export BURNIN_AGENT_COMMAND_POLL_INTERVAL_SEC="${BURNIN_AGENT_COMMAND_POLL_INTERVAL_SEC:-2}"
  export BURNIN_AGENT_COMMAND_WORKERS="${BURNIN_AGENT_COMMAND_WORKERS:-2}"
  export BURNIN_AGENT_EVENT_FLUSH_INTERVAL_SEC="${BURNIN_AGENT_EVENT_FLUSH_INTERVAL_SEC:-2}"
  export BURNIN_AGENT_COMMAND_PROGRESS_INTERVAL_SEC="${BURNIN_AGENT_COMMAND_PROGRESS_INTERVAL_SEC:-15}"
  export BURNIN_AGENT_LOG_SNAPSHOT_INTERVAL_SEC="${BURNIN_AGENT_LOG_SNAPSHOT_INTERVAL_SEC:-60}"
  export BURNIN_AGENT_DATA_DIR="${BURNIN_AGENT_DATA_DIR:-/var/lib/tspi-burnin}"
  export BURNIN_AGENT_MAX_SPOOL_FILES="${BURNIN_AGENT_MAX_SPOOL_FILES:-20000}"
  export BURNIN_AGENT_REQUEST_TIMEOUT_SEC="${BURNIN_AGENT_REQUEST_TIMEOUT_SEC:-5}"
  export BURNIN_AGENT_ARTIFACT_MAX_BYTES="${BURNIN_AGENT_ARTIFACT_MAX_BYTES:-262144}"
  export BURNIN_AGENT_BTMON_CAPTURE_SEC="${BURNIN_AGENT_BTMON_CAPTURE_SEC:-4}"
}

burnin_toml_string() {
  local value="${1:-}"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  printf '%s' "${value}"
}

burnin_toml_bool() {
  case "${1:-false}" in
    true|TRUE|True|1|yes|YES|Yes) printf 'true' ;;
    *) printf 'false' ;;
  esac
}

burnin_emit_agent_config() {
  local target="$1"
  local config_label="${BURNIN_CONFIG_FILE}"
  if [[ "${config_label}" == "${BURNIN_PROJECT_DIR}/"* ]]; then
    config_label="${config_label#${BURNIN_PROJECT_DIR}/}"
  fi
  mkdir -p "$(dirname "${target}")"
  cat >"${target}" <<EOF
# Generated from ${config_label}.
# Edit the unified config file instead of editing this file by hand.
board_id = "$(burnin_toml_string "${BURNIN_AGENT_BOARD_ID}")"
server_url = "$(burnin_toml_string "${BURNIN_SERVER_URL%/}")"
api_token = "$(burnin_toml_string "${BURNIN_API_TOKEN}")"

uplink_interface = "$(burnin_toml_string "${BURNIN_AGENT_UPLINK_INTERFACE}")"
require_uplink_interface = $(burnin_toml_bool "${BURNIN_AGENT_REQUIRE_UPLINK_INTERFACE}")
wifi_interface = "$(burnin_toml_string "${BURNIN_AGENT_WIFI_INTERFACE}")"
bt_controller = "$(burnin_toml_string "${BURNIN_AGENT_BT_CONTROLLER}")"
iperf3_port = ${BURNIN_IPERF3_PORT}

heartbeat_interval_sec = ${BURNIN_AGENT_HEARTBEAT_INTERVAL_SEC}
metrics_interval_sec = ${BURNIN_AGENT_METRICS_INTERVAL_SEC}
log_flush_interval_sec = ${BURNIN_AGENT_LOG_FLUSH_INTERVAL_SEC}
command_poll_interval_sec = ${BURNIN_AGENT_COMMAND_POLL_INTERVAL_SEC}
command_workers = ${BURNIN_AGENT_COMMAND_WORKERS}
event_flush_interval_sec = ${BURNIN_AGENT_EVENT_FLUSH_INTERVAL_SEC}
command_progress_interval_sec = ${BURNIN_AGENT_COMMAND_PROGRESS_INTERVAL_SEC}
log_snapshot_interval_sec = ${BURNIN_AGENT_LOG_SNAPSHOT_INTERVAL_SEC}

data_dir = "$(burnin_toml_string "${BURNIN_AGENT_DATA_DIR}")"
max_spool_files = ${BURNIN_AGENT_MAX_SPOOL_FILES}
request_timeout_sec = ${BURNIN_AGENT_REQUEST_TIMEOUT_SEC}
artifact_max_bytes = ${BURNIN_AGENT_ARTIFACT_MAX_BYTES}
btmon_capture_sec = ${BURNIN_AGENT_BTMON_CAPTURE_SEC}
EOF
}
