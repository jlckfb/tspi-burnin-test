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
    wifi_tcp_parallel = int(os.environ.get("BURNIN_WIFI_TCP_PARALLEL", "4"))
    wifi_udp_bandwidth = os.environ.get("BURNIN_WIFI_UDP_BANDWIDTH", "0")
    wifi_udp_flood_bandwidth = os.environ.get("BURNIN_WIFI_UDP_FLOOD_BANDWIDTH", "200M")
    wifi_udp_small_bandwidth = os.environ.get("BURNIN_WIFI_UDP_SMALL_BANDWIDTH", "60M")
    wifi_udp_large_bandwidth = os.environ.get("BURNIN_WIFI_UDP_LARGE_BANDWIDTH", "120M")
    wifi_udp_flood_rates = os.environ.get("BURNIN_WIFI_UDP_FLOOD_RATES", "50M,100M,150M,200M")
    wifi_udp_small_rates = os.environ.get("BURNIN_WIFI_UDP_SMALL_RATES", "5M,10M,20M,30M,40M,60M")
    wifi_udp_large_rates = os.environ.get("BURNIN_WIFI_UDP_LARGE_RATES", "30M,60M,90M,120M,160M,200M")
    wifi_udp_adaptive_probe_sec = int(os.environ.get("BURNIN_WIFI_UDP_ADAPTIVE_PROBE_SEC", "5"))
    wifi_udp_adaptive_max_loss_percent = float(os.environ.get("BURNIN_WIFI_UDP_ADAPTIVE_MAX_LOSS_PERCENT", "5"))
    wifi_udp_min_sec = int(os.environ.get("BURNIN_WIFI_UDP_MIN_SEC", "10"))
    iperf3_port = int(os.environ.get("BURNIN_IPERF3_PORT", "5201"))
    bt_period_sec = int(os.environ.get("BURNIN_BT_PERIOD_SEC", "60"))
    bt_duration_sec = int(os.environ.get("BURNIN_BT_DURATION_SEC", "20"))
    bt_l2test_duration_sec = int(os.environ.get("BURNIN_BT_L2TEST_DURATION_SEC", "25"))
    bt_l2test_frames = int(os.environ.get("BURNIN_BT_L2TEST_FRAMES", "1000000"))
    bt_l2test_bytes = int(os.environ.get("BURNIN_BT_L2TEST_BYTES", "600"))
    bt_l2test_delay_ms = int(os.environ.get("BURNIN_BT_L2TEST_DELAY_MS", "20"))
    bt_l2test_psm = os.environ.get("BURNIN_BT_L2TEST_PSM", "").strip()
    dashboard_path = "/" + os.environ.get("BURNIN_DASHBOARD_PATH", "/tspi-burnin").strip("/")
    test_start_ms = int(os.environ.get("BURNIN_TEST_START_MS", "0") or "0")
    test_duration_sec = int(os.environ.get("BURNIN_TEST_DURATION_SEC", str(7 * 24 * 3600)))
    metrics_retain_hours = int(os.environ.get("BURNIN_METRICS_RETAIN_HOURS", "24"))
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


def dashboard_result(item: dict[str, Any]) -> dict[str, Any]:
    summary = item.get("summary") or {}
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
        "iperf3_attempts",
        "iperf3_retry_reason",
        "udp_bandwidth",
        "udp_length",
        "adaptive_udp",
        "adaptive_selected_bandwidth",
        "adaptive_probe_sec",
        "adaptive_max_loss_percent",
        "adaptive_probe_count",
        "adaptive_probe_summary",
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
        "btmon_captured",
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
        "error": truncate_text(item.get("error") or item.get("stderr"), 220),
        "summary": {key: summary.get(key) for key in keep_summary_keys if key in summary},
    }


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
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

    def insert_results(self, payload: dict[str, Any]) -> None:
        board_id = payload.get("board_id")
        boot_id = payload.get("boot_id")
        rows = []
        for item in payload.get("results", []):
            item.setdefault("board_id", board_id)
            item.setdefault("boot_id", boot_id)
            item.setdefault("timestamp_ms", item.get("finished_at_ms") or now_ms())
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
        with self.lock, self.conn:
            self.conn.executemany("INSERT INTO results(board_id, boot_id, command_id, type, status, timestamp_ms, data_json) VALUES(?,?,?,?,?,?,?)", rows)

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

    def clear_history(self, board_id: str | None = None) -> dict[str, int]:
        counts: dict[str, int] = {}
        params = (board_id,) if board_id else ()
        where = " WHERE board_id=?" if board_id else ""
        with self.lock, self.conn:
            artifact_paths = [
                str(row["path"])
                for row in self.conn.execute(f"SELECT path FROM artifacts{where}", params).fetchall()
            ]
            for table in ("metrics", "logs", "results", "artifacts"):
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
        return {
            "wifi_epoch_sec": settings.wifi_epoch_sec,
            "wifi_tcp_sec": settings.wifi_tcp_sec,
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

    def snapshot(self) -> dict[str, Any]:
        boards = self.list_boards()
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
        return {
            "timestamp_ms": now_ms(),
            "boards": boards,
            "latest_metrics": latest_metrics,
            "recent_logs": logs,
            "recent_results": results,
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


async def purge_metrics_task() -> None:
    """Periodically purge old metrics to prevent SQLite bloat."""
    while True:
        await asyncio.sleep(3600)
        try:
            deleted = store.purge_old_metrics()
            if deleted:
                store.insert_logs([{
                    "board_id": "server",
                    "timestamp_ms": now_ms(),
                    "level": "info",
                    "message": f"purged {deleted} old metrics rows",
                }])
        except Exception:
            pass


@app.on_event("startup")
async def on_startup() -> None:
    asyncio.create_task(purge_metrics_task())


def compute_commands(board_id: str) -> list[dict[str, Any]]:
    commands_out: list[dict[str, Any]] = []
    if not store.schedule().get("commands_enabled"):
        return commands_out
    boards = [board for board in store.list_boards() if board.get("online") and board.get("wifi_ip")]
    boards.sort(key=lambda item: item["board_id"])
    ids = [board["board_id"] for board in boards]
    if board_id in ids and len(boards) >= 2:
        index = ids.index(board_id)
        epoch = int(time.time() // settings.wifi_epoch_sec)
        peer = peer_for_epoch(boards, index, epoch)
        mode = wifi_mode_for_epoch(epoch)
        if peer:
            command = {
                "id": f"{mode['type']}-e{epoch}-{board_id}-to-{peer['board_id']}",
                "type": mode["type"],
                "peer_board_id": peer["board_id"],
                "peer_ip": peer["wifi_ip"],
                "port": settings.iperf3_port,
                "duration_sec": mode.get("duration_sec", max(5, settings.wifi_epoch_sec - 5)),
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


def wifi_mode_for_epoch(epoch: int) -> dict[str, Any]:
    epoch_budget = max(5, settings.wifi_epoch_sec - 60)
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
        }
        if settings.bt_l2test_psm:
            command["psm"] = settings.bt_l2test_psm
        if role == "client":
            command.update({"peer_board_id": peer["board_id"], "peer_bt_mac": peer_mac})
        return [command]
    return [{"id": f"bt-ble-fallback-e{epoch}-{board_id}", "type": "bt_ble_scan", "role": "scan", "duration_sec": duration}]
