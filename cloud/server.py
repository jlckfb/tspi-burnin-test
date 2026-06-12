#!/usr/bin/env python3
"""Cloud-side API and dashboard for TaishanPi burn-in tests."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"


class Settings:
    db_path = Path(os.environ.get("BURNIN_DB", "/data/burnin.sqlite3"))
    artifact_dir = Path(os.environ.get("BURNIN_ARTIFACT_DIR", "/data/artifacts"))
    api_token = os.environ.get("BURNIN_API_TOKEN", "")
    online_timeout_sec = int(os.environ.get("BURNIN_ONLINE_TIMEOUT_SEC", "10"))
    wifi_epoch_sec = int(os.environ.get("BURNIN_WIFI_EPOCH_SEC", "360"))
    wifi_tcp_sec = int(os.environ.get("BURNIN_WIFI_TCP_SEC", "300"))
    wifi_gap_sec = int(os.environ.get("BURNIN_WIFI_GAP_SEC", "5"))
    wifi_start_grace_sec = int(os.environ.get("BURNIN_WIFI_START_GRACE_SEC", "8"))
    wifi_tcp_parallel = int(os.environ.get("BURNIN_WIFI_TCP_PARALLEL", "4"))
    wifi_udp_bandwidth = os.environ.get("BURNIN_WIFI_UDP_BANDWIDTH", "0")
    wifi_udp_flood_bandwidth = os.environ.get("BURNIN_WIFI_UDP_FLOOD_BANDWIDTH", "30M")
    wifi_udp_small_bandwidth = os.environ.get("BURNIN_WIFI_UDP_SMALL_BANDWIDTH", "5M")
    wifi_udp_large_bandwidth = os.environ.get("BURNIN_WIFI_UDP_LARGE_BANDWIDTH", "20M")
    wifi_udp_flood_rates = os.environ.get("BURNIN_WIFI_UDP_FLOOD_RATES", "5M,10M,20M,30M")
    wifi_udp_small_rates = os.environ.get("BURNIN_WIFI_UDP_SMALL_RATES", "1M,2M,5M")
    wifi_udp_large_rates = os.environ.get("BURNIN_WIFI_UDP_LARGE_RATES", "5M,10M,20M")
    wifi_udp_adaptive_probe_sec = int(os.environ.get("BURNIN_WIFI_UDP_ADAPTIVE_PROBE_SEC", "3"))
    wifi_udp_adaptive_max_loss_percent = float(os.environ.get("BURNIN_WIFI_UDP_ADAPTIVE_MAX_LOSS_PERCENT", "5"))
    wifi_udp_min_sec = int(os.environ.get("BURNIN_WIFI_UDP_MIN_SEC", "10"))
    iperf3_port = int(os.environ.get("BURNIN_IPERF3_PORT", "5201"))
    bt_period_sec = int(os.environ.get("BURNIN_BT_PERIOD_SEC", "60"))
    bt_duration_sec = int(os.environ.get("BURNIN_BT_DURATION_SEC", "20"))
    bt_l2test_duration_sec = int(os.environ.get("BURNIN_BT_L2TEST_DURATION_SEC", "25"))
    bt_l2test_frames = int(os.environ.get("BURNIN_BT_L2TEST_FRAMES", "1000000"))
    bt_l2test_bytes = int(os.environ.get("BURNIN_BT_L2TEST_BYTES", "600"))
    bt_l2test_delay_ms = int(os.environ.get("BURNIN_BT_L2TEST_DELAY_MS", "20"))
    bt_l2test_client_delay_sec = float(os.environ.get("BURNIN_BT_L2TEST_CLIENT_DELAY_SEC", "6"))
    bt_l2test_startup_grace_sec = float(os.environ.get("BURNIN_BT_L2TEST_STARTUP_GRACE_SEC", "10"))
    bt_l2test_psm = os.environ.get("BURNIN_BT_L2TEST_PSM", "").strip()
    dashboard_path = "/" + os.environ.get("BURNIN_DASHBOARD_PATH", "/tspi-burnin").strip("/")
    test_start_ms = int(os.environ.get("BURNIN_TEST_START_MS", "0") or "0")
    test_duration_sec = int(os.environ.get("BURNIN_TEST_DURATION_SEC", str(7 * 24 * 3600)))
    metrics_retain_hours = int(os.environ.get("BURNIN_METRICS_RETAIN_HOURS", "168"))
    events_retain_hours = int(os.environ.get("BURNIN_EVENTS_RETAIN_HOURS", "168"))
    public_dashboard_path = "/" + os.environ.get("BURNIN_PUBLIC_DASHBOARD_PATH", "/tspi-burnin-view").strip("/")


settings = Settings()
settings.db_path.parent.mkdir(parents=True, exist_ok=True)
settings.artifact_dir.mkdir(parents=True, exist_ok=True)


def truncate_text(value: Any, limit: int = 240) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else f"{text[:limit]}..."


def dashboard_log(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "timestamp_ms": item.get("timestamp_ms"),
        "board_id": item.get("board_id"),
        "level": item.get("level"),
        "message": truncate_text(item.get("message") or item.get("error") or item, 220),
    }


def compact_event_data(data: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key in ("resource", "runtime_sec", "status", "failure_category", "failure_reason", "incident_id", "category"):
        if key in data:
            compact[key] = data.get(key)
    command = data.get("command")
    if isinstance(command, dict):
        compact["command"] = {
            key: command.get(key)
            for key in ("id", "type", "peer_board_id", "peer_ip", "peer_bt_mac")
            if key in command
        }
    result = data.get("result")
    if isinstance(result, dict):
        compact["result"] = {
            key: result.get(key)
            for key in ("command_id", "type", "status", "failure_category", "failure_reason")
            if key in result
        }
    if "snapshot" in data:
        compact["snapshot_available"] = True
    if "journals" in data:
        compact["journals_available"] = True
    if "dmesg_tail" in data:
        compact["dmesg_available"] = True
    return compact


def event_view(item: dict[str, Any], include_data: bool = True) -> dict[str, Any]:
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    return {
        "timestamp_ms": item.get("timestamp_ms"),
        "board_id": item.get("board_id"),
        "boot_id": item.get("boot_id"),
        "event_type": item.get("event_type"),
        "command_id": item.get("command_id"),
        "severity": item.get("severity") or "info",
        "message": truncate_text(item.get("message") or data.get("message") or failure_reason(item), 260),
        "data": data if include_data else compact_event_data(data),
    }


def dashboard_result(item: dict[str, Any]) -> dict[str, Any]:
    summary = item.get("summary") or {}
    failed = item.get("status") != "passed"
    failure = failure_details(item) if failed else {}
    keep_summary_keys = (
        "sent_bits_per_second",
        "received_bits_per_second",
        "bits_per_second",
        "retransmits",
        "sent_bytes",
        "received_bytes",
        "lost_packets",
        "packets",
        "lost_percent",
        "jitter_ms",
        "wifi_path_ok",
        "local_wifi_ip",
        "route_dev",
        "bound_dev",
        "gateway_ip",
        "gateway_ping_ok",
        "peer_ping_ok",
        "preflight_failure",
        "wifi_recovered",
        "wifi_recovery_error",
        "packets_transmitted",
        "packets_received",
        "packet_loss_percent",
        "iperf3_attempts",
        "iperf3_retry_reason",
        "iperf3_nonfatal_error",
        "udp_bandwidth",
        "udp_length",
        "adaptive_udp",
        "adaptive_selected_bandwidth",
        "adaptive_probe_sec",
        "adaptive_max_loss_percent",
        "adaptive_probe_count",
        "adaptive_probe_summary",
        "udp_max_loss_percent",
        "udp_loss_exceeded",
        "role",
        "peer_bt_mac",
        "duration_sec",
        "psm",
        "bytes",
        "frames",
        "delay_ms",
        "client_delay_sec",
        "startup_grace_sec",
        "connected",
        "reset_by_peer",
        "connection_aborted",
        "connect_failed",
        "disconnect_seen",
        "sample_count",
        "returncode",
        "duration_ms",
        "ran_long_enough",
        "activity_seen",
        "btmon_captured",
        "devices_found",
        "scan_timed_out",
        "scan_ran_long_enough",
        "controller_up",
        "stdout_lines",
        "stderr_lines",
    )
    return {
        "command_id": item.get("command_id"),
        "type": item.get("type"),
        "status": item.get("status"),
        "started_at_ms": item.get("started_at_ms"),
        "finished_at_ms": item.get("finished_at_ms"),
        "timestamp_ms": item.get("timestamp_ms"),
        "board_id": item.get("board_id"),
        "boot_id": item.get("boot_id"),
        "peer_ip": item.get("peer_ip") or summary.get("peer_ip"),
        "peer_bt_mac": item.get("peer_bt_mac") or summary.get("peer_bt_mac"),
        "peer_board_id": item.get("peer_board_id") or summary.get("peer_board_id"),
        "local_wifi_ip": item.get("local_wifi_ip") or summary.get("local_wifi_ip"),
        "returncode": item.get("returncode"),
        "error": truncate_text((item.get("error") or item.get("stderr") or failure.get("reason")) if failed else "", 220),
        "failure_reason": failure.get("reason"),
        "failure_category": failure.get("category"),
        "failed_command": failure.get("command"),
        "stderr_excerpt": failure.get("stderr"),
        "summary": {key: summary.get(key) for key in keep_summary_keys if key in summary},
    }


def failure_details(item: dict[str, Any]) -> dict[str, str]:
    summary = item.get("summary") if isinstance(item.get("summary"), dict) else {}
    raw = item.get("raw")
    reason = item.get("failure_reason") or item.get("error") or item.get("stderr") or summary.get("error")
    command = ""
    stderr = ""
    if isinstance(raw, dict):
        reason = reason or raw.get("error") or (raw.get("end") or {}).get("error")
        stderr = str(raw.get("stderr") or "")[-300:]
    elif isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            result = entry.get("result") if isinstance(entry.get("result"), dict) else {}
            if result and not result.get("ok"):
                command = str(entry.get("cmd") or "")
                stderr = str(result.get("stderr") or result.get("stdout") or "")[-300:]
                reason = reason or stderr or f"{command} failed"
                break
    if not reason and item.get("status") and item.get("status") != "passed":
        reason = str(item.get("status"))
    text = str(reason or "").strip()
    category = classify_failure(item, text)
    return {
        "reason": truncate_text(text, 260),
        "category": category,
        "command": truncate_text(command, 160),
        "stderr": truncate_text(stderr, 260),
    }


def failure_reason(item: dict[str, Any]) -> str:
    return failure_details(item).get("reason") or ""


def classify_failure(item: dict[str, Any], reason: str) -> str:
    text = f"{item.get('type') or ''} {item.get('status') or ''} {reason}".lower()
    summary = item.get("summary") if isinstance(item.get("summary"), dict) else {}
    if summary.get("udp_loss_exceeded") or "udp loss" in text:
        return "udp_loss"
    if "gateway_ping_failed" in str(summary.get("preflight_failure") or "") or "gateway_ping_failed" in text:
        return "wifi_data_plane_down"
    if "peer_ping_failed" in str(summary.get("preflight_failure") or "") or "peer_ping_failed" in text:
        return "wifi_peer_unreachable"
    if item.get("type") == "wifi_ping" and number_or_none(summary.get("packet_loss_percent")) == 100.0:
        return "wifi_loss"
    if "lost_during_command" in text or "offline" in text:
        return "board_offline"
    if "server is busy" in text:
        return "iperf3_busy"
    if "timeout" in text and str(item.get("type") or "").startswith("wifi_"):
        return "iperf3_timeout"
    if "connection reset" in text or "resource temporarily unavailable" in text or "unable to write to stream socket" in text:
        return "iperf3_control"
    if "btmgmt" in text or "hcitool" in text or "timeout" in text and str(item.get("type") or "").startswith("bt_"):
        return "bt_mgmt_timeout"
    if "host is down" in text or "can't connect" in text:
        return "bt_link_down"
    if item.get("status") == "agent_error":
        return "agent_error"
    return "test_failed" if item.get("status") and item.get("status") != "passed" else ""


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.init_schema()

    def init_schema(self) -> None:
        with self.lock, self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS boards (
                    board_id TEXT PRIMARY KEY,
                    first_seen_ms INTEGER NOT NULL,
                    last_seen_ms INTEGER NOT NULL,
                    boot_id TEXT,
                    hostname TEXT,
                    remote_ip TEXT,
                    wifi_ip TEXT,
                    wired_ip TEXT,
                    status TEXT NOT NULL DEFAULT 'unknown',
                    last_heartbeat_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    board_id TEXT NOT NULL,
                    boot_id TEXT,
                    seq INTEGER,
                    timestamp_ms INTEGER,
                    data_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_metrics_board_ts ON metrics(board_id, timestamp_ms);
                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    board_id TEXT NOT NULL,
                    boot_id TEXT,
                    seq INTEGER,
                    timestamp_ms INTEGER,
                    level TEXT,
                    message TEXT,
                    data_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_logs_board_ts ON logs(board_id, timestamp_ms);
                CREATE TABLE IF NOT EXISTS results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    board_id TEXT NOT NULL,
                    boot_id TEXT,
                    command_id TEXT,
                    type TEXT,
                    status TEXT,
                    timestamp_ms INTEGER,
                    data_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_results_board_ts ON results(board_id, timestamp_ms);
                CREATE INDEX IF NOT EXISTS idx_results_status_ts ON results(status, timestamp_ms);
                CREATE TABLE IF NOT EXISTS artifacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    board_id TEXT NOT NULL,
                    boot_id TEXT,
                    timestamp_ms INTEGER,
                    name TEXT NOT NULL,
                    size INTEGER,
                    path TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS config (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    board_id TEXT NOT NULL,
                    boot_id TEXT,
                    timestamp_ms INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    command_id TEXT,
                    severity TEXT NOT NULL DEFAULT 'info',
                    message TEXT,
                    data_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_board_ts ON events(board_id, timestamp_ms);
                CREATE INDEX IF NOT EXISTS idx_events_command_ts ON events(command_id, timestamp_ms);
                CREATE INDEX IF NOT EXISTS idx_events_type_ts ON events(event_type, timestamp_ms);
                CREATE TABLE IF NOT EXISTS incidents (
                    incident_id TEXT PRIMARY KEY,
                    board_id TEXT NOT NULL,
                    boot_id TEXT,
                    started_at_ms INTEGER NOT NULL,
                    ended_at_ms INTEGER,
                    status TEXT NOT NULL,
                    category TEXT NOT NULL,
                    last_command_id TEXT,
                    summary_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_incidents_board_status ON incidents(board_id, status, started_at_ms);
                CREATE INDEX IF NOT EXISTS idx_incidents_status_ts ON incidents(status, started_at_ms);
                """
            )

    def upsert_board(self, payload: dict[str, Any], remote_ip: str | None) -> None:
        board_id = require_board_id(payload)
        ts = now_ms()
        hostname = payload.get("hostname")
        boot_id = payload.get("boot_id")
        wifi_ip, wired_ip = extract_ips(payload)
        with self.lock, self.conn:
            existing = self.conn.execute("SELECT first_seen_ms FROM boards WHERE board_id=?", (board_id,)).fetchone()
            first_seen = int(existing["first_seen_ms"]) if existing else ts
            self.conn.execute(
                """
                INSERT INTO boards(board_id, first_seen_ms, last_seen_ms, boot_id, hostname, remote_ip, wifi_ip, wired_ip, status, last_heartbeat_json)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(board_id) DO UPDATE SET
                    last_seen_ms=excluded.last_seen_ms,
                    boot_id=excluded.boot_id,
                    hostname=COALESCE(excluded.hostname, boards.hostname),
                    remote_ip=excluded.remote_ip,
                    wifi_ip=COALESCE(excluded.wifi_ip, boards.wifi_ip),
                    wired_ip=COALESCE(excluded.wired_ip, boards.wired_ip),
                    status='online',
                    last_heartbeat_json=excluded.last_heartbeat_json
                """,
                (
                    board_id,
                    first_seen,
                    ts,
                    boot_id,
                    hostname,
                    remote_ip,
                    wifi_ip,
                    wired_ip,
                    "online",
                    dump_json(payload),
                ),
            )

    def insert_metrics(self, metrics: list[dict[str, Any]]) -> None:
        rows = []
        for item in metrics:
            board_id = require_board_id(item)
            rows.append((board_id, item.get("boot_id"), item.get("seq"), item.get("timestamp_ms") or now_ms(), dump_json(item)))
        with self.lock, self.conn:
            self.conn.executemany("INSERT INTO metrics(board_id, boot_id, seq, timestamp_ms, data_json) VALUES(?,?,?,?,?)", rows)

    def insert_logs(self, logs: list[dict[str, Any]]) -> None:
        rows = []
        for item in logs:
            board_id = require_board_id(item)
            rows.append(
                (
                    board_id,
                    item.get("boot_id"),
                    item.get("seq"),
                    item.get("timestamp_ms") or now_ms(),
                    item.get("level"),
                    item.get("message"),
                    dump_json(item),
                )
            )
        with self.lock, self.conn:
            self.conn.executemany("INSERT INTO logs(board_id, boot_id, seq, timestamp_ms, level, message, data_json) VALUES(?,?,?,?,?,?,?)", rows)

    def insert_events(self, events: list[dict[str, Any]]) -> int:
        rows = []
        for item in events:
            board_id = require_board_id(item)
            ts = int(item.get("timestamp_ms") or now_ms())
            event_type = str(item.get("event_type") or item.get("type") or "event").strip() or "event"
            data = item.get("data") if isinstance(item.get("data"), dict) else {}
            rows.append(
                (
                    board_id,
                    item.get("boot_id"),
                    ts,
                    event_type,
                    item.get("command_id") or data.get("command_id"),
                    str(item.get("severity") or data.get("severity") or "info"),
                    truncate_text(item.get("message") or data.get("message") or "", 500),
                    dump_json(item),
                )
            )
        if rows:
            with self.lock, self.conn:
                self.conn.executemany(
                    """
                    INSERT INTO events(board_id, boot_id, timestamp_ms, event_type, command_id, severity, message, data_json)
                    VALUES(?,?,?,?,?,?,?,?)
                    """,
                    rows,
                )
        return len(rows)

    def insert_results(self, payload: dict[str, Any]) -> None:
        board_id = payload.get("board_id")
        boot_id = payload.get("boot_id")
        rows = []
        events = []
        for item in payload.get("results", []):
            item.setdefault("board_id", board_id)
            item.setdefault("boot_id", boot_id)
            item.setdefault("timestamp_ms", item.get("finished_at_ms") or now_ms())
            details = failure_details(item)
            if item.get("status") != "passed":
                item.setdefault("failure_reason", details.get("reason"))
                item.setdefault("failure_category", details.get("category"))
            rows.append(
                (
                    require_board_id(item),
                    item.get("boot_id"),
                    item.get("command_id"),
                    item.get("type"),
                    item.get("status"),
                    item.get("timestamp_ms"),
                    dump_json(item),
                )
            )
            events.append(
                {
                    "board_id": item.get("board_id"),
                    "boot_id": item.get("boot_id"),
                    "timestamp_ms": item.get("timestamp_ms"),
                    "event_type": "command_finished",
                    "command_id": item.get("command_id"),
                    "severity": "error" if item.get("status") not in {"passed", "unsupported"} else "info",
                    "message": details.get("reason") if item.get("status") != "passed" else "command finished",
                    "data": {
                        "result": dashboard_result(item),
                        "status": item.get("status"),
                        "failure_category": details.get("category"),
                        "failure_reason": details.get("reason"),
                    },
                }
            )
        with self.lock, self.conn:
            self.conn.executemany("INSERT INTO results(board_id, boot_id, command_id, type, status, timestamp_ms, data_json) VALUES(?,?,?,?,?,?,?)", rows)
        self.insert_events(events)

    def insert_artifacts(self, payload: dict[str, Any]) -> int:
        board_id = require_board_id(payload)
        boot_id = str(payload.get("boot_id") or "unknown")
        ts = int(payload.get("timestamp_ms") or now_ms())
        safe_board = safe_name(board_id)
        safe_boot = safe_name(boot_id)
        target_dir = settings.artifact_dir / safe_board / safe_boot
        target_dir.mkdir(parents=True, exist_ok=True)
        rows = []
        count = 0
        for artifact in payload.get("artifacts", []):
            name = safe_name(str(artifact.get("path") or f"artifact_{count}"))
            raw = base64.b64decode(str(artifact.get("content_b64") or ""), validate=False)
            path = target_dir / f"{ts}_{count}_{name}"
            path.write_bytes(raw)
            rows.append((board_id, boot_id, ts, str(artifact.get("path") or name), len(raw), str(path)))
            count += 1
        with self.lock, self.conn:
            self.conn.executemany("INSERT INTO artifacts(board_id, boot_id, timestamp_ms, name, size, path) VALUES(?,?,?,?,?,?)", rows)
        return count

    def purge_old_metrics(self) -> int:
        cutoff = now_ms() - settings.metrics_retain_hours * 3600 * 1000
        with self.lock, self.conn:
            cursor = self.conn.execute("DELETE FROM metrics WHERE timestamp_ms < ?", (cutoff,))
            return cursor.rowcount

    def purge_old_events(self) -> int:
        cutoff = now_ms() - settings.events_retain_hours * 3600 * 1000
        with self.lock, self.conn:
            cursor = self.conn.execute("DELETE FROM events WHERE timestamp_ms < ?", (cutoff,))
            return cursor.rowcount

    def clear_history(self, board_id: str | None = None) -> dict[str, int]:
        counts: dict[str, int] = {}
        params = (board_id,) if board_id else ()
        where = " WHERE board_id=?" if board_id else ""
        with self.lock, self.conn:
            artifact_paths = [
                str(row["path"])
                for row in self.conn.execute(f"SELECT path FROM artifacts{where}", params).fetchall()
            ]
            for table in ("metrics", "logs", "results", "artifacts", "events", "incidents"):
                cursor = self.conn.execute(f"DELETE FROM {table}{where}", params)
                counts[table] = cursor.rowcount
        deleted_files = 0
        for path in artifact_paths:
            try:
                Path(path).unlink(missing_ok=True)
                deleted_files += 1
            except Exception:
                pass
        counts["artifact_files"] = deleted_files
        return counts

    def delete_board(self, board_id: str, delete_history: bool = True) -> dict[str, Any]:
        with self.lock, self.conn:
            cursor = self.conn.execute("DELETE FROM boards WHERE board_id=?", (board_id,))
            board_count = cursor.rowcount
        history_counts = self.clear_history(board_id) if delete_history else {}
        return {"boards": board_count, "history": history_counts}

    def get_config(self, key: str, default: Any) -> Any:
        with self.lock:
            row = self.conn.execute("SELECT value_json FROM config WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value_json"])
        except Exception:
            return default

    def set_config(self, key: str, value: Any) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO config(key, value_json) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
                (key, dump_json(value)),
            )

    def set_schedule(self, start_ms: int, end_ms: int) -> dict[str, Any]:
        start_ms = max(0, int(start_ms or 0))
        end_ms = max(0, int(end_ms or 0))
        if start_ms and end_ms and end_ms <= start_ms:
            raise HTTPException(status_code=400, detail="end_ms must be greater than start_ms")
        duration_sec = int((end_ms - start_ms) / 1000) if start_ms and end_ms else 0
        self.set_config("test_start_ms", start_ms)
        self.set_config("test_duration_sec", duration_sec)
        return self.schedule()

    def schedule(self) -> dict[str, Any]:
        start_ms = int(self.get_config("test_start_ms", settings.test_start_ms) or 0)
        duration_sec = int(self.get_config("test_duration_sec", settings.test_duration_sec) or 0)
        end_ms = start_ms + duration_sec * 1000 if start_ms and duration_sec > 0 else 0
        now = now_ms()
        if not start_ms or duration_sec <= 0:
            status = "unlimited"
            commands_enabled = True
        elif now < start_ms:
            status = "pending"
            commands_enabled = False
        elif end_ms and now >= end_ms:
            status = "expired"
            commands_enabled = False
        else:
            status = "running"
            commands_enabled = True
        wifi_epoch = int(time.time() // settings.wifi_epoch_sec) if settings.wifi_epoch_sec > 0 else 0
        wifi_modes = []
        for offset in range(8):
            mode = wifi_mode_for_epoch(wifi_epoch + offset)
            start = (wifi_epoch + offset) * settings.wifi_epoch_sec * 1000
            wifi_modes.append(
                {
                    "epoch": wifi_epoch + offset,
                    "type": mode.get("type"),
                    "start_ms": start,
                    "end_ms": start + settings.wifi_epoch_sec * 1000,
                    "bandwidth": mode.get("bandwidth"),
                    "adaptive_rates": mode.get("adaptive_rates"),
                }
            )
        return {
            "wifi_epoch_sec": settings.wifi_epoch_sec,
            "wifi_tcp_sec": settings.wifi_tcp_sec,
            "wifi_gap_sec": settings.wifi_gap_sec,
            "wifi_start_grace_sec": settings.wifi_start_grace_sec,
            "wifi_udp_flood_bandwidth": settings.wifi_udp_flood_bandwidth,
            "wifi_udp_small_bandwidth": settings.wifi_udp_small_bandwidth,
            "wifi_udp_large_bandwidth": settings.wifi_udp_large_bandwidth,
            "iperf3_port": settings.iperf3_port,
            "bt_period_sec": settings.bt_period_sec,
            "dashboard_path": settings.dashboard_path,
            "public_dashboard_path": settings.public_dashboard_path,
            "test_start_ms": start_ms,
            "test_duration_sec": duration_sec,
            "test_end_ms": end_ms,
            "test_progress_percent": test_progress_percent(start_ms, duration_sec),
            "status": status,
            "commands_enabled": commands_enabled,
            "current_wifi_mode": wifi_modes[0] if wifi_modes else None,
            "upcoming_wifi_modes": wifi_modes,
        }

    def trend_snapshot(self, hours: int = 24, max_points_per_board: int = 360) -> dict[str, Any]:
        hours = min(max(int(hours or 24), 1), 168)
        cutoff = now_ms() - hours * 3600 * 1000
        with self.lock:
            metric_rows = [
                dict(row)
                for row in self.conn.execute(
                    "SELECT board_id, timestamp_ms, data_json FROM metrics WHERE timestamp_ms >= ? ORDER BY board_id, timestamp_ms",
                    (cutoff,),
                ).fetchall()
            ]
            result_rows = [
                dict(row)
                for row in self.conn.execute(
                    "SELECT board_id, timestamp_ms, data_json FROM results WHERE timestamp_ms >= ? ORDER BY board_id, timestamp_ms",
                    (cutoff,),
                ).fetchall()
            ]
        by_board: dict[str, list[dict[str, Any]]] = {}
        for row in metric_rows:
            by_board.setdefault(str(row["board_id"]), []).append(row)
        points: list[dict[str, Any]] = []
        for board_id, rows in by_board.items():
            stride = max(1, len(rows) // max_points_per_board)
            previous: dict[str, Any] | None = None
            sampled = rows[::stride]
            if rows and sampled[-1] is not rows[-1]:
                sampled.append(rows[-1])
            for row in sampled:
                try:
                    metric = json.loads(row["data_json"])
                except Exception:
                    continue
                point = metric_trend_point(board_id, int(row["timestamp_ms"]), metric, previous)
                points.append(point)
                previous = {"metric": metric, "timestamp_ms": int(row["timestamp_ms"])}
        result_points: list[dict[str, Any]] = []
        for row in result_rows[-1000:]:
            try:
                result = json.loads(row["data_json"])
            except Exception:
                continue
            result_points.append(result_trend_point(str(row["board_id"]), int(row["timestamp_ms"]), result))
        return {
            "timestamp_ms": now_ms(),
            "hours": hours,
            "points": points,
            "result_points": result_points,
        }

    def list_boards(self) -> list[dict[str, Any]]:
        cutoff = now_ms() - settings.online_timeout_sec * 1000
        with self.lock:
            boards = [dict(row) for row in self.conn.execute("SELECT * FROM boards ORDER BY board_id").fetchall()]
        for board in boards:
            board["online"] = int(board["last_seen_ms"]) >= cutoff
            board["status"] = "online" if board["online"] else "offline"
            try:
                board["last_heartbeat"] = json.loads(board.pop("last_heartbeat_json"))
            except Exception:
                board["last_heartbeat"] = {}
        return boards

    def event_rows(self, rows: list[sqlite3.Row], include_data: bool = True) -> list[dict[str, Any]]:
        items = []
        for row in rows:
            try:
                item = json.loads(row["data_json"])
            except Exception:
                item = {
                    "timestamp_ms": row["timestamp_ms"],
                    "board_id": row["board_id"],
                    "boot_id": row["boot_id"],
                    "event_type": row["event_type"],
                    "command_id": row["command_id"],
                    "severity": row["severity"],
                    "message": row["message"],
                    "data": {},
                }
            items.append(event_view(item, include_data=include_data))
        return items

    def recent_events(
        self,
        board_id: str | None = None,
        command_id: str | None = None,
        event_type: str | None = None,
        severity: str | None = None,
        limit: int = 200,
        since_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        if board_id:
            clauses.append("board_id=?")
            params.append(board_id)
        if command_id:
            clauses.append("command_id=?")
            params.append(command_id)
        if event_type:
            clauses.append("event_type=?")
            params.append(event_type)
        if severity:
            clauses.append("severity=?")
            params.append(severity)
        if since_ms:
            clauses.append("timestamp_ms>=?")
            params.append(int(since_ms))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit or 200), 2000)))
        with self.lock:
            rows = self.conn.execute(
                f"SELECT * FROM events {where} ORDER BY timestamp_ms DESC, id DESC LIMIT ?",
                params,
            ).fetchall()
        return list(reversed(self.event_rows(rows)))

    def recent_logs(self, board_id: str | None = None, limit: int = 200, since_ms: int | None = None) -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        if board_id:
            clauses.append("board_id=?")
            params.append(board_id)
        if since_ms:
            clauses.append("timestamp_ms>=?")
            params.append(int(since_ms))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit or 200), 2000)))
        with self.lock:
            rows = self.conn.execute(
                f"SELECT data_json FROM logs {where} ORDER BY timestamp_ms DESC, id DESC LIMIT ?",
                params,
            ).fetchall()
        logs = []
        for row in rows:
            try:
                logs.append(json.loads(row["data_json"]))
            except Exception:
                continue
        return list(reversed(logs))

    def recent_metrics(
        self,
        board_id: str | None = None,
        limit: int = 200,
        since_ms: int | None = None,
        compact: bool = True,
    ) -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        if board_id:
            clauses.append("board_id=?")
            params.append(board_id)
        if since_ms:
            clauses.append("timestamp_ms>=?")
            params.append(int(since_ms))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit or 200), 2000)))
        with self.lock:
            rows = self.conn.execute(
                f"SELECT data_json FROM metrics {where} ORDER BY timestamp_ms DESC, id DESC LIMIT ?",
                params,
            ).fetchall()
        metrics = []
        for row in rows:
            try:
                metric = json.loads(row["data_json"])
            except Exception:
                continue
            metrics.append(compact_metric(metric) if compact else metric)
        return list(reversed(metrics))

    def recent_failures(self, board_id: str | None = None, limit: int = 100, raw: bool = False) -> list[dict[str, Any]]:
        clauses = ["COALESCE(status, 'unknown') != 'passed'"]
        params: list[Any] = []
        if board_id:
            clauses.append("board_id=?")
            params.append(board_id)
        params.append(max(1, min(int(limit or 100), 1000)))
        with self.lock:
            rows = self.conn.execute(
                f"""
                SELECT data_json FROM results
                WHERE {' AND '.join(clauses)}
                ORDER BY timestamp_ms DESC, id DESC LIMIT ?
                """,
                params,
            ).fetchall()
        failures = []
        for row in rows:
            try:
                item = json.loads(row["data_json"])
            except Exception:
                continue
            failures.append(item if raw else dashboard_result(item))
        return failures

    def recent_incidents(self, board_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        params: list[Any] = []
        where = ""
        if board_id:
            where = "WHERE board_id=?"
            params.append(board_id)
        params.append(max(1, min(int(limit or 50), 500)))
        with self.lock:
            rows = self.conn.execute(
                f"SELECT * FROM incidents {where} ORDER BY started_at_ms DESC LIMIT ?",
                params,
            ).fetchall()
        incidents = []
        for row in rows:
            item = dict(row)
            try:
                item["summary"] = json.loads(item.pop("summary_json"))
            except Exception:
                item["summary"] = {}
            incidents.append(item)
        return incidents

    def maintain_offline_incidents(self, boards: list[dict[str, Any]]) -> None:
        now = now_ms()
        with self.lock, self.conn:
            for board in boards:
                board_id = str(board["board_id"])
                open_row = self.conn.execute(
                    "SELECT * FROM incidents WHERE board_id=? AND status='open' ORDER BY started_at_ms DESC LIMIT 1",
                    (board_id,),
                ).fetchone()
                if board.get("online"):
                    if open_row:
                        self.conn.execute(
                            "UPDATE incidents SET status='closed', ended_at_ms=? WHERE incident_id=?",
                            (now, open_row["incident_id"]),
                        )
                    continue
                if open_row:
                    continue
                last_event_row = self.conn.execute(
                    "SELECT * FROM events WHERE board_id=? ORDER BY timestamp_ms DESC, id DESC LIMIT 1",
                    (board_id,),
                ).fetchone()
                last_command_row = self.conn.execute(
                    """
                    SELECT * FROM events
                    WHERE board_id=? AND command_id IS NOT NULL
                    ORDER BY timestamp_ms DESC, id DESC LIMIT 1
                    """,
                    (board_id,),
                ).fetchone()
                last_metric_row = self.conn.execute(
                    "SELECT timestamp_ms, data_json FROM metrics WHERE board_id=? ORDER BY timestamp_ms DESC, id DESC LIMIT 1",
                    (board_id,),
                ).fetchone()
                last_log_rows = self.conn.execute(
                    "SELECT data_json FROM logs WHERE board_id=? ORDER BY timestamp_ms DESC, id DESC LIMIT 10",
                    (board_id,),
                ).fetchall()
                last_result_row = self.conn.execute(
                    "SELECT data_json FROM results WHERE board_id=? ORDER BY timestamp_ms DESC, id DESC LIMIT 1",
                    (board_id,),
                ).fetchone()
                last_command = json.loads(last_command_row["data_json"]) if last_command_row else {}
                last_event = json.loads(last_event_row["data_json"]) if last_event_row else {}
                last_metric = json.loads(last_metric_row["data_json"]) if last_metric_row else {}
                last_result = json.loads(last_result_row["data_json"]) if last_result_row else {}
                last_logs = []
                for row in last_log_rows:
                    try:
                        last_logs.append(dashboard_log(json.loads(row["data_json"])))
                    except Exception:
                        pass
                last_command_type = str(last_command.get("event_type") or "")
                category = "lost_during_command" if last_command.get("command_id") and last_command_type != "command_finished" else "board_offline"
                incident_id = f"{board_id}-{int(board.get('last_seen_ms') or now)}"
                summary = {
                    "last_seen_ms": board.get("last_seen_ms"),
                    "offline_detected_ms": now,
                    "last_event": event_view(last_event) if last_event else None,
                    "last_command": event_view(last_command) if last_command else None,
                    "last_metric": compact_metric(last_metric),
                    "last_result": dashboard_result(last_result) if last_result else None,
                    "last_logs": last_logs,
                }
                self.conn.execute(
                    """
                    INSERT OR IGNORE INTO incidents(incident_id, board_id, boot_id, started_at_ms, ended_at_ms, status, category, last_command_id, summary_json)
                    VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        incident_id,
                        board_id,
                        board.get("boot_id"),
                        int(board.get("last_seen_ms") or now),
                        None,
                        "open",
                        category,
                        last_command.get("command_id"),
                        dump_json(summary),
                    ),
                )
                event = {
                    "board_id": board_id,
                    "boot_id": board.get("boot_id"),
                    "timestamp_ms": now,
                    "event_type": "board_offline_incident",
                    "command_id": last_command.get("command_id"),
                    "severity": "error",
                    "message": category,
                    "data": {"incident_id": incident_id, "category": category, "summary": summary},
                }
                self.conn.execute(
                    """
                    INSERT INTO events(board_id, boot_id, timestamp_ms, event_type, command_id, severity, message, data_json)
                    VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (
                        board_id,
                        board.get("boot_id"),
                        now,
                        "board_offline_incident",
                        last_command.get("command_id"),
                        "error",
                        category,
                        dump_json(event),
                    ),
                )

    def board_diagnostics(self, board_id: str, limit: int = 200) -> dict[str, Any]:
        boards = [board for board in self.list_boards() if board["board_id"] == board_id]
        if not boards:
            raise HTTPException(status_code=404, detail="board not found")
        self.maintain_offline_incidents(boards)
        events = self.recent_events(board_id=board_id, limit=limit)
        incidents = self.recent_incidents(board_id=board_id, limit=20)
        failures = self.recent_failures(board_id=board_id, limit=50)
        log_snapshots = self.recent_events(board_id=board_id, event_type="log_snapshot", limit=5)
        with self.lock:
            metrics = [
                compact_metric(json.loads(row["data_json"]))
                for row in self.conn.execute(
                    "SELECT data_json FROM metrics WHERE board_id=? ORDER BY timestamp_ms DESC, id DESC LIMIT 20",
                    (board_id,),
                )
            ]
            results = [
                dashboard_result(json.loads(row["data_json"]))
                for row in self.conn.execute(
                    "SELECT data_json FROM results WHERE board_id=? ORDER BY timestamp_ms DESC, id DESC LIMIT 30",
                    (board_id,),
                )
            ]
            logs = [
                dashboard_log(json.loads(row["data_json"]))
                for row in self.conn.execute(
                    "SELECT data_json FROM logs WHERE board_id=? ORDER BY timestamp_ms DESC, id DESC LIMIT 50",
                    (board_id,),
                )
            ]
        return {
            "timestamp_ms": now_ms(),
            "board": boards[0],
            "incidents": incidents,
            "events": events,
            "log_snapshots": log_snapshots,
            "latest_metrics": metrics,
            "recent_results": results,
            "recent_failures": failures,
            "recent_logs": logs,
        }

    def diagnostics_summary(self, board_id: str | None = None, limit: int = 50) -> dict[str, Any]:
        limit = max(1, min(int(limit or 50), 500))
        boards = self.list_boards()
        if board_id:
            boards = [board for board in boards if board["board_id"] == board_id]
            if not boards:
                raise HTTPException(status_code=404, detail="board not found")
        self.maintain_offline_incidents(boards)
        failures = self.recent_failures(board_id=board_id, limit=limit)
        incidents = self.recent_incidents(board_id=board_id, limit=limit)
        events = self.recent_events(board_id=board_id, limit=limit)
        recent_command_events = self.recent_events(board_id=board_id, limit=500)
        failure_categories: dict[str, int] = {}
        for failure in failures:
            category = str(failure.get("failure_category") or "unknown")
            failure_categories[category] = failure_categories.get(category, 0) + 1
        command_last: dict[str, dict[str, Any]] = {}
        for event in recent_command_events:
            command_id = event.get("command_id")
            if command_id:
                command_last[str(command_id)] = event
        active_commands = [
            event
            for event in command_last.values()
            if event.get("event_type") in {"command_started", "command_progress"}
        ]
        cutoff_24h = now_ms() - 24 * 3600 * 1000
        clauses = ["timestamp_ms>=?"]
        params: list[Any] = [cutoff_24h]
        if board_id:
            clauses.append("board_id=?")
            params.append(board_id)
        with self.lock:
            event_count_rows = [
                dict(row)
                for row in self.conn.execute(
                    f"""
                    SELECT event_type, severity, COUNT(*) AS count, MAX(timestamp_ms) AS last_ms
                    FROM events
                    WHERE {' AND '.join(clauses)}
                    GROUP BY event_type, severity
                    ORDER BY count DESC, event_type, severity
                    LIMIT 50
                    """,
                    params,
                ).fetchall()
            ]
            latest_log_snapshot_rows = [
                dict(row)
                for row in self.conn.execute(
                    f"""
                    SELECT board_id, MAX(timestamp_ms) AS timestamp_ms
                    FROM events
                    WHERE event_type='log_snapshot' {"AND board_id=?" if board_id else ""}
                    GROUP BY board_id
                    ORDER BY timestamp_ms DESC
                    LIMIT 20
                    """,
                    (board_id,) if board_id else (),
                ).fetchall()
            ]
        offline_boards = [
            {
                "board_id": board["board_id"],
                "boot_id": board.get("boot_id"),
                "hostname": board.get("hostname"),
                "last_seen_ms": board.get("last_seen_ms"),
                "wifi_ip": board.get("wifi_ip"),
                "remote_ip": board.get("remote_ip"),
            }
            for board in boards
            if not board.get("online")
        ]
        open_incidents = [item for item in incidents if item.get("status") == "open"]
        latest_log_snapshots = [
            {
                "board_id": row["board_id"],
                "timestamp_ms": row["timestamp_ms"],
                "query": f"/api/v1/diagnostics/events?board_id={row['board_id']}&event_type=log_snapshot&limit=1",
            }
            for row in latest_log_snapshot_rows
        ]
        queries = [
            {"name": "summary", "path": "/api/v1/diagnostics?limit=100"},
            {"name": "boards", "path": "/api/v1/diagnostics/boards"},
            {"name": "events", "path": "/api/v1/diagnostics/events?limit=200"},
            {"name": "failures", "path": "/api/v1/diagnostics/failures?limit=100"},
            {"name": "logs", "path": "/api/v1/diagnostics/logs?limit=200"},
            {"name": "metrics", "path": "/api/v1/diagnostics/metrics?limit=200&compact=true"},
            {"name": "board_detail", "path": "/api/v1/diagnostics/boards/{board_id}?limit=300"},
            {"name": "command_detail", "path": "/api/v1/diagnostics/commands/{command_id}"},
        ]
        return {
            "timestamp_ms": now_ms(),
            "scope": {"board_id": board_id, "limit": limit},
            "retention": {
                "metrics_hours": settings.metrics_retain_hours,
                "events_hours": settings.events_retain_hours,
                "logs": "not automatically purged",
                "results": "not automatically purged",
            },
            "boards": boards,
            "offline_boards": offline_boards,
            "open_incidents": open_incidents,
            "recent_incidents": incidents,
            "recent_failures": failures,
            "failure_categories": failure_categories,
            "active_or_unfinished_commands": active_commands[-50:],
            "event_counts_24h": event_count_rows,
            "recent_events": events,
            "latest_log_snapshots": latest_log_snapshots,
            "queries": queries,
        }

    def snapshot(self) -> dict[str, Any]:
        boards = self.list_boards()
        self.maintain_offline_incidents(boards)
        latest_metrics = {}
        schedule = self.schedule()
        with self.lock:
            for board in boards:
                row = self.conn.execute(
                    "SELECT data_json FROM metrics WHERE board_id=? ORDER BY timestamp_ms DESC, id DESC LIMIT 1",
                    (board["board_id"],),
                ).fetchone()
                latest_metrics[board["board_id"]] = json.loads(row["data_json"]) if row else None
            logs = [
                dashboard_log(json.loads(row["data_json"]))
                for row in self.conn.execute("SELECT data_json FROM logs ORDER BY timestamp_ms DESC, id DESC LIMIT 80")
            ]
            results = [
                dashboard_result(json.loads(row["data_json"]))
                for row in self.conn.execute("SELECT data_json FROM results ORDER BY timestamp_ms DESC, id DESC LIMIT 50")
            ]
            events = self.event_rows(
                self.conn.execute("SELECT * FROM events ORDER BY timestamp_ms DESC, id DESC LIMIT 10").fetchall(),
                include_data=False,
            )
            failed_where = "COALESCE(status, 'unknown') != 'passed'"
            cutoff_24h = now_ms() - 24 * 3600 * 1000
            total_row = self.conn.execute(
                "SELECT COUNT(*) AS c, MIN(timestamp_ms) AS first_ms, MAX(timestamp_ms) AS last_ms FROM results"
            ).fetchone()
            type_rows = [
                dict(row)
                for row in self.conn.execute(
                    f"""
                    SELECT
                        COALESCE(type, 'unknown') AS type,
                        COUNT(*) AS total,
                        SUM(CASE WHEN status='passed' THEN 1 ELSE 0 END) AS passed,
                        SUM(CASE WHEN {failed_where} THEN 1 ELSE 0 END) AS failed,
                        MAX(timestamp_ms) AS last_ms
                    FROM results
                    GROUP BY COALESCE(type, 'unknown')
                    ORDER BY total DESC, type
                    LIMIT 30
                    """
                )
            ]
            try:
                udp_loss_row = self.conn.execute(
                    """
                    SELECT
                        COUNT(*) AS samples,
                        AVG(CAST(json_extract(data_json, '$.summary.lost_percent') AS REAL)) AS avg_loss,
                        MAX(CAST(json_extract(data_json, '$.summary.lost_percent') AS REAL)) AS max_loss
                    FROM results
                    WHERE type LIKE 'wifi_udp%'
                      AND json_extract(data_json, '$.summary.lost_percent') IS NOT NULL
                    """
                ).fetchone()
            except sqlite3.Error:
                udp_loss_row = {"samples": 0, "avg_loss": None, "max_loss": None}
            try:
                tcp_retransmit_row = self.conn.execute(
                    """
                    SELECT
                        COUNT(*) AS samples,
                        SUM(CAST(json_extract(data_json, '$.summary.retransmits') AS INTEGER)) AS retransmits_total,
                        MAX(CAST(json_extract(data_json, '$.summary.retransmits') AS INTEGER)) AS retransmits_max,
                        SUM(CAST(json_extract(data_json, '$.summary.sent_bytes') AS REAL)) AS sent_bytes
                    FROM results
                    WHERE (type LIKE 'wifi_tcp%' OR type='wifi_iperf3_tcp')
                      AND json_extract(data_json, '$.summary.retransmits') IS NOT NULL
                    """
                ).fetchone()
            except sqlite3.Error:
                tcp_retransmit_row = {"samples": 0, "retransmits_total": None, "retransmits_max": None, "sent_bytes": None}
            tcp_sent_gb = float(tcp_retransmit_row["sent_bytes"] or 0) / 1_000_000_000
            tcp_sent_bytes = float(tcp_retransmit_row["sent_bytes"] or 0)
            tcp_retransmits_total = int(tcp_retransmit_row["retransmits_total"] or 0)
            tcp_retransmits_per_gb = tcp_retransmits_total / tcp_sent_gb if tcp_sent_gb > 0 else None
            tcp_retransmit_mss_bytes = 1448
            tcp_retransmit_segment_ratio_percent = (
                tcp_retransmits_total * tcp_retransmit_mss_bytes / tcp_sent_bytes * 100 if tcp_sent_bytes > 0 else None
            )
            wifi_total = sum(int(row["total"] or 0) for row in type_rows if str(row["type"]).startswith("wifi_"))
            bt_total = sum(int(row["total"] or 0) for row in type_rows if str(row["type"]).startswith("bt_"))
            tcp_total = sum(int(row["total"] or 0) for row in type_rows if str(row["type"]).startswith("wifi_tcp"))
            udp_total = sum(int(row["total"] or 0) for row in type_rows if str(row["type"]).startswith("wifi_udp"))
            result_stats = {
                "total": total_row["c"],
                "passed_total": self.conn.execute(
                    "SELECT COUNT(*) AS c FROM results WHERE status='passed'"
                ).fetchone()["c"],
                "failed_total": self.conn.execute(
                    f"SELECT COUNT(*) AS c FROM results WHERE {failed_where}"
                ).fetchone()["c"],
                "failed_24h": self.conn.execute(
                    f"SELECT COUNT(*) AS c FROM results WHERE {failed_where} AND timestamp_ms>=?",
                    (cutoff_24h,),
                ).fetchone()["c"],
                "failed_by_type": [
                    {"type": row["type"] or "unknown", "count": row["c"]}
                    for row in self.conn.execute(
                        f"""
                        SELECT COALESCE(type, 'unknown') AS type, COUNT(*) AS c
                        FROM results
                        WHERE {failed_where}
                        GROUP BY COALESCE(type, 'unknown')
                        ORDER BY c DESC, type
                        LIMIT 20
                        """
                    )
                ],
                "first_result_ms": total_row["first_ms"],
                "last_result_ms": total_row["last_ms"],
                "wifi_total": wifi_total,
                "bt_total": bt_total,
                "tcp_total": tcp_total,
                "udp_total": udp_total,
                "udp_loss_samples": udp_loss_row["samples"],
                "udp_avg_loss_percent": udp_loss_row["avg_loss"],
                "udp_max_loss_percent": udp_loss_row["max_loss"],
                "tcp_retransmit_samples": tcp_retransmit_row["samples"],
                "tcp_retransmits_total": tcp_retransmits_total,
                "tcp_retransmits_max": tcp_retransmit_row["retransmits_max"],
                "tcp_sent_bytes": tcp_retransmit_row["sent_bytes"],
                "tcp_retransmits_per_gb": tcp_retransmits_per_gb,
                "tcp_retransmit_mss_bytes": tcp_retransmit_mss_bytes,
                "tcp_retransmit_segment_ratio_percent": tcp_retransmit_segment_ratio_percent,
                "metric_samples": self.conn.execute("SELECT COUNT(*) AS c FROM metrics").fetchone()["c"],
                "log_entries": self.conn.execute("SELECT COUNT(*) AS c FROM logs").fetchone()["c"],
                "by_type": type_rows,
            }
            artifact_count = self.conn.execute("SELECT COUNT(*) AS c FROM artifacts").fetchone()["c"]
            incidents = self.recent_incidents(limit=20)
        return {
            "timestamp_ms": now_ms(),
            "boards": boards,
            "latest_metrics": latest_metrics,
            "recent_logs": logs,
            "recent_results": results,
            "recent_events": events,
            "recent_incidents": incidents,
            "result_stats": result_stats,
            "artifact_count": artifact_count,
            "schedule": schedule,
        }


store = Store(settings.db_path)
app = FastAPI(title="TaishanPi Burn-in Server", version="0.2.0")
app.mount(f"{settings.dashboard_path}/static", StaticFiles(directory=str(STATIC_DIR)), name="dashboard-static")
if settings.public_dashboard_path != settings.dashboard_path:
    app.mount(f"{settings.public_dashboard_path}/static", StaticFiles(directory=str(STATIC_DIR)), name="public-dashboard-static")


def now_ms() -> int:
    return int(time.time() * 1000)


def dump_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=True, separators=(",", ":"))


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")[:160] or "artifact"


