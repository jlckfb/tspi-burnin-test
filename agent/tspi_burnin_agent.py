#!/usr/bin/env python3
"""Board-side burn-in agent for TaishanPi 3M RK3576.

The agent intentionally depends only on the Python standard library and common
Ubuntu command-line tools. It collects system/WiFi/BT telemetry, executes cloud
scheduled iperf3 and Bluetooth probes, uploads data in near real time, and
spools failed uploads locally for later replay.
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import queue
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import contextlib
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:
    tomllib = None  # type: ignore[assignment]


DEFAULT_CONFIG = {
    "server_url": "http://127.0.0.1:8080",
    "board_id": "",
    "api_token": "",
    "uplink_interface": "end0",
    "require_uplink_interface": True,
    "wifi_interface": "wlan0",
    "bt_controller": "hci0",
    "iperf3_port": 5201,
    "heartbeat_interval_sec": 5.0,
    "metrics_interval_sec": 5.0,
    "log_flush_interval_sec": 2.0,
    "command_poll_interval_sec": 2.0,
    "command_workers": 2,
    "event_flush_interval_sec": 2.0,
    "command_progress_interval_sec": 15.0,
    "log_snapshot_interval_sec": 60.0,
    "data_dir": "/var/lib/tspi-burnin",
    "max_spool_files": 20000,
    "request_timeout_sec": 5.0,
    "artifact_max_bytes": 262144,
    "btmon_capture_sec": 4,
}


def now_ms() -> int:
    return int(time.time() * 1000)


def monotonic_ms() -> int:
    return int(time.monotonic() * 1000)


def command_output_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip()
    return str(value).strip()


def subprocess_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in ("NOTIFY_SOCKET", "WATCHDOG_PID", "WATCHDOG_USEC"):
        env.pop(key, None)
    return env


def run_cmd(args: list[str], timeout: float = 5.0) -> dict[str, Any]:
    started = monotonic_ms()
    try:
        proc = subprocess.run(
            args,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            env=subprocess_env(),
        )
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
            "duration_ms": monotonic_ms() - started,
        }
    except FileNotFoundError as exc:
        return {
            "ok": False,
            "returncode": 127,
            "stdout": "",
            "stderr": str(exc),
            "duration_ms": monotonic_ms() - started,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "returncode": 124,
            "stdout": command_output_text(exc.stdout),
            "stderr": command_output_text(exc.stderr) or "timeout",
            "duration_ms": monotonic_ms() - started,
        }


def read_text(path: str | Path, limit: int | None = None) -> str:
    data = Path(path).read_bytes()
    if limit is not None:
        data = data[:limit]
    return data.decode("utf-8", errors="replace")


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def atomic_write_text(path: Path, data: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def load_config(path: Path) -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    if path.exists():
        if tomllib is not None:
            with path.open("rb") as handle:
                loaded = tomllib.load(handle)
        else:
            loaded = parse_simple_toml(path.read_text(encoding="utf-8"))
        for key, value in loaded.items():
            if key in DEFAULT_CONFIG:
                config[key] = value
    if not config["board_id"]:
        config["board_id"] = derive_board_id(config)
    config["server_url"] = str(config["server_url"]).rstrip("/")
    return config


def derive_board_id(config: dict[str, Any]) -> str:
    data_dir = Path(str(config.get("data_dir") or DEFAULT_CONFIG["data_dir"]))
    board_id_path = data_dir / "board_id"
    if board_id_path.exists():
        value = board_id_path.read_text(encoding="utf-8").strip()
        if value:
            return value
    wifi_mac = iface_mac(str(config.get("wifi_interface") or "wlan0"))
    uplink_mac = iface_mac(str(config.get("uplink_interface") or "end0"))
    seed = wifi_mac or uplink_mac or uuid.uuid4().hex
    suffix = re.sub(r"[^0-9A-Fa-f]", "", seed).lower()[-8:]
    board_id = f"tspi3m-{suffix or uuid.uuid4().hex[:8]}"
    try:
        atomic_write_text(board_id_path, board_id + "\n")
    except Exception:
        pass
    return board_id


def iface_mac(iface: str) -> str | None:
    path = Path("/sys/class/net") / iface / "address"
    try:
        value = path.read_text(encoding="utf-8").strip()
        return value if value and value != "00:00:00:00:00:00" else None
    except Exception:
        return None


def iface_ipv4(iface: str) -> str | None:
    result = run_cmd(["ip", "-j", "-4", "addr", "show", "dev", iface], timeout=3)
    if not result["ok"]:
        return None
    try:
        data = json.loads(result["stdout"])
        for item in data:
            for addr in item.get("addr_info", []):
                if addr.get("family") == "inet" and addr.get("local"):
                    return str(addr["local"])
    except Exception:
        return None
    return None


def parse_simple_toml(text: str) -> dict[str, Any]:
    """Parse the flat key/value subset used by config.example.toml."""
    data: dict[str, Any] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        raw_value = raw_value.split("#", 1)[0].strip()
        if raw_value.startswith('"') and raw_value.endswith('"'):
            value: Any = raw_value[1:-1]
        elif raw_value.lower() in {"true", "false"}:
            value = raw_value.lower() == "true"
        else:
            try:
                value = int(raw_value)
            except ValueError:
                try:
                    value = float(raw_value)
                except ValueError:
                    value = raw_value
        data[key] = value
    return data


@dataclass
class LocalState:
    data_dir: Path
    boot_id: str = field(default_factory=lambda: read_text("/proc/sys/kernel/random/boot_id").strip())
    seq: int = 0
    completed_commands: set[str] = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def state_path(self) -> Path:
        return self.data_dir / "state.json"

    @property
    def last_state_path(self) -> Path:
        return self.data_dir / "last_state.json"

    def load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.seq = int(data.get("seq", 0))
            completed = list(data.get("completed_commands", []))
            self.completed_commands = set(completed[-2000:])
        except Exception:
            self.seq = 0
            self.completed_commands = set()

    def next_seq(self) -> int:
        with self.lock:
            self.seq += 1
            return self.seq

    def mark_command_done(self, command_id: str) -> None:
        with self.lock:
            self.completed_commands.add(command_id)
            if len(self.completed_commands) > 2000:
                self.completed_commands = set(sorted(self.completed_commands)[-1000:])
            atomic_write_json(
                self.state_path,
                {
                    "seq": self.seq,
                    "boot_id": self.boot_id,
                    "completed_commands": sorted(self.completed_commands),
                    "updated_at_ms": now_ms(),
                },
            )


class SdNotify:
    def __init__(self) -> None:
        self.socket_path = os.environ.get("NOTIFY_SOCKET")

    def notify(self, message: str) -> None:
        if not self.socket_path:
            return
        address = self.socket_path
        if address.startswith("@"):
            address = "\0" + address[1:]
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.connect(address)
            sock.sendall(message.encode("utf-8"))
        except OSError:
            pass
        finally:
            sock.close()

    def ready(self) -> None:
        self.notify("READY=1")

    def watchdog(self) -> None:
        self.notify("WATCHDOG=1")


class Uploader:
    def __init__(self, config: dict[str, Any], state: LocalState) -> None:
        self.config = config
        self.state = state
        self.spool_dir = Path(config["data_dir"]) / "spool"
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = float(config["request_timeout_sec"])
        self.max_spool_files = int(config["max_spool_files"])

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        token = str(self.config.get("api_token") or "")
        if token:
            headers["Authorization"] = f"Bearer {token}"
            headers["X-Burnin-Token"] = token
        return headers

    def _request(self, method: str, endpoint: str, payload: Any | None = None) -> Any:
        url = f"{self.config['server_url']}{endpoint}"
        data = None
        headers = self._headers()
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        source_address = self._source_address()
        if source_address is None and self._require_uplink():
            raise RuntimeError(f"uplink interface {self.config.get('uplink_interface')} has no IPv4 address")
        try:
            return self._http_request(method, url, data, headers, source_address)
        except Exception:
            if source_address is not None and not self._require_uplink():
                return self._http_request(method, url, data, headers, None)
            raise

    def _source_address(self) -> tuple[str, int] | None:
        iface = str(self.config.get("uplink_interface") or "")
        if not iface:
            return None
        ip = iface_ipv4(iface)
        return (ip, 0) if ip else None

    def _require_uplink(self) -> bool:
        return bool(self.config.get("require_uplink_interface", True))

    def _http_request(
        self,
        method: str,
        url: str,
        data: bytes | None,
        headers: dict[str, str],
        source_address: tuple[str, int] | None,
    ) -> Any:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"http", "https"}:
            raise RuntimeError(f"unsupported server_url scheme: {parsed.scheme}")
        host = parsed.hostname
        if not host:
            raise RuntimeError("server_url is missing host")
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        conn_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        conn = conn_cls(
            host,
            parsed.port,
            timeout=self.timeout,
            source_address=source_address,
        )
        try:
            conn.request(method, path, body=data, headers=headers)
            response = conn.getresponse()
            body = response.read()
            if response.status >= 400:
                raise RuntimeError(f"HTTP {response.status}: {body[:512]!r}")
            if not body:
                return None
            return json.loads(body.decode("utf-8"))
        finally:
            conn.close()

    def get(self, endpoint: str) -> Any:
        return self._request("GET", endpoint)

    def post(self, endpoint: str, payload: Any, spool_on_fail: bool = True) -> bool:
        try:
            self._request("POST", endpoint, payload)
            return True
        except Exception:
            if spool_on_fail:
                self.spool(endpoint, payload)
            return False

    def spool(self, endpoint: str, payload: Any) -> None:
        self._trim_spool()
        seq = self.state.next_seq()
        safe_endpoint = endpoint.strip("/").replace("/", "_") or "root"
        path = self.spool_dir / f"{now_ms()}_{seq}_{safe_endpoint}.json"
        atomic_write_json(path, {"endpoint": endpoint, "payload": payload})

    def flush_spool(self, limit: int = 100) -> int:
        sent = 0
        for path in sorted(self.spool_dir.glob("*.json"))[:limit]:
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
                endpoint = item["endpoint"]
                payload = item["payload"]
                self._request("POST", endpoint, payload)
                path.unlink()
                sent += 1
            except Exception:
                break
        return sent

    def _trim_spool(self) -> None:
        files = sorted(self.spool_dir.glob("*.json"))
        excess = len(files) - self.max_spool_files
        for path in files[: max(0, excess)]:
            try:
                path.unlink()
            except OSError:
                pass


class Collector:
    def __init__(self, config: dict[str, Any], bt_lock: threading.Lock | None = None) -> None:
        self.config = config
        self.uplink_interface = str(config["uplink_interface"])
        self.wifi_interface = str(config["wifi_interface"])
        self.bt_controller = str(config["bt_controller"])
        self.bt_lock = bt_lock
        self.bt_metrics_cache: dict[str, Any] = {}
        self.bt_metrics_cache_lock = threading.Lock()

    def collect(self) -> dict[str, Any]:
        return {
            "timestamp_ms": now_ms(),
            "monotonic_ms": monotonic_ms(),
            "system": self.system_metrics(),
            "thermal": self.thermal_metrics(),
            "network": self.network_metrics(),
            "wifi": self.wifi_metrics(),
            "bluetooth": self.bluetooth_metrics(),
            "process": self.process_metrics(),
        }

    def heartbeat(self) -> dict[str, Any]:
        wifi = self.wifi_metrics()
        network = self.network_metrics()
        return {
            "timestamp_ms": now_ms(),
            "monotonic_ms": monotonic_ms(),
            "hostname": socket.gethostname(),
            "wifi": wifi,
            "network": network,
            "bluetooth": self.bluetooth_metrics(),
        }

    def system_metrics(self) -> dict[str, Any]:
        meminfo = {}
        try:
            for line in read_text("/proc/meminfo").splitlines():
                key, raw = line.split(":", 1)
                meminfo[key] = int(raw.strip().split()[0]) * 1024
        except Exception:
            pass
        uptime = 0.0
        try:
            uptime = float(read_text("/proc/uptime").split()[0])
        except Exception:
            pass
        return {
            "uptime_sec": uptime,
            "loadavg": list(os.getloadavg()) if hasattr(os, "getloadavg") else [],
            "mem_total": meminfo.get("MemTotal"),
            "mem_available": meminfo.get("MemAvailable"),
            "swap_total": meminfo.get("SwapTotal"),
            "swap_free": meminfo.get("SwapFree"),
        }

    def thermal_metrics(self) -> list[dict[str, Any]]:
        zones = []
        for zone in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
            try:
                zones.append(
                    {
                        "name": read_text(zone / "type").strip(),
                        "temp_millic": int(read_text(zone / "temp").strip()),
                    }
                )
            except Exception:
                continue
        return zones

    def network_metrics(self) -> dict[str, Any]:
        interfaces: dict[str, Any] = {}
        ip_json = run_cmd(["ip", "-j", "addr"], timeout=3)
        if ip_json["ok"]:
            try:
                for item in json.loads(ip_json["stdout"]):
                    name = item.get("ifname")
                    if not name:
                        continue
                    addrs = []
                    for addr in item.get("addr_info", []):
                        if addr.get("family") == "inet":
                            addrs.append(addr.get("local"))
                    interfaces[name] = {"state": item.get("operstate"), "ipv4": addrs}
            except Exception:
                pass
        for iface_dir in Path("/sys/class/net").glob("*"):
            iface = iface_dir.name
            stats_dir = iface_dir / "statistics"
            if not stats_dir.exists():
                continue
            entry = interfaces.setdefault(iface, {})
            for stat in ("rx_bytes", "tx_bytes", "rx_packets", "tx_packets", "rx_errors", "tx_errors", "rx_dropped", "tx_dropped"):
                try:
                    entry[stat] = int(read_text(stats_dir / stat).strip())
                except Exception:
                    pass
            try:
                entry["mac"] = read_text(iface_dir / "address").strip()
            except Exception:
                pass
        default_route = run_cmd(["ip", "route", "show", "default"], timeout=3)
        return {
            "interfaces": interfaces,
            "uplink_interface": self.uplink_interface,
            "wifi_interface": self.wifi_interface,
            "uplink_ip": first_nonempty(interfaces.get(self.uplink_interface, {}).get("ipv4")),
            "wifi_ip": first_nonempty(interfaces.get(self.wifi_interface, {}).get("ipv4")),
            "uplink_ready": bool(first_nonempty(interfaces.get(self.uplink_interface, {}).get("ipv4"))),
            "default_route_raw": default_route["stdout"][-2048:],
        }

    def wifi_metrics(self) -> dict[str, Any]:
        data: dict[str, Any] = {"interface": self.wifi_interface, "ipv4": iface_ipv4(self.wifi_interface)}
        iw_link = run_cmd(["iw", "dev", self.wifi_interface, "link"], timeout=3)
        data["link_raw"] = iw_link["stdout"][-2048:]
        data["connected"] = "Connected to" in iw_link["stdout"]
        if data["connected"]:
            data.update(parse_iw_link(iw_link["stdout"]))
        iw_dev = run_cmd(["iw", "dev"], timeout=3)
        data["txpower_dbm"] = parse_txpower(iw_dev["stdout"], self.wifi_interface)
        nmcli = run_cmd(["nmcli", "-t", "-f", "DEVICE,STATE,CONNECTION", "device", "status"], timeout=3)
        if nmcli["ok"]:
            data["nmcli_device_status"] = nmcli["stdout"]
        station = run_cmd(["iw", "dev", self.wifi_interface, "station", "dump"], timeout=3)
        if station["stdout"]:
            data["station_dump_raw"] = station["stdout"][-4096:]
            data.update(parse_iw_station_dump(station["stdout"]))
        survey = run_cmd(["iw", "dev", self.wifi_interface, "survey", "dump"], timeout=3)
        if survey["stdout"]:
            data["survey_dump_raw"] = survey["stdout"][-4096:]
        data["proc_wireless"] = parse_proc_wireless(self.wifi_interface)
        return data

    def bluetooth_metrics(self) -> dict[str, Any]:
        info: dict[str, Any] = {"controller": self.bt_controller}
        acquired = True
        if self.bt_lock is not None:
            acquired = self.bt_lock.acquire(blocking=False)
        if not acquired:
            with self.bt_metrics_cache_lock:
                info.update(self.bt_metrics_cache)
            if not self.bt_metrics_cache:
                hciconfig = run_cmd(["hciconfig", self.bt_controller, "-a"], timeout=2)
                info["available"] = hciconfig["ok"]
                info["hciconfig_raw"] = hciconfig["stdout"][-2048:]
                info.update(parse_hciconfig(hciconfig["stdout"]))
            info["controller"] = self.bt_controller
            info.setdefault("available", None)
            info["busy"] = True
            info["stale"] = bool(self.bt_metrics_cache)
            return info
        try:
            hciconfig = run_cmd(["hciconfig", self.bt_controller, "-a"], timeout=3)
            info["available"] = hciconfig["ok"]
            info["hciconfig_raw"] = hciconfig["stdout"][-2048:]
            info.update(parse_hciconfig(hciconfig["stdout"]))
            btmgmt = run_cmd(["btmgmt", "info"], timeout=3)
            info["btmgmt_ok"] = btmgmt["ok"]
            info["btmgmt_raw"] = btmgmt["stdout"][-2048:]
            rfkill = run_cmd(["rfkill", "list"], timeout=3)
            info["rfkill_raw"] = rfkill["stdout"][-2048:]
            info["busy"] = False
            info["stale"] = False
            with self.bt_metrics_cache_lock:
                self.bt_metrics_cache = dict(info)
            return info
        finally:
            if self.bt_lock is not None:
                self.bt_lock.release()

    def process_metrics(self) -> dict[str, Any]:
        return {
            "iperf3_path": shutil.which("iperf3"),
            "ping_path": shutil.which("ping"),
            "l2ping_path": shutil.which("l2ping"),
            "l2test_path": shutil.which("l2test"),
            "btmon_path": shutil.which("btmon"),
            "agent_pid": os.getpid(),
            "python": sys.version.split()[0],
        }


def first_nonempty(values: Any) -> str | None:
    if isinstance(values, list):
        for value in values:
            if value:
                return str(value)
    if values:
        return str(values)
    return None


def parse_iw_link(output: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    patterns = {
        "bssid": r"Connected to ([0-9a-f:]+)",
        "ssid": r"SSID:\s*(.+)",
        "freq_mhz": r"freq:\s*(\d+)",
        "signal_dbm": r"signal:\s*(-?\d+)\s*dBm",
        "rx_bitrate": r"rx bitrate:\s*([^\n]+)",
        "tx_bitrate": r"tx bitrate:\s*([^\n]+)",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, output)
        if not match:
            continue
        value: Any = match.group(1).strip()
        if key in {"freq_mhz", "signal_dbm"}:
            value = int(value)
        data[key] = value
    return data


def parse_iw_station_dump(output: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    patterns = {
        "tx_retries": r"\btx retries:\s*(\d+)",
        "tx_failed": r"\btx failed:\s*(\d+)",
        "rx_packets": r"\brx packets:\s*(\d+)",
        "tx_packets": r"\btx packets:\s*(\d+)",
        "beacon_loss": r"\bbeacon loss:\s*(\d+)",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, output)
        if match:
            data[key] = int(match.group(1))
    return data


def parse_proc_wireless(iface: str) -> dict[str, Any]:
    path = Path("/proc/net/wireless")
    if not path.exists():
        return {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped.startswith(f"{iface}:"):
                continue
            parts = stripped.replace(":", " ").split()
            if len(parts) >= 5:
                return {
                    "status": parts[1],
                    "link_quality": float(parts[2].strip(".")),
                    "level_dbm": float(parts[3].strip(".")),
                    "noise_dbm": float(parts[4].strip(".")),
                }
    except Exception:
        return {}
    return {}


def parse_txpower(output: str, iface: str) -> float | None:
    current_iface = None
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("Interface "):
            current_iface = stripped.split(maxsplit=1)[1]
        if current_iface == iface and stripped.startswith("txpower "):
            try:
                return float(stripped.split()[1])
            except Exception:
                return None
    return None


def parse_hciconfig(output: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    match = re.search(r"BD Address:\s*([0-9A-F:]+)", output)
    if match:
        data["address"] = match.group(1)
    match = re.search(r"RX bytes:(\d+).*?errors:(\d+)", output)
    if match:
        data["rx_bytes"] = int(match.group(1))
        data["rx_errors"] = int(match.group(2))
    match = re.search(r"TX bytes:(\d+).*?errors:(\d+)", output)
    if match:
        data["tx_bytes"] = int(match.group(1))
        data["tx_errors"] = int(match.group(2))
    data["up"] = "UP RUNNING" in output
    data["pscan"] = "PSCAN" in output
    data["iscan"] = "ISCAN" in output
    return data


def prepare_bluetooth(controller: str) -> list[dict[str, Any]]:
    steps = [
        ["hciconfig", controller, "up"],
        ["hciconfig", controller, "piscan"],
    ]
    return [{"cmd": " ".join(args), "result": run_cmd(args, timeout=8)} for args in steps]


def l2test_connected(result: dict[str, Any]) -> bool:
    output = f"{result.get('stdout') or ''}\n{result.get('stderr') or ''}"
    return "Connected to " in output and "Can't connect" not in output


def l2test_summary(result: dict[str, Any], duration_sec: int) -> dict[str, Any]:
    output = f"{result.get('stdout') or ''}\n{result.get('stderr') or ''}"
    duration_ms = int(result.get("duration_ms") or 0)
    ran_long_enough = duration_ms >= min(duration_sec * 800, max(5000, duration_sec * 1000 - 2000))
    connected = "Connected to " in output and "Can't connect" not in output
    disconnect_seen = "Disconnect:" in output
    sample_count = len(re.findall(r"\b\d+\s+bytes in\b", output))
    return {
        "connected": connected,
        "reset_by_peer": "Connection reset by peer" in output,
        "connection_aborted": "Software caused connection abort" in output,
        "connect_failed": "Can't connect" in output,
        "disconnect_seen": disconnect_seen,
        "sample_count": sample_count,
        "returncode": result.get("returncode"),
        "duration_ms": duration_ms,
        "ran_long_enough": ran_long_enough,
        "activity_seen": bool(connected or disconnect_seen or sample_count > 0),
    }


def command_resource(command_type: Any) -> str | None:
    value = str(command_type or "")
    if value.startswith("wifi_"):
        return "wifi"
    if value.startswith("bt_"):
        return "bt"
    return None


class CommandRunner:
    def __init__(
        self,
        config: dict[str, Any],
        state: LocalState,
        uploader: Uploader,
        log_queue: queue.Queue[dict[str, Any]],
        event_queue: queue.Queue[dict[str, Any]],
        bt_lock: threading.Lock | None = None,
    ) -> None:
        self.config = config
        self.state = state
        self.uploader = uploader
        self.log_queue = log_queue
        self.event_queue = event_queue
        self.bt_lock = bt_lock
        self.active: set[str] = set()
        self.active_resources: set[str] = set()
        self.lock = threading.Lock()

    def maybe_start(self, command: dict[str, Any]) -> None:
        command_id = str(command.get("id") or "")
        if not command_id:
            return
        resource = command_resource(command.get("type"))
        with self.lock:
            if command_id in self.active or command_id in self.state.completed_commands:
                return
            if resource and resource in self.active_resources:
                self._event(
                    "command_deferred",
                    command_id=command_id,
                    severity="warn",
                    message=f"{resource} resource busy",
                    data={"command": command, "resource": resource},
                )
                return
            if len(self.active) >= int(self.config.get("command_workers", 1)):
                return
            self.active.add(command_id)
            if resource:
                self.active_resources.add(resource)
        thread = threading.Thread(target=self._run_guarded, args=(command,), daemon=True)
        thread.start()

    def _run_guarded(self, command: dict[str, Any]) -> None:
        command_id = str(command["id"])
        resource = command_resource(command.get("type"))
        started_at = now_ms()
        stop_progress = threading.Event()
        progress_thread = threading.Thread(target=self._progress_loop, args=(command, started_at, stop_progress), daemon=True)
        try:
            self._log("info", "starting command", command=command)
            self._event(
                "command_started",
                command_id=command_id,
                message="command started",
                data={"command": command, "resource": resource},
            )
            progress_thread.start()
            result = self.run(command)
            self._stamp_result(result)
            enrich_failure(result)
            self.uploader.post("/api/v1/results/batch", {"results": [result]})
            self._log("info", "command finished", command_id=command_id, status=result.get("status"))
            self._event(
                "command_finished",
                command_id=command_id,
                severity="error" if result.get("status") not in {"passed", "unsupported"} else "info",
                message=str(result.get("failure_reason") or result.get("status") or "command finished"),
                data={"result": dashboard_result_for_agent(result), "resource": resource},
            )
        except Exception as exc:
            result = {
                "command_id": command_id,
                "type": command.get("type"),
                "status": "agent_error",
                "error": repr(exc),
                "timestamp_ms": now_ms(),
            }
            self._stamp_result(result)
            enrich_failure(result)
            self.uploader.post("/api/v1/results/batch", {"results": [result]})
            self._log("error", "command failed in agent", command_id=command_id, error=repr(exc))
            self._event(
                "command_finished",
                command_id=command_id,
                severity="error",
                message=str(result.get("failure_reason") or repr(exc)),
                data={"result": dashboard_result_for_agent(result), "resource": resource},
            )
        finally:
            stop_progress.set()
            progress_thread.join(timeout=1.0)
            self.state.mark_command_done(command_id)
            with self.lock:
                self.active.discard(command_id)
                if resource:
                    self.active_resources.discard(resource)

    def _stamp_result(self, result: dict[str, Any]) -> None:
        result.setdefault("board_id", self.config["board_id"])
        result.setdefault("boot_id", self.state.boot_id)
        result.setdefault("seq", self.state.next_seq())
        result.setdefault("timestamp_ms", result.get("finished_at_ms") or now_ms())

    def _progress_loop(self, command: dict[str, Any], started_at: int, stop_event: threading.Event) -> None:
        interval = max(5.0, float(self.config.get("command_progress_interval_sec") or 15.0))
        command_id = str(command.get("id") or "")
        while not stop_event.wait(interval):
            self._event(
                "command_progress",
                command_id=command_id,
                message="command running",
                data={
                    "command": command,
                    "resource": command_resource(command.get("type")),
                    "started_at_ms": started_at,
                    "runtime_sec": round((now_ms() - started_at) / 1000, 1),
                    "state": runtime_snapshot(self.config, command),
                },
            )

    def _event(
        self,
        event_type: str,
        command_id: str | None = None,
        severity: str = "info",
        message: str = "",
        data: dict[str, Any] | None = None,
    ) -> None:
        self.event_queue.put(
            {
                "timestamp_ms": now_ms(),
                "event_type": event_type,
                "command_id": command_id,
                "severity": severity,
                "message": message,
                "data": data or {},
            }
        )

    def run(self, command: dict[str, Any]) -> dict[str, Any]:
        command_type = command.get("type")
        if command_type in {
            "wifi_iperf3_tcp",
            "wifi_iperf3_udp",
            "wifi_tcp_single",
            "wifi_tcp_multi",
            "wifi_tcp_reverse",
            "wifi_tcp_bidir",
            "wifi_udp_flood",
            "wifi_udp_small",
            "wifi_udp_large",
        }:
            return self.run_iperf3(command)
        if command_type == "wifi_ping":
            return self.run_wifi_ping(command)
        if command_type in {"bt_ble_probe", "bt_ble_advertise", "bt_ble_scan"}:
            return self.run_with_bt_lock(self.run_bt_ble, command)
        if command_type == "bt_bredr_inquiry":
            return self.run_with_bt_lock(self.run_bt_bredr_inquiry, command)
        if command_type == "bt_l2ping":
            return self.run_with_bt_lock(self.run_bt_l2ping, command)
        if command_type == "bt_l2test":
            return self.run_with_bt_lock(self.run_bt_l2test, command)
        return {
            "command_id": command.get("id"),
            "type": command_type,
            "status": "unsupported",
            "timestamp_ms": now_ms(),
        }

    def run_with_bt_lock(self, fn: Any, command: dict[str, Any]) -> dict[str, Any]:
        if self.bt_lock is None:
            return fn(command)
        acquired = self.bt_lock.acquire(timeout=5)
        if not acquired:
            return {
                "command_id": command.get("id"),
                "type": command.get("type"),
                "status": "failed",
                "started_at_ms": now_ms(),
                "finished_at_ms": now_ms(),
                "summary": {"error": "bt resource busy"},
                "error": "bt resource busy",
            }
        try:
            return fn(command)
        finally:
            self.bt_lock.release()

    def run_iperf3(self, command: dict[str, Any]) -> dict[str, Any]:
        peer_ip = str(command["peer_ip"])
        port = int(command.get("port", self.config["iperf3_port"]))
        duration = int(command.get("duration_sec", 60))
        command_type = str(command.get("type") or "")
        preflight = self.wifi_preflight(peer_ip)
        if not preflight["ok"]:
            return {
                "command_id": command.get("id"),
                "type": command_type,
                "status": "failed",
                "started_at_ms": now_ms(),
                "finished_at_ms": now_ms(),
                "peer_ip": peer_ip,
                "preflight": preflight,
                "summary": {"wifi_path_ok": False},
                "error": "wifi preflight failed",
            }
        local_ip = str(preflight["local_wifi_ip"])
        started = now_ms()
        base_args = ["iperf3", "-c", peer_ip, "-p", str(port), "-J", "-B", local_ip]
        if iperf3_supports_bind_dev():
            base_args.extend(["--bind-dev", str(self.config["wifi_interface"])])
        is_udp = command_type.startswith("wifi_udp") or command_type == "wifi_iperf3_udp"
        udp_length = command.get("length")
        if is_udp:
            if command_type == "wifi_udp_small":
                udp_length = udp_length or 256
            elif command_type == "wifi_udp_large":
                udp_length = udp_length or 1400

        def build_args(test_duration: int, bandwidth: str | None = None) -> list[str]:
            args = [*base_args, "-t", str(max(1, int(test_duration)))]
            if command_type in {"wifi_iperf3_tcp", "wifi_tcp_multi"}:
                args.extend(["-P", str(int(command.get("parallel", 4)))])
            elif command_type == "wifi_tcp_single":
                args.extend(["-P", "1"])
            elif command_type == "wifi_tcp_reverse":
                args.extend(["-R", "-P", str(int(command.get("parallel", 4)))])
            elif command_type == "wifi_tcp_bidir":
                args.extend(["--bidir", "-P", str(int(command.get("parallel", 4)))])
            else:
                args.extend(["-u", "-b", str(bandwidth if bandwidth is not None else command.get("bandwidth", "0"))])
                if udp_length:
                    args.extend(["-l", str(udp_length)])
            return args

        def run_iperf3_json(test_args: list[str], test_duration: int) -> tuple[dict[str, Any], Any, int, str]:
            result: dict[str, Any] = {}
            parsed = None
            retry_reason = ""
            attempts = 0
            for attempt in range(1, 4):
                attempts = attempt
                result = run_cmd(test_args, timeout=max(10, int(test_duration) + 30))
                parsed = None
                if result["stdout"]:
                    try:
                        parsed = json.loads(result["stdout"])
                    except json.JSONDecodeError:
                        parsed = None
                raw_error = str(parsed.get("error") or "") if isinstance(parsed, dict) else ""
                if result["ok"] or "server is busy" not in raw_error.lower() or attempt == 3:
                    break
                retry_reason = raw_error
                time.sleep(10)
            return result, parsed, attempts, retry_reason

        selected_bandwidth = str(command.get("bandwidth", "0"))
        adaptive_probe_summary: list[dict[str, Any]] = []
        adaptive_max_loss = float(command.get("adaptive_max_loss_percent", 5) or 5)
        adaptive_probe_sec = max(1, int(command.get("adaptive_probe_sec", 5) or 5))
        adaptive_enabled = bool(command.get("adaptive")) and is_udp
        command_started = time.monotonic()
        if adaptive_enabled:
            rates = parse_rate_list(command.get("adaptive_rates"), selected_bandwidth)
            best_ok_rate = ""
            lowest_loss_rate = ""
            lowest_loss_value: float | None = None
            for rate in rates:
                probe_args = build_args(adaptive_probe_sec, rate)
                probe_result, probe_parsed, probe_attempts, _probe_retry_reason = run_iperf3_json(probe_args, adaptive_probe_sec)
                probe_summary = summarize_iperf3(probe_parsed)
                loss = number_or_none(probe_summary.get("lost_percent"))
                adaptive_probe_summary.append(
                    {
                        "bandwidth": rate,
                        "status": "passed" if probe_result["ok"] and probe_parsed else "failed",
                        "lost_percent": loss,
                        "received_bits_per_second": probe_summary.get("received_bits_per_second"),
                        "bits_per_second": probe_summary.get("bits_per_second"),
                        "returncode": probe_result.get("returncode"),
                        "attempts": probe_attempts,
                        "error": (probe_parsed or {}).get("error") if isinstance(probe_parsed, dict) else "",
                    }
                )
                if loss is not None and (lowest_loss_value is None or loss < lowest_loss_value):
                    lowest_loss_value = loss
                    lowest_loss_rate = rate
                if probe_result["ok"] and probe_parsed and loss is not None and loss <= adaptive_max_loss:
                    best_ok_rate = rate
            selected_bandwidth = best_ok_rate or lowest_loss_rate or (rates[0] if rates else selected_bandwidth)

        elapsed_sec = int(time.monotonic() - command_started)
        final_duration = duration
        if adaptive_enabled:
            final_duration = max(30, duration - elapsed_sec - 5)
        args = build_args(final_duration, selected_bandwidth if is_udp else None)
        result, parsed, attempts, retry_reason = run_iperf3_json(args, final_duration)
        summary = summarize_iperf3(parsed)
        probe_text = ", ".join(
            f"{item['bandwidth']}:{'-' if item.get('lost_percent') is None else round(float(item['lost_percent']), 1)}%"
            for item in adaptive_probe_summary
        )
        summary.update(
            {
                "wifi_path_ok": preflight["ok"],
                "local_wifi_ip": local_ip,
                "route_dev": preflight.get("route_dev"),
                "bound_dev": self.config["wifi_interface"],
                "iperf3_attempts": attempts,
                "iperf3_retry_reason": retry_reason,
                "udp_bandwidth": selected_bandwidth if is_udp else "",
                "udp_length": udp_length if is_udp else None,
                "adaptive_udp": adaptive_enabled,
                "adaptive_selected_bandwidth": selected_bandwidth if adaptive_enabled else "",
                "adaptive_probe_sec": adaptive_probe_sec if adaptive_enabled else None,
                "adaptive_max_loss_percent": adaptive_max_loss if adaptive_enabled else None,
                "adaptive_probe_count": len(adaptive_probe_summary),
                "adaptive_probe_summary": probe_text,
            }
        )
        udp_loss_error = ""
        if is_udp:
            final_loss = number_or_none(summary.get("lost_percent"))
            udp_max_loss = float(command.get("max_loss_percent", adaptive_max_loss) or adaptive_max_loss)
            summary["udp_max_loss_percent"] = udp_max_loss
            summary["udp_loss_exceeded"] = bool(final_loss is not None and final_loss > udp_max_loss)
            if summary["udp_loss_exceeded"]:
                udp_loss_error = f"udp loss {final_loss:.2f}% exceeds {udp_max_loss:.2f}%"
        status = "passed" if result["ok"] and parsed and not udp_loss_error else "failed"
        return {
            "command_id": command.get("id"),
            "type": command_type,
            "status": status,
            "started_at_ms": started,
            "finished_at_ms": now_ms(),
            "peer_ip": peer_ip,
            "local_wifi_ip": local_ip,
            "preflight": preflight,
            "args": redact_args(args),
            "returncode": result["returncode"],
            "error": udp_loss_error or ((parsed or {}).get("error") if isinstance(parsed, dict) else ""),
            "stderr": result["stderr"][-4096:],
            "summary": summary,
            "raw": parsed,
        }

    def run_wifi_ping(self, command: dict[str, Any]) -> dict[str, Any]:
        peer_ip = str(command["peer_ip"])
        count = int(command.get("count", 20))
        preflight = self.wifi_preflight(peer_ip)
        args = ["ping", "-I", str(self.config["wifi_interface"]), "-c", str(count), "-W", "2", peer_ip]
        started = now_ms()
        result = run_cmd(args, timeout=max(10, count * 3))
        summary = parse_ping_summary(result["stdout"])
        summary.update({"wifi_path_ok": preflight["ok"], "route_dev": preflight.get("route_dev")})
        return {
            "command_id": command.get("id"),
            "type": command.get("type"),
            "status": "passed" if result["ok"] else "failed",
            "started_at_ms": started,
            "finished_at_ms": now_ms(),
            "peer_ip": peer_ip,
            "preflight": preflight,
            "args": redact_args(args),
            "returncode": result["returncode"],
            "summary": summary,
            "raw": {"stdout": result["stdout"][-4096:], "stderr": result["stderr"][-4096:]},
        }

    def wifi_preflight(self, peer_ip: str) -> dict[str, Any]:
        iface = str(self.config["wifi_interface"])
        local_ip = iface_ipv4(iface)
        link = run_cmd(["iw", "dev", iface, "link"], timeout=3)
        route = run_cmd(["ip", "route", "get", peer_ip, "from", local_ip or "0.0.0.0"], timeout=3)
        route_dev = parse_route_dev(route["stdout"])
        ok = bool(local_ip) and "Connected to" in link["stdout"] and route["ok"] and route_dev == iface
        return {
            "ok": ok,
            "wifi_interface": iface,
            "local_wifi_ip": local_ip,
            "peer_ip": peer_ip,
            "route_dev": route_dev,
            "route_raw": route["stdout"][-2048:],
            "link_connected": "Connected to" in link["stdout"],
            "link_raw": link["stdout"][-2048:],
        }

    def run_bt_ble(self, command: dict[str, Any]) -> dict[str, Any]:
        duration = int(command.get("duration_sec", 20))
        command_type = str(command.get("type") or "bt_ble_probe")
        role = str(command.get("role") or ("advertise" if command_type == "bt_ble_advertise" else "scan"))
        started = now_ms()
        outputs = prepare_bluetooth(str(self.config["bt_controller"]))
        critical_ok = False
        ran_long_enough = False
        if role == "advertise":
            outputs.append({"cmd": "bluetoothctl system-alias", "result": run_cmd(["bluetoothctl", "system-alias", str(self.config["board_id"])], timeout=5)})
            advertise_on = run_cmd(["bluetoothctl", "--timeout", str(duration), "advertise", "on"], timeout=duration + 8)
            outputs.append({"cmd": "bluetoothctl advertise on", "result": advertise_on})
            ran_long_enough = int(advertise_on.get("duration_ms") or 0) >= max(5000, duration * 900)
            critical_ok = bool(advertise_on.get("ok")) or bool(advertise_on.get("returncode") == 124 and ran_long_enough)
            outputs.append({"cmd": "bluetoothctl advertise off", "result": run_cmd(["bluetoothctl", "advertise", "off"], timeout=5)})
        else:
            scan = run_cmd(["bluetoothctl", "--timeout", str(duration), "scan", "on"], timeout=duration + 8)
            outputs.append({"cmd": "bluetoothctl scan on", "result": scan})
            ran_long_enough = int(scan.get("duration_ms") or 0) >= max(5000, duration * 900)
            critical_ok = bool(scan.get("ok")) or bool(scan.get("returncode") == 124 and ran_long_enough)
        status_result = run_cmd(["hciconfig", str(self.config["bt_controller"]), "-a"], timeout=5)
        outputs.append({"cmd": "hciconfig status", "result": status_result})
        btmon = capture_btmon(str(self.config["bt_controller"]), int(self.config.get("btmon_capture_sec") or 0))
        if btmon:
            outputs.append({"cmd": "btmon sample", "result": btmon})
        controller_up = parse_hciconfig(str(status_result.get("stdout") or "")).get("up")
        ok = critical_ok and bool(controller_up)
        return {
            "command_id": command.get("id"),
            "type": command_type,
            "status": "passed" if ok else "failed",
            "role": role,
            "started_at_ms": started,
            "finished_at_ms": now_ms(),
            "summary": {
                "role": role,
                "duration_sec": duration,
                "controller_up": bool(controller_up),
                "ran_long_enough": ran_long_enough,
                "btmon_captured": bool(btmon),
            },
            "raw": outputs,
        }

    def run_bt_bredr_inquiry(self, command: dict[str, Any]) -> dict[str, Any]:
        duration = int(command.get("duration_sec", 20))
        started = now_ms()
        outputs = prepare_bluetooth(str(self.config["bt_controller"]))
        scan = run_cmd(["hcitool", "-i", str(self.config["bt_controller"]), "scan", "--flush"], timeout=duration + 3)
        outputs.append({"cmd": "hcitool scan", "result": scan})
        status = run_cmd(["hciconfig", str(self.config["bt_controller"]), "-a"], timeout=5)
        outputs.append({"cmd": "hciconfig status", "result": status})
        found = parse_bt_scan_count(scan["stdout"])
        scan_timed_out = scan.get("returncode") == 124
        scan_ran_long_enough = int(scan.get("duration_ms") or 0) >= max(5000, duration * 900)
        controller_up = parse_hciconfig(str(status.get("stdout") or "")).get("up")
        ok = bool(scan.get("ok")) or bool(scan_timed_out and scan_ran_long_enough and controller_up)
        return {
            "command_id": command.get("id"),
            "type": command.get("type"),
            "status": "passed" if ok else "failed",
            "started_at_ms": started,
            "finished_at_ms": now_ms(),
            "summary": {
                "duration_sec": duration,
                "devices_found": found,
                "scan_timed_out": scan_timed_out,
                "scan_ran_long_enough": scan_ran_long_enough,
                "controller_up": bool(controller_up),
            },
            "raw": outputs,
        }

    def run_bt_l2ping(self, command: dict[str, Any]) -> dict[str, Any]:
        peer_mac = str(command.get("peer_bt_mac") or "")
        count = int(command.get("count", 20))
        size = int(command.get("size", 600))
        started = now_ms()
        if not peer_mac:
            return {
                "command_id": command.get("id"),
                "type": command.get("type"),
                "status": "failed",
                "started_at_ms": started,
                "finished_at_ms": now_ms(),
                "summary": {"error": "missing peer_bt_mac"},
            }
        args = ["l2ping", "-i", str(self.config["bt_controller"]), "-s", str(size), "-c", str(count), "-v", peer_mac]
        outputs = prepare_bluetooth(str(self.config["bt_controller"]))
        outputs.append({"cmd": "l2ping", "result": run_cmd(args, timeout=max(10, count * 3))})
        summary = parse_l2ping_summary(outputs[-1]["result"]["stdout"])
        summary.update({"peer_bt_mac": peer_mac, "size": size, "count": count})
        return {
            "command_id": command.get("id"),
            "type": command.get("type"),
            "status": "passed" if outputs[-1]["result"]["ok"] else "failed",
            "started_at_ms": started,
            "finished_at_ms": now_ms(),
            "peer_bt_mac": peer_mac,
            "summary": summary,
            "raw": outputs,
        }

    def run_bt_l2test(self, command: dict[str, Any]) -> dict[str, Any]:
        role = str(command.get("role") or "client")
        peer_mac = str(command.get("peer_bt_mac") or "")
        duration = int(command.get("duration_sec", 20))
        psm = str(command.get("psm") or "").strip()
        packet_bytes = int(command.get("bytes", 600))
        frames = int(command.get("frames", 0) or 0)
        delay_ms = max(0, int(command.get("delay_ms", 0) or 0))
        client_delay_sec = max(0.0, float(command.get("client_delay_sec", 6)))
        startup_grace_sec = max(0.0, float(command.get("startup_grace_sec", 10)))
        started = now_ms()
        outputs = prepare_bluetooth(str(self.config["bt_controller"]))
        if role == "server":
            args = ["l2test", "-r", "-i", str(self.config["bt_controller"])]
            if psm:
                args.extend(["-P", psm])
            if packet_bytes > 0:
                args.extend(["-b", str(packet_bytes)])
            result = run_cmd(args, timeout=duration + startup_grace_sec)
            test_summary = l2test_summary(result, duration)
            ok = result["ok"] or (
                result["returncode"] == 124
                and test_summary["ran_long_enough"]
                and test_summary["activity_seen"]
            )
        elif peer_mac:
            args = ["l2test", "-s", "-i", str(self.config["bt_controller"])]
            if psm:
                args.extend(["-P", psm])
            if packet_bytes > 0:
                args.extend(["-b", str(packet_bytes)])
            if frames > 0:
                args.extend(["-N", str(frames)])
            if delay_ms > 0:
                args.extend(["-D", str(delay_ms)])
            args.append(peer_mac)
            if client_delay_sec:
                time.sleep(client_delay_sec)
            result = run_cmd(args, timeout=duration)
            test_summary = l2test_summary(result, duration)
            link_error = test_summary["reset_by_peer"] or test_summary["connection_aborted"] or test_summary["connect_failed"]
            ok = (result["ok"] or (test_summary["ran_long_enough"] and test_summary["activity_seen"])) and not link_error
        else:
            args = ["l2test"]
            result = {"ok": False, "returncode": 2, "stdout": "", "stderr": "missing peer_bt_mac", "duration_ms": 0}
            test_summary = l2test_summary(result, duration)
            ok = False
        outputs.append({"cmd": " ".join(args), "result": result})
        summary = {
            "role": role,
            "peer_bt_mac": peer_mac,
            "duration_sec": duration,
            "psm": psm or "default",
            "bytes": packet_bytes,
            "frames": frames,
            "delay_ms": delay_ms,
            "client_delay_sec": client_delay_sec,
            "startup_grace_sec": startup_grace_sec,
        }
        summary.update(test_summary)
        return {
            "command_id": command.get("id"),
            "type": command.get("type"),
            "status": "passed" if ok else "failed",
            "started_at_ms": started,
            "finished_at_ms": now_ms(),
            "peer_bt_mac": peer_mac,
            "summary": summary,
            "raw": outputs,
        }

    def _log(self, level: str, message: str, **fields: Any) -> None:
        event = {"timestamp_ms": now_ms(), "level": level, "message": message}
        event.update(fields)
        self.log_queue.put(event)


def summarize_iperf3(parsed: Any) -> dict[str, Any]:
    if not isinstance(parsed, dict):
        return {}
    end = parsed.get("end", {})
    summary: dict[str, Any] = {}
    if "sum_sent" in end:
        sent = end.get("sum_sent") or {}
        summary["sent_bits_per_second"] = sent.get("bits_per_second")
        summary["retransmits"] = sent.get("retransmits")
        summary["sent_bytes"] = sent.get("bytes")
    if "sum_received" in end:
        received = end.get("sum_received") or {}
        summary["received_bits_per_second"] = received.get("bits_per_second")
        summary["received_bytes"] = received.get("bytes")
    if "sum" in end:
        udp = end.get("sum") or {}
        summary["bits_per_second"] = udp.get("bits_per_second")
        summary["jitter_ms"] = udp.get("jitter_ms")
        summary["lost_packets"] = udp.get("lost_packets")
        summary["packets"] = udp.get("packets")
        summary["lost_percent"] = udp.get("lost_percent")
    return summary


def number_or_none(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_rate_list(value: Any, fallback: str) -> list[str]:
    if isinstance(value, list):
        rates = [str(item).strip() for item in value if str(item).strip()]
    else:
        rates = [part.strip() for part in str(value or "").split(",") if part.strip()]
    if not rates and fallback:
        rates = [fallback]
    return rates


_IPERF3_BIND_DEV_SUPPORT: bool | None = None


def iperf3_supports_bind_dev() -> bool:
    global _IPERF3_BIND_DEV_SUPPORT
    if _IPERF3_BIND_DEV_SUPPORT is None:
        result = run_cmd(["iperf3", "--help"], timeout=3)
        _IPERF3_BIND_DEV_SUPPORT = "--bind-dev" in result["stdout"] or "--bind-dev" in result["stderr"]
    return bool(_IPERF3_BIND_DEV_SUPPORT)


def cleanup_stale_iperf3_clients() -> int:
    killed = 0
    proc_root = Path("/proc")
    for cmdline_path in proc_root.glob("[0-9]*/cmdline"):
        with contextlib.suppress(Exception):
            raw = cmdline_path.read_bytes()
            if not raw:
                continue
            args = [part.decode("utf-8", errors="ignore") for part in raw.split(b"\0") if part]
            if not args or Path(args[0]).name != "iperf3" or "-c" not in args:
                continue
            os.kill(int(cmdline_path.parent.name), signal.SIGTERM)
            killed += 1
    if killed:
        time.sleep(0.5)
        for cmdline_path in proc_root.glob("[0-9]*/cmdline"):
            with contextlib.suppress(Exception):
                raw = cmdline_path.read_bytes()
                args = [part.decode("utf-8", errors="ignore") for part in raw.split(b"\0") if part]
                if args and Path(args[0]).name == "iperf3" and "-c" in args:
                    os.kill(int(cmdline_path.parent.name), signal.SIGKILL)
    return killed


def parse_route_dev(output: str) -> str | None:
    match = re.search(r"\bdev\s+(\S+)", output)
    return match.group(1) if match else None


def redact_args(args: list[str]) -> list[str]:
    return [str(item) for item in args]


def parse_ping_summary(output: str) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    match = re.search(r"(\d+) packets transmitted, (\d+) received,.*?(\d+(?:\.\d+)?)% packet loss", output)
    if match:
        tx = int(match.group(1))
        rx = int(match.group(2))
        summary.update({"packets_transmitted": tx, "packets_received": rx, "packet_loss_percent": float(match.group(3))})
    match = re.search(r"rtt min/avg/max/mdev = ([0-9.]+)/([0-9.]+)/([0-9.]+)/([0-9.]+) ms", output)
    if match:
        summary.update(
            {
                "rtt_min_ms": float(match.group(1)),
                "rtt_avg_ms": float(match.group(2)),
                "rtt_max_ms": float(match.group(3)),
                "rtt_mdev_ms": float(match.group(4)),
            }
        )
    return summary


def parse_bt_scan_count(output: str) -> int:
    count = 0
    for line in output.splitlines():
        if re.search(r"([0-9A-F]{2}:){5}[0-9A-F]{2}", line, flags=re.IGNORECASE):
            count += 1
    return count


def parse_l2ping_summary(output: str) -> dict[str, Any]:
    sent = len(re.findall(r"\bsent\b", output, flags=re.IGNORECASE))
    received = len(re.findall(r"\bbytes from\b", output, flags=re.IGNORECASE))
    times = [float(value) for value in re.findall(r"time\s+([0-9.]+)ms", output)]
    summary: dict[str, Any] = {"sent_lines": sent, "received": received}
    if times:
        summary.update({"rtt_min_ms": min(times), "rtt_avg_ms": sum(times) / len(times), "rtt_max_ms": max(times)})
    return summary


def failure_info(result: dict[str, Any]) -> dict[str, str]:
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    raw = result.get("raw")
    reason = result.get("error") or result.get("stderr") or summary.get("error")
    command = ""
    stderr = ""
    if isinstance(raw, dict):
        reason = reason or raw.get("error") or (raw.get("end") or {}).get("error")
        stderr = str(raw.get("stderr") or "")[-300:]
    elif isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            item_result = item.get("result") if isinstance(item.get("result"), dict) else {}
            if item_result and not item_result.get("ok"):
                command = str(item.get("cmd") or "")
                stderr = str(item_result.get("stderr") or item_result.get("stdout") or "")[-300:]
                reason = reason or stderr or f"{command} failed"
                break
    if not reason and result.get("status") and result.get("status") != "passed":
        reason = str(result.get("status"))
    text = str(reason or "").strip()
    return {
        "reason": text[:500],
        "category": classify_failure_for_agent(result, text),
        "command": command[:240],
        "stderr": stderr[:500],
    }


def classify_failure_for_agent(result: dict[str, Any], reason: str) -> str:
    text = f"{result.get('type') or ''} {result.get('status') or ''} {reason}".lower()
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    if summary.get("udp_loss_exceeded") or "udp loss" in text:
        return "udp_loss"
    if "server is busy" in text:
        return "iperf3_busy"
    if "connection reset" in text or "resource temporarily unavailable" in text:
        return "iperf3_control"
    if "btmgmt" in text or "hcitool" in text or ("timeout" in text and str(result.get("type") or "").startswith("bt_")):
        return "bt_mgmt_timeout"
    if "host is down" in text or "can't connect" in text:
        return "bt_link_down"
    if result.get("status") == "agent_error":
        return "agent_error"
    return "test_failed" if result.get("status") and result.get("status") != "passed" else ""


def enrich_failure(result: dict[str, Any]) -> None:
    if result.get("status") == "passed":
        return
    info = failure_info(result)
    result.setdefault("failure_reason", info["reason"])
    result.setdefault("failure_category", info["category"])
    result.setdefault("failed_command", info["command"])
    result.setdefault("stderr_excerpt", info["stderr"])


def dashboard_result_for_agent(result: dict[str, Any]) -> dict[str, Any]:
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    return {
        "command_id": result.get("command_id"),
        "type": result.get("type"),
        "status": result.get("status"),
        "started_at_ms": result.get("started_at_ms"),
        "finished_at_ms": result.get("finished_at_ms"),
        "timestamp_ms": result.get("timestamp_ms"),
        "peer_ip": result.get("peer_ip") or summary.get("peer_ip"),
        "peer_bt_mac": result.get("peer_bt_mac") or summary.get("peer_bt_mac"),
        "returncode": result.get("returncode") or summary.get("returncode"),
        "failure_reason": result.get("failure_reason"),
        "failure_category": result.get("failure_category"),
        "failed_command": result.get("failed_command"),
        "stderr_excerpt": result.get("stderr_excerpt"),
        "summary": {
            key: summary.get(key)
            for key in (
                "sent_bits_per_second",
                "received_bits_per_second",
                "bits_per_second",
                "lost_percent",
                "retransmits",
                "wifi_path_ok",
                "route_dev",
                "bound_dev",
                "adaptive_selected_bandwidth",
                "adaptive_probe_summary",
                "udp_max_loss_percent",
                "udp_loss_exceeded",
                "connected",
                "connect_failed",
                "reset_by_peer",
                "connection_aborted",
                "duration_ms",
                "sample_count",
            )
            if key in summary
        },
    }


def compact_metric_event(metric: dict[str, Any]) -> dict[str, Any]:
    system = metric.get("system") if isinstance(metric.get("system"), dict) else {}
    wifi = metric.get("wifi") if isinstance(metric.get("wifi"), dict) else {}
    bluetooth = metric.get("bluetooth") if isinstance(metric.get("bluetooth"), dict) else {}
    network = metric.get("network") if isinstance(metric.get("network"), dict) else {}
    temps = [zone.get("temp_millic") for zone in metric.get("thermal", []) if isinstance(zone, dict) and zone.get("temp_millic") is not None]
    return {
        "timestamp_ms": metric.get("timestamp_ms"),
        "system": {
            "uptime_sec": system.get("uptime_sec"),
            "loadavg": system.get("loadavg"),
            "mem_total": system.get("mem_total"),
            "mem_available": system.get("mem_available"),
        },
        "max_temp_millic": max(temps) if temps else None,
        "wifi": {
            "interface": wifi.get("interface"),
            "ipv4": wifi.get("ipv4"),
            "connected": wifi.get("connected"),
            "ssid": wifi.get("ssid"),
            "bssid": wifi.get("bssid"),
            "signal_dbm": wifi.get("signal_dbm"),
            "tx_bitrate": wifi.get("tx_bitrate"),
            "rx_bitrate": wifi.get("rx_bitrate"),
            "tx_retries": wifi.get("tx_retries"),
            "tx_failed": wifi.get("tx_failed"),
        },
        "bluetooth": {
            "controller": bluetooth.get("controller"),
            "available": bluetooth.get("available"),
            "busy": bluetooth.get("busy"),
            "stale": bluetooth.get("stale"),
            "up": bluetooth.get("up"),
            "address": bluetooth.get("address"),
            "rx_errors": bluetooth.get("rx_errors"),
            "tx_errors": bluetooth.get("tx_errors"),
        },
        "network": {
            "uplink_ip": network.get("uplink_ip"),
            "wifi_ip": network.get("wifi_ip"),
            "uplink_ready": network.get("uplink_ready"),
        },
    }


def runtime_snapshot(config: dict[str, Any], command: dict[str, Any] | None = None) -> dict[str, Any]:
    wifi_iface = str(config.get("wifi_interface") or "wlan0")
    bt_controller = str(config.get("bt_controller") or "hci0")
    snapshot: dict[str, Any] = {
        "timestamp_ms": now_ms(),
        "system": light_system_snapshot(),
        "thermal": light_thermal_snapshot(),
        "processes": process_snapshot(),
    }
    snapshot["wifi"] = light_wifi_snapshot(wifi_iface)
    snapshot["bluetooth"] = light_bt_snapshot(bt_controller)
    if command and str(command.get("peer_ip") or ""):
        peer_ip = str(command.get("peer_ip"))
        local_ip = iface_ipv4(wifi_iface)
        route = run_cmd(["ip", "route", "get", peer_ip, "from", local_ip or "0.0.0.0"], timeout=2)
        snapshot["route_to_peer"] = {
            "peer_ip": peer_ip,
            "local_ip": local_ip,
            "ok": route.get("ok"),
            "dev": parse_route_dev(route.get("stdout") or ""),
            "raw": str(route.get("stdout") or "")[-512:],
        }
    return snapshot


def light_system_snapshot() -> dict[str, Any]:
    data: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        data["uptime_sec"] = float(read_text("/proc/uptime").split()[0])
    with contextlib.suppress(Exception):
        data["loadavg"] = list(os.getloadavg())
    meminfo = {}
    with contextlib.suppress(Exception):
        for line in read_text("/proc/meminfo").splitlines():
            key, raw = line.split(":", 1)
            if key in {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}:
                meminfo[key] = int(raw.strip().split()[0]) * 1024
    data["mem_total"] = meminfo.get("MemTotal")
    data["mem_available"] = meminfo.get("MemAvailable")
    return data


def light_thermal_snapshot() -> list[dict[str, Any]]:
    zones = []
    for zone in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
        with contextlib.suppress(Exception):
            zones.append({"name": read_text(zone / "type").strip(), "temp_millic": int(read_text(zone / "temp").strip())})
    return zones


def light_wifi_snapshot(iface: str) -> dict[str, Any]:
    link = run_cmd(["iw", "dev", iface, "link"], timeout=2)
    data = {"interface": iface, "ipv4": iface_ipv4(iface), "connected": "Connected to" in str(link.get("stdout") or "")}
    if link.get("stdout"):
        data.update(parse_iw_link(str(link["stdout"])))
        data["link_raw"] = str(link["stdout"])[-1024:]
    return data


def light_bt_snapshot(controller: str) -> dict[str, Any]:
    hciconfig = run_cmd(["hciconfig", controller, "-a"], timeout=2)
    data = {"controller": controller, "available": hciconfig.get("ok")}
    if hciconfig.get("stdout"):
        data.update(parse_hciconfig(str(hciconfig["stdout"])))
        data["hciconfig_raw"] = str(hciconfig["stdout"])[-1024:]
    return data


def process_snapshot() -> list[str]:
    targets = {"iperf3", "l2test", "l2ping", "btmgmt", "hcitool", "ping"}
    rows: list[str] = []
    for proc_dir in Path("/proc").iterdir():
        if not proc_dir.name.isdigit():
            continue
        pid = proc_dir.name
        try:
            comm = read_text(proc_dir / "comm", limit=128).strip()
            cmdline_raw = (proc_dir / "cmdline").read_bytes()[:4096]
            cmdline = cmdline_raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
            name = comm or Path(cmdline.split(" ", 1)[0]).name
            if name not in targets and not any(part in targets for part in cmdline.split()):
                continue
            status = {}
            for line in read_text(proc_dir / "status", limit=4096).splitlines():
                if line.startswith(("PPid:", "State:", "Threads:", "VmRSS:")):
                    key, value = line.split(":", 1)
                    status[key] = " ".join(value.split())
            rows.append(
                "pid={pid} ppid={ppid} state={state} threads={threads} rss={rss} cmd={cmd}".format(
                    pid=pid,
                    ppid=status.get("PPid", "?"),
                    state=status.get("State", "?"),
                    threads=status.get("Threads", "?"),
                    rss=status.get("VmRSS", "?"),
                    cmd=(cmdline or comm)[:360],
                )
            )
        except Exception:
            continue
    return sorted(rows)[-40:]


def diagnostic_log_snapshot(config: dict[str, Any]) -> dict[str, Any]:
    units = [
        "tspi-burnin-agent.service",
        "tspi-burnin-iperf3.service",
        "bluetooth.service",
        "NetworkManager.service",
        "wpa_supplicant.service",
    ]
    journals = []
    for unit in units:
        result = run_cmd(["journalctl", "-u", unit, "-n", "80", "--no-pager", "-o", "short-iso"], timeout=5)
        journals.append({"unit": unit, "ok": result.get("ok"), "text": str(result.get("stdout") or result.get("stderr") or "")[-6000:]})
    dmesg = run_cmd(["dmesg", "-T"], timeout=5)
    status_agent = run_cmd(["systemctl", "--no-pager", "--full", "status", "tspi-burnin-agent.service"], timeout=5)
    status_iperf = run_cmd(["systemctl", "--no-pager", "--full", "status", "tspi-burnin-iperf3.service"], timeout=5)
    return {
        "snapshot": runtime_snapshot(config),
        "journals": journals,
        "dmesg_tail": str(dmesg.get("stdout") or dmesg.get("stderr") or "")[-12000:],
        "systemd": {
            "agent": str(status_agent.get("stdout") or status_agent.get("stderr") or "")[-6000:],
            "iperf3": str(status_iperf.get("stdout") or status_iperf.get("stderr") or "")[-6000:],
        },
    }


def capture_btmon(controller: str, duration_sec: int) -> dict[str, Any] | None:
    if duration_sec <= 0 or not shutil.which("btmon"):
        return None
    started = monotonic_ms()
    proc = subprocess.Popen(
        ["btmon", "-i", controller],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=subprocess_env(),
    )
    try:
        time.sleep(duration_sec)
        proc.terminate()
        stdout, stderr = proc.communicate(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate(timeout=3)
    return {
        "ok": proc.returncode in {0, -15},
        "returncode": proc.returncode,
        "stdout": (stdout or "")[-8192:],
        "stderr": (stderr or "")[-4096:],
        "duration_ms": monotonic_ms() - started,
    }


class Agent:
    def __init__(self, config_path: Path) -> None:
        self.config = load_config(config_path)
        self.data_dir = Path(self.config["data_dir"])
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state = LocalState(self.data_dir)
        self.state.load()
        self.uploader = Uploader(self.config, self.state)
        self.bt_lock = threading.Lock()
        self.collector = Collector(self.config, self.bt_lock)
        self.log_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self.event_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self.runner = CommandRunner(self.config, self.state, self.uploader, self.log_queue, self.event_queue, self.bt_lock)
        self.sd_notify = SdNotify()
        self.stop_event = threading.Event()

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        killed = cleanup_stale_iperf3_clients()
        if killed:
            self.log_queue.put({"timestamp_ms": now_ms(), "level": "warn", "message": "cleaned stale iperf3 clients", "count": killed})
        self.register()
        self.upload_startup_artifacts()
        threads = [
            threading.Thread(target=self.heartbeat_loop, daemon=True),
            threading.Thread(target=self.metrics_loop, daemon=True),
            threading.Thread(target=self.log_loop, daemon=True),
            threading.Thread(target=self.event_loop, daemon=True),
            threading.Thread(target=self.diagnostic_loop, daemon=True),
            threading.Thread(target=self.command_loop, daemon=True),
            threading.Thread(target=self.spool_loop, daemon=True),
        ]
        for thread in threads:
            thread.start()
        self.sd_notify.ready()
        last_watchdog = 0.0
        while not self.stop_event.wait(1.0):
            if time.monotonic() - last_watchdog >= 10.0:
                self.sd_notify.watchdog()
                last_watchdog = time.monotonic()

    def _base_payload(self) -> dict[str, Any]:
        return {
            "board_id": self.config["board_id"],
            "boot_id": self.state.boot_id,
            "seq": self.state.next_seq(),
            "agent_version": "0.2.0",
        }

    def _stamp_event(self, event: dict[str, Any]) -> dict[str, Any]:
        stamped = dict(event)
        stamped.setdefault("timestamp_ms", now_ms())
        stamped.setdefault("severity", "info")
        if "board_id" not in stamped:
            stamped.update(self._base_payload())
        else:
            stamped.setdefault("boot_id", self.state.boot_id)
            stamped.setdefault("seq", self.state.next_seq())
            stamped.setdefault("agent_version", "0.2.0")
        return stamped

    def register(self) -> None:
        payload = self._base_payload()
        payload.update(
            {
                "hostname": socket.gethostname(),
                "timestamp_ms": now_ms(),
                "config": {
                    "uplink_interface": self.config["uplink_interface"],
                    "wifi_interface": self.config["wifi_interface"],
                    "bt_controller": self.config["bt_controller"],
                    "iperf3_port": self.config["iperf3_port"],
                },
            }
        )
        self.uploader.post("/api/v1/agent/register", payload)
        self.event_queue.put({"timestamp_ms": now_ms(), "event_type": "agent_registered", "message": "agent registered", "data": payload})

    def heartbeat_loop(self) -> None:
        interval = float(self.config["heartbeat_interval_sec"])
        while not self.stop_event.is_set():
            payload = self._base_payload()
            payload.update(self.collector.heartbeat())
            atomic_write_json(self.state.last_state_path, payload)
            self.uploader.post("/api/v1/agent/heartbeat", payload)
            self.event_queue.put(
                {
                    "timestamp_ms": payload.get("timestamp_ms") or now_ms(),
                    "event_type": "heartbeat",
                    "message": "heartbeat",
                    "data": {
                        "hostname": payload.get("hostname"),
                        "wifi": payload.get("wifi"),
                        "network": payload.get("network"),
                        "bluetooth": payload.get("bluetooth"),
                    },
                }
            )
            self.stop_event.wait(interval)

    def metrics_loop(self) -> None:
        interval = float(self.config["metrics_interval_sec"])
        while not self.stop_event.is_set():
            metric = self.collector.collect()
            metric.update(self._base_payload())
            self.uploader.post("/api/v1/metrics/batch", {"metrics": [metric]})
            self.event_queue.put(
                {
                    "timestamp_ms": metric.get("timestamp_ms") or now_ms(),
                    "event_type": "metric_sample",
                    "message": "metric sample",
                    "data": compact_metric_event(metric),
                }
            )
            self.stop_event.wait(interval)

    def log_loop(self) -> None:
        interval = float(self.config["log_flush_interval_sec"])
        log_file = self.data_dir / "logs" / f"{self.state.boot_id}.jsonl"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        batch: list[dict[str, Any]] = []
        last_flush = time.monotonic()
        while not self.stop_event.is_set():
            try:
                event = self.log_queue.get(timeout=0.2)
                event.update(self._base_payload())
                batch.append(event)
                self.event_queue.put(
                    {
                        "timestamp_ms": event.get("timestamp_ms") or now_ms(),
                        "event_type": "agent_log",
                        "severity": str(event.get("level") or "info"),
                        "message": str(event.get("message") or ""),
                        "data": {key: value for key, value in event.items() if key not in {"timestamp_ms", "board_id", "boot_id", "seq"}},
                    }
                )
                with log_file.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event, ensure_ascii=True, separators=(",", ":")) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            except queue.Empty:
                pass
            if batch and (time.monotonic() - last_flush >= interval or len(batch) >= 50):
                self.uploader.post("/api/v1/logs/batch", {"logs": batch})
                batch = []
                last_flush = time.monotonic()
        if batch:
            self.uploader.post("/api/v1/logs/batch", {"logs": batch})

    def event_loop(self) -> None:
        interval = float(self.config.get("event_flush_interval_sec") or 2.0)
        batch: list[dict[str, Any]] = []
        last_flush = time.monotonic()
        while not self.stop_event.is_set():
            try:
                event = self.event_queue.get(timeout=0.2)
                batch.append(self._stamp_event(event))
            except queue.Empty:
                pass
            if batch and (time.monotonic() - last_flush >= interval or len(batch) >= 50):
                self.uploader.post("/api/v1/events/batch", {"events": batch})
                batch = []
                last_flush = time.monotonic()
        while True:
            try:
                batch.append(self._stamp_event(self.event_queue.get_nowait()))
            except queue.Empty:
                break
        if batch:
            self.uploader.post("/api/v1/events/batch", {"events": batch})

    def diagnostic_loop(self) -> None:
        interval = max(30.0, float(self.config.get("log_snapshot_interval_sec") or 60.0))
        while not self.stop_event.is_set():
            self.stop_event.wait(interval)
            if self.stop_event.is_set():
                break
            self.event_queue.put(
                {
                    "timestamp_ms": now_ms(),
                    "event_type": "log_snapshot",
                    "message": "periodic diagnostic snapshot",
                    "data": diagnostic_log_snapshot(self.config),
                }
            )

    def command_loop(self) -> None:
        interval = float(self.config["command_poll_interval_sec"])
        while not self.stop_event.is_set():
            try:
                query = urllib.parse.urlencode({"board_id": self.config["board_id"], "boot_id": self.state.boot_id})
                response = self.uploader.get(f"/api/v1/agent/commands?{query}")
                for command in response.get("commands", []):
                    self.runner.maybe_start(command)
            except Exception as exc:
                self.log_queue.put({"timestamp_ms": now_ms(), "level": "warn", "message": "command poll failed", "error": repr(exc)})
            self.stop_event.wait(interval)

    def spool_loop(self) -> None:
        while not self.stop_event.is_set():
            flushed = self.uploader.flush_spool(limit=100)
            if flushed:
                self.log_queue.put({"timestamp_ms": now_ms(), "level": "info", "message": "flushed spool", "count": flushed})
            self.stop_event.wait(5.0)

    def upload_startup_artifacts(self) -> None:
        artifacts = []
        max_bytes = int(self.config["artifact_max_bytes"])
        for path in sorted(Path("/sys/fs/pstore").glob("*")):
            try:
                raw = path.read_bytes()[:max_bytes]
                artifacts.append({"path": str(path), "size": len(raw), "content_b64": base64.b64encode(raw).decode("ascii")})
            except Exception:
                continue
        journal = run_cmd(["journalctl", "-b", "-1", "-n", "500", "--no-pager", "-o", "short-iso"], timeout=10)
        if journal["stdout"]:
            raw = journal["stdout"].encode("utf-8", errors="replace")[:max_bytes]
            artifacts.append({"path": "journal_previous_boot_tail", "size": len(raw), "content_b64": base64.b64encode(raw).decode("ascii")})
        if artifacts:
            payload = self._base_payload()
            payload.update({"timestamp_ms": now_ms(), "artifacts": artifacts})
            self.uploader.post("/api/v1/crash/upload", payload)

    def _handle_signal(self, signum: int, _frame: Any) -> None:
        self.log_queue.put({"timestamp_ms": now_ms(), "level": "info", "message": "agent stopping", "signal": signum})
        self.stop_event.set()


def main() -> int:
    config_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/etc/tspi-burnin/config.toml")
    agent = Agent(config_path)
    agent.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