def require_board_id(payload: dict[str, Any]) -> str:
    board_id = str(payload.get("board_id") or "").strip()
    if not board_id:
        raise HTTPException(status_code=400, detail="board_id is required")
    return board_id


def extract_ips(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    network = payload.get("network") or {}
    interfaces = network.get("interfaces") or {}
    wifi_iface = ((payload.get("wifi") or {}).get("interface")) or "wlan0"
    wifi_ip = first_ipv4((interfaces.get(wifi_iface) or {}).get("ipv4"))
    wired_ip = None
    for name, item in interfaces.items():
        if name == wifi_iface or name == "lo":
            continue
        wired_ip = first_ipv4((item or {}).get("ipv4"))
        if wired_ip:
            break
    return wifi_ip, wired_ip


def first_ipv4(values: Any) -> str | None:
    if isinstance(values, list) and values:
        return str(values[0])
    if isinstance(values, str):
        return values
    return None


def compact_metric(metric: dict[str, Any]) -> dict[str, Any]:
    if not metric:
        return {}
    system = metric.get("system") or {}
    wifi = metric.get("wifi") or {}
    bluetooth = metric.get("bluetooth") or {}
    network = metric.get("network") or {}
    return {
        "timestamp_ms": metric.get("timestamp_ms"),
        "board_id": metric.get("board_id"),
        "boot_id": metric.get("boot_id"),
        "uptime_sec": system.get("uptime_sec"),
        "loadavg": system.get("loadavg"),
        "mem_total": system.get("mem_total"),
        "mem_available": system.get("mem_available"),
        "max_temp_c": max_temp_c(metric.get("thermal") or []),
        "wifi": {
            "interface": wifi.get("interface"),
            "ipv4": wifi.get("ipv4"),
            "connected": wifi.get("connected"),
            "ssid": wifi.get("ssid"),
            "bssid": wifi.get("bssid"),
            "signal_dbm": wifi.get("signal_dbm"),
            "tx_bitrate": wifi.get("tx_bitrate"),
            "rx_bitrate": wifi.get("rx_bitrate"),
            "txpower_dbm": wifi.get("txpower_dbm"),
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
            "uplink_interface": network.get("uplink_interface"),
            "wifi_interface": network.get("wifi_interface"),
            "uplink_ip": network.get("uplink_ip"),
            "wifi_ip": network.get("wifi_ip"),
            "uplink_ready": network.get("uplink_ready"),
        },
    }


def metric_trend_point(board_id: str, timestamp_ms: int, metric: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
    wifi = metric.get("wifi") or {}
    network = metric.get("network") or {}
    interfaces = network.get("interfaces") or {}
    wifi_iface = wifi.get("interface") or network.get("wifi_interface") or "wlan0"
    wlan = interfaces.get(wifi_iface) or {}
    system = metric.get("system") or {}
    bluetooth = metric.get("bluetooth") or {}
    temp_c = max_temp_c(metric.get("thermal") or [])
    mem_total = number_or_none(system.get("mem_total"))
    mem_available = number_or_none(system.get("mem_available"))
    mem_used_percent = None
    if mem_total and mem_available is not None:
        mem_used_percent = max(0.0, min(100.0, (mem_total - mem_available) / mem_total * 100.0))
    rx_bytes = number_or_none(wlan.get("rx_bytes"))
    tx_bytes = number_or_none(wlan.get("tx_bytes"))
    rx_bps = None
    tx_bps = None
    if previous:
        prev_metric = previous.get("metric") or {}
        prev_wifi = prev_metric.get("wifi") or {}
        prev_network = prev_metric.get("network") or {}
        prev_ifaces = prev_network.get("interfaces") or {}
        prev_iface = prev_wifi.get("interface") or prev_network.get("wifi_interface") or "wlan0"
        prev_wlan = prev_ifaces.get(prev_iface) or {}
        prev_rx = number_or_none(prev_wlan.get("rx_bytes"))
        prev_tx = number_or_none(prev_wlan.get("tx_bytes"))
        elapsed_sec = max(1.0, (timestamp_ms - int(previous.get("timestamp_ms") or timestamp_ms)) / 1000.0)
        if rx_bytes is not None and prev_rx is not None and rx_bytes >= prev_rx:
            rx_bps = (rx_bytes - prev_rx) * 8.0 / elapsed_sec
        if tx_bytes is not None and prev_tx is not None and tx_bytes >= prev_tx:
            tx_bps = (tx_bytes - prev_tx) * 8.0 / elapsed_sec
    loadavg = system.get("loadavg") or []
    bt_rx_errors = int(number_or_none(bluetooth.get("rx_errors")) or 0)
    bt_tx_errors = int(number_or_none(bluetooth.get("tx_errors")) or 0)
    return {
        "board_id": board_id,
        "timestamp_ms": timestamp_ms,
        "temp_c": temp_c,
        "signal_dbm": number_or_none(wifi.get("signal_dbm")),
        "wifi_rx_bps": rx_bps,
        "wifi_tx_bps": tx_bps,
        "mem_used_percent": mem_used_percent,
        "load1": number_or_none(loadavg[0]) if loadavg else None,
        "bt_errors": bt_rx_errors + bt_tx_errors,
        "wifi_tx_retries": number_or_none(wifi.get("tx_retries")),
        "wifi_tx_failed": number_or_none(wifi.get("tx_failed")),
    }


def result_trend_point(board_id: str, timestamp_ms: int, result: dict[str, Any]) -> dict[str, Any]:
    summary = result.get("summary") or {}
    throughput = (
        summary.get("received_bits_per_second")
        or summary.get("sent_bits_per_second")
        or summary.get("bits_per_second")
    )
    return {
        "board_id": board_id,
        "timestamp_ms": timestamp_ms,
        "type": result.get("type"),
        "status": result.get("status"),
        "throughput_bps": number_or_none(throughput),
        "loss_percent": number_or_none(summary.get("lost_percent")),
        "wifi_path_ok": summary.get("wifi_path_ok"),
    }


def max_temp_c(zones: list[dict[str, Any]]) -> float | None:
    temps = [number_or_none(zone.get("temp_millic")) for zone in zones]
    temps = [temp for temp in temps if temp is not None]
    if not temps:
        return None
    return max(temps) / 1000.0


def number_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except Exception:
        return None


def dashboard_html(mode: str) -> HTMLResponse:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    html = html.replace('data-dashboard-mode="admin"', f'data-dashboard-mode="{mode}"', 1)
    if mode == "readonly":
        html = re.sub(r"\n\s*<!-- ADMIN_PANEL_START -->.*?<!-- ADMIN_PANEL_END -->\s*\n", "\n", html, count=1, flags=re.S)
    return HTMLResponse(html)


def test_progress_percent(start_ms: int, duration_sec: int) -> float:
    if not start_ms or duration_sec <= 0:
        return 0.0
    elapsed = max(0, now_ms() - start_ms)
    return min(100.0, elapsed / (duration_sec * 1000) * 100.0)


def board_bt_mac(board: dict[str, Any]) -> str | None:
    heartbeat = board.get("last_heartbeat") or {}
    bluetooth = heartbeat.get("bluetooth") or {}
    address = str(bluetooth.get("address") or "").strip()
    return address or None


async def require_auth(request: Request) -> None:
    if not settings.api_token:
        return
    token = request_token(request)
    if token != settings.api_token:
        raise HTTPException(status_code=401, detail="invalid token")


def request_token(request: Request) -> str:
    header = request.headers.get("x-burnin-token") or ""
    auth = request.headers.get("authorization") or ""
    return header or auth.removeprefix("Bearer ").strip()


@app.get("/")
async def root() -> PlainTextResponse:
    return PlainTextResponse(
        f"TaishanPi burn-in dashboard: {settings.dashboard_path}/\n"
        f"Read-only dashboard: {settings.public_dashboard_path}/\n",
        status_code=404,
    )


@app.get(settings.dashboard_path)
async def dashboard_redirect() -> RedirectResponse:
    return RedirectResponse(f"{settings.dashboard_path}/", status_code=307)


@app.get(f"{settings.dashboard_path}/")
async def dashboard_index() -> HTMLResponse:
    return dashboard_html("admin")


if settings.public_dashboard_path != settings.dashboard_path:

    @app.get(settings.public_dashboard_path)
    async def public_dashboard_redirect() -> RedirectResponse:
        return RedirectResponse(f"{settings.public_dashboard_path}/", status_code=307)

    @app.get(f"{settings.public_dashboard_path}/")
    async def public_dashboard_index() -> HTMLResponse:
        return dashboard_html("readonly")


@app.get("/api/v1/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "timestamp_ms": now_ms()}


@app.post("/api/v1/admin/history/clear")
async def admin_clear_history(payload: dict[str, Any]) -> dict[str, Any]:
    board_id = str(payload.get("board_id") or "").strip() or None
    return {"ok": True, "deleted": store.clear_history(board_id)}


@app.post("/api/v1/admin/boards/delete")
async def admin_delete_board(payload: dict[str, Any]) -> dict[str, Any]:
    board_id = require_board_id(payload)
    delete_history = bool(payload.get("delete_history", True))
    return {"ok": True, "board_id": board_id, "deleted": store.delete_board(board_id, delete_history)}


@app.post("/api/v1/admin/schedule")
async def admin_set_schedule(payload: dict[str, Any]) -> dict[str, Any]:
    start_ms = int(payload.get("start_ms") or 0)
    end_ms = int(payload.get("end_ms") or 0)
    return {"ok": True, "schedule": store.set_schedule(start_ms, end_ms)}


@app.post("/api/v1/admin/schedule/clear")
async def admin_clear_schedule() -> dict[str, Any]:
    return {"ok": True, "schedule": store.set_schedule(0, 0)}


@app.post("/api/v1/agent/register", dependencies=[Depends(require_auth)])
async def register(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    store.upsert_board(payload, request.client.host if request.client else None)
    return {"ok": True}


@app.post("/api/v1/agent/heartbeat", dependencies=[Depends(require_auth)])
async def heartbeat(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    store.upsert_board(payload, request.client.host if request.client else None)
    return {"ok": True}


@app.post("/api/v1/metrics/batch", dependencies=[Depends(require_auth)])
async def metrics_batch(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    metrics = payload.get("metrics") or []
    for item in metrics:
        if "board_id" not in item and "board_id" in payload:
            item["board_id"] = payload["board_id"]
        if "boot_id" not in item and "boot_id" in payload:
            item["boot_id"] = payload["boot_id"]
    store.insert_metrics(metrics)
    if metrics:
        store.upsert_board(metrics[-1], request.client.host if request.client else None)
    return {"ok": True, "count": len(metrics)}


@app.post("/api/v1/logs/batch", dependencies=[Depends(require_auth)])
async def logs_batch(payload: dict[str, Any]) -> dict[str, Any]:
    logs = payload.get("logs") or []
    for item in logs:
        if "board_id" not in item and "board_id" in payload:
            item["board_id"] = payload["board_id"]
        if "boot_id" not in item and "boot_id" in payload:
            item["boot_id"] = payload["boot_id"]
    store.insert_logs(logs)
    return {"ok": True, "count": len(logs)}


@app.post("/api/v1/events/batch", dependencies=[Depends(require_auth)])
async def events_batch(payload: dict[str, Any]) -> dict[str, Any]:
    events = payload.get("events") or []
    for item in events:
        if "board_id" not in item and "board_id" in payload:
            item["board_id"] = payload["board_id"]
        if "boot_id" not in item and "boot_id" in payload:
            item["boot_id"] = payload["boot_id"]
    return {"ok": True, "count": store.insert_events(events)}


@app.post("/api/v1/results/batch", dependencies=[Depends(require_auth)])
async def results_batch(payload: dict[str, Any]) -> dict[str, Any]:
    store.insert_results(payload)
    return {"ok": True, "count": len(payload.get("results") or [])}


@app.post("/api/v1/crash/upload", dependencies=[Depends(require_auth)])
async def crash_upload(payload: dict[str, Any]) -> dict[str, Any]:
    count = store.insert_artifacts(payload)
    event = {
        "board_id": payload.get("board_id"),
        "boot_id": payload.get("boot_id"),
        "timestamp_ms": payload.get("timestamp_ms") or now_ms(),
        "level": "warn",
        "message": f"uploaded {count} startup/crash artifacts",
    }
    if count:
        store.insert_logs([event])
    return {"ok": True, "count": count}


@app.get("/api/v1/agent/commands", dependencies=[Depends(require_auth)])
async def commands(board_id: str, boot_id: str | None = None) -> dict[str, Any]:
    return {"commands": compute_commands(board_id)}


@app.get("/api/v1/diagnostics")
async def diagnostics_index(board_id: str | None = None, limit: int = 50) -> dict[str, Any]:
    return store.diagnostics_summary(board_id=board_id, limit=limit)


@app.get("/api/v1/diagnostics/summary")
async def diagnostics_summary(board_id: str | None = None, limit: int = 50) -> dict[str, Any]:
    return store.diagnostics_summary(board_id=board_id, limit=limit)


@app.get("/api/v1/diagnostics/boards")
async def diagnostics_boards() -> dict[str, Any]:
    boards = store.list_boards()
    store.maintain_offline_incidents(boards)
    return {
        "timestamp_ms": now_ms(),
        "boards": boards,
        "incidents": store.recent_incidents(limit=100),
    }


@app.get("/api/v1/diagnostics/boards/{board_id}")
async def diagnostics_board(board_id: str, limit: int = 200) -> dict[str, Any]:
    return store.board_diagnostics(board_id, limit=limit)


@app.get("/api/v1/diagnostics/events")
async def diagnostics_events(
    board_id: str | None = None,
    command_id: str | None = None,
    event_type: str | None = None,
    severity: str | None = None,
    since_ms: int | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    return {
        "timestamp_ms": now_ms(),
        "events": store.recent_events(
            board_id=board_id,
            command_id=command_id,
            event_type=event_type,
            severity=severity,
            since_ms=since_ms,
            limit=limit,
        ),
    }


@app.get("/api/v1/diagnostics/failures")
async def diagnostics_failures(board_id: str | None = None, limit: int = 100, raw: bool = False) -> dict[str, Any]:
    return {
        "timestamp_ms": now_ms(),
        "failures": store.recent_failures(board_id=board_id, limit=limit, raw=raw),
    }


@app.get("/api/v1/diagnostics/logs")
async def diagnostics_logs(board_id: str | None = None, since_ms: int | None = None, limit: int = 200) -> dict[str, Any]:
    return {
        "timestamp_ms": now_ms(),
        "logs": store.recent_logs(board_id=board_id, since_ms=since_ms, limit=limit),
    }


@app.get("/api/v1/diagnostics/metrics")
async def diagnostics_metrics(
    board_id: str | None = None,
    since_ms: int | None = None,
    limit: int = 200,
    compact: bool = True,
) -> dict[str, Any]:
    return {
        "timestamp_ms": now_ms(),
        "metrics": store.recent_metrics(board_id=board_id, since_ms=since_ms, limit=limit, compact=compact),
    }


@app.get("/api/v1/diagnostics/incidents")
async def diagnostics_incidents(board_id: str | None = None, limit: int = 100) -> dict[str, Any]:
    boards = store.list_boards()
    store.maintain_offline_incidents(boards)
    return {
        "timestamp_ms": now_ms(),
        "incidents": store.recent_incidents(board_id=board_id, limit=limit),
    }


@app.get("/api/v1/diagnostics/commands/{command_id}")
async def diagnostics_command(command_id: str) -> dict[str, Any]:
    with store.lock:
        rows = store.conn.execute(
            "SELECT data_json FROM results WHERE command_id=? ORDER BY timestamp_ms DESC, id DESC",
            (command_id,),
        ).fetchall()
        results = [dashboard_result(json.loads(row["data_json"])) for row in rows]
        raw_results = [json.loads(row["data_json"]) for row in rows]
    return {
        "timestamp_ms": now_ms(),
        "command_id": command_id,
        "events": store.recent_events(command_id=command_id, limit=500),
        "results": results,
        "raw_results": raw_results,
    }


@app.get(f"{settings.dashboard_path}/api/snapshot")
async def dashboard_snapshot() -> JSONResponse:
    return JSONResponse(store.snapshot())


@app.get(f"{settings.dashboard_path}/api/trends")
async def dashboard_trends(hours: int = 24) -> JSONResponse:
    return JSONResponse(store.trend_snapshot(hours))


@app.websocket(f"{settings.dashboard_path}/ws/dashboard")
async def dashboard_ws(websocket: WebSocket) -> None:
    await stream_dashboard(websocket)


if settings.public_dashboard_path != settings.dashboard_path:

    @app.get(f"{settings.public_dashboard_path}/api/snapshot")
    async def public_dashboard_snapshot() -> JSONResponse:
        return JSONResponse(store.snapshot())

    @app.get(f"{settings.public_dashboard_path}/api/trends")
    async def public_dashboard_trends(hours: int = 24) -> JSONResponse:
        return JSONResponse(store.trend_snapshot(hours))

    @app.websocket(f"{settings.public_dashboard_path}/ws/dashboard")
    async def public_dashboard_ws(websocket: WebSocket) -> None:
        await stream_dashboard(websocket)


async def stream_dashboard(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        while True:
            await websocket.send_json(store.snapshot())
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        return


async def purge_history_task() -> None:
    """Periodically purge high-frequency telemetry to prevent SQLite bloat."""
    while True:
        await asyncio.sleep(3600)
        try:
            deleted_metrics = store.purge_old_metrics()
            deleted_events = store.purge_old_events()
            deleted = deleted_metrics + deleted_events
            if deleted:
                store.insert_logs([{
                    "board_id": "server",
                    "timestamp_ms": now_ms(),
                    "level": "info",
                    "message": f"purged old telemetry rows metrics={deleted_metrics} events={deleted_events}",
                }])
        except Exception:
            pass


@app.on_event("startup")
async def on_startup() -> None:
    asyncio.create_task(purge_history_task())


def compute_commands(board_id: str) -> list[dict[str, Any]]:
    commands_out: list[dict[str, Any]] = []
    if not store.schedule().get("commands_enabled"):
        return commands_out
    boards = [board for board in store.list_boards() if board.get("online") and board.get("wifi_ip")]
    boards.sort(key=lambda item: item["board_id"])
    ids = [board["board_id"] for board in boards]
    if board_id in ids and len(boards) >= 2:
        index = ids.index(board_id)
        now_sec = time.time()
        epoch = int(now_sec // settings.wifi_epoch_sec)
        epoch_start_sec = epoch * settings.wifi_epoch_sec
        epoch_end_sec = epoch_start_sec + settings.wifi_epoch_sec
        elapsed_sec = now_sec - epoch_start_sec
        remaining_sec = epoch_end_sec - now_sec
        peer = peer_for_epoch(boards, index, epoch)
        mode = wifi_mode_for_epoch(epoch)
        mode_type = str(mode.get("type") or "")
        should_start_wifi = (
            elapsed_sec <= max(1, settings.wifi_start_grace_sec)
            and remaining_sec > max(5, settings.wifi_gap_sec + 5)
            and wifi_should_initiate(mode_type, index, epoch, len(boards))
        )
        if peer and should_start_wifi:
            duration_sec = min(
                int(mode.get("duration_sec", max(5, settings.wifi_epoch_sec - settings.wifi_gap_sec))),
                max(5, int(remaining_sec - max(0, settings.wifi_gap_sec))),
            )
            command = {
                "id": f"{mode['type']}-e{epoch}-{board_id}-to-{peer['board_id']}",
                "type": mode["type"],
                "peer_board_id": peer["board_id"],
                "peer_ip": peer["wifi_ip"],
                "port": settings.iperf3_port,
                "duration_sec": duration_sec,
                "initiator_index": index,
                "initiator_count": len(boards),
            }
            command.update({key: value for key, value in mode.items() if key not in {"type", "duration_sec"}})
            commands_out.append(command)
    if board_id in ids and boards:
        bt_epoch = int(time.time() // settings.bt_period_sec)
        index = ids.index(board_id)
        bt_peer = peer_for_epoch(boards, index, bt_epoch)
        commands_out.extend(bt_commands_for_epoch(board_id, bt_epoch, index, len(boards), bt_peer))
    return commands_out


def peer_for_epoch(boards: list[dict[str, Any]], index: int, epoch: int) -> dict[str, Any] | None:
    if len(boards) < 2:
        return None
    offset = 1 + (epoch % (len(boards) - 1))
    return boards[(index + offset) % len(boards)]


def wifi_should_initiate(mode_type: str, board_index: int, epoch: int, board_count: int) -> bool:
    if board_count <= 1:
        return False
    if mode_type == "wifi_ping":
        return True
    if mode_type.startswith("wifi_"):
        return board_index == (epoch % board_count)
    return False


def wifi_mode_for_epoch(epoch: int) -> dict[str, Any]:
    epoch_budget = max(5, settings.wifi_epoch_sec - max(0, settings.wifi_gap_sec))
    duration = max(5, min(settings.wifi_tcp_sec, epoch_budget))
    modes = [
        {"type": "wifi_tcp_single", "duration_sec": duration, "parallel": 1},
        {"type": "wifi_tcp_multi", "duration_sec": duration, "parallel": settings.wifi_tcp_parallel},
        {"type": "wifi_tcp_reverse", "duration_sec": duration, "parallel": settings.wifi_tcp_parallel},
        {"type": "wifi_tcp_bidir", "duration_sec": duration, "parallel": max(2, settings.wifi_tcp_parallel // 2)},
        {
            "type": "wifi_udp_flood",
            "duration_sec": max(settings.wifi_udp_min_sec, duration),
            "bandwidth": settings.wifi_udp_flood_bandwidth,
            "adaptive": True,
            "adaptive_rates": settings.wifi_udp_flood_rates,
            "adaptive_probe_sec": settings.wifi_udp_adaptive_probe_sec,
            "adaptive_max_loss_percent": settings.wifi_udp_adaptive_max_loss_percent,
        },
        {
            "type": "wifi_udp_small",
            "duration_sec": max(settings.wifi_udp_min_sec, duration),
            "bandwidth": settings.wifi_udp_small_bandwidth,
            "length": 256,
            "adaptive": True,
            "adaptive_rates": settings.wifi_udp_small_rates,
            "adaptive_probe_sec": settings.wifi_udp_adaptive_probe_sec,
            "adaptive_max_loss_percent": settings.wifi_udp_adaptive_max_loss_percent,
        },
        {
            "type": "wifi_udp_large",
            "duration_sec": max(settings.wifi_udp_min_sec, duration),
            "bandwidth": settings.wifi_udp_large_bandwidth,
            "length": 1400,
            "adaptive": True,
            "adaptive_rates": settings.wifi_udp_large_rates,
            "adaptive_probe_sec": settings.wifi_udp_adaptive_probe_sec,
            "adaptive_max_loss_percent": settings.wifi_udp_adaptive_max_loss_percent,
        },
        {"type": "wifi_ping", "duration_sec": 5, "count": 20},
    ]
    return modes[epoch % len(modes)]


def bt_commands_for_epoch(board_id: str, epoch: int, index: int, board_count: int, peer: dict[str, Any] | None) -> list[dict[str, Any]]:
    phase = epoch % 5
    duration = settings.bt_duration_sec
    if phase == 0:
        role = "advertise" if index % 2 == 0 else "scan"
        return [{"id": f"bt-ble-e{epoch}-{board_id}-{role}", "type": f"bt_ble_{role}", "role": role, "duration_sec": duration}]
    if phase == 1:
        role = "scan" if index % 2 == 0 else "advertise"
        return [{"id": f"bt-ble-e{epoch}-{board_id}-{role}", "type": f"bt_ble_{role}", "role": role, "duration_sec": duration}]
    if phase == 2:
        return [{"id": f"bt-bredr-e{epoch}-{board_id}", "type": "bt_bredr_inquiry", "duration_sec": duration}]
    peer_mac = board_bt_mac(peer or {})
    if phase == 3 and peer_mac:
        if board_count >= 2 and index != epoch % board_count:
            return []
        return [{"id": f"bt-l2ping-e{epoch}-{board_id}-to-{peer['board_id']}", "type": "bt_l2ping", "peer_board_id": peer["board_id"], "peer_bt_mac": peer_mac, "count": 20, "size": 600}]
    if phase == 4 and peer_mac:
        role = "server" if (index + epoch) % 2 == 0 else "client"
        command = {
            "id": f"bt-l2test-e{epoch}-{board_id}-{role}",
            "type": "bt_l2test",
            "role": role,
            "duration_sec": settings.bt_l2test_duration_sec,
            "bytes": settings.bt_l2test_bytes,
            "frames": settings.bt_l2test_frames,
            "delay_ms": settings.bt_l2test_delay_ms,
            "client_delay_sec": settings.bt_l2test_client_delay_sec,
            "startup_grace_sec": settings.bt_l2test_startup_grace_sec,
        }
        if settings.bt_l2test_psm:
            command["psm"] = settings.bt_l2test_psm
        if role == "client":
            command.update({"peer_board_id": peer["board_id"], "peer_bt_mac": peer_mac})
        return [command]
    return [{"id": f"bt-ble-fallback-e{epoch}-{board_id}", "type": "bt_ble_scan", "role": "scan", "duration_sec": duration}]
