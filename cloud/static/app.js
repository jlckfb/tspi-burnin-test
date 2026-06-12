const state = {
  socket: null,
  lastSnapshotAt: 0,
  previousWifiCounters: new Map(),
  boards: [],
};

const $ = (id) => document.getElementById(id);
const dashboardBase = location.pathname.replace(/\/$/, "");
const apiUrl = (path) => `${dashboardBase}${path}`;
const dashboardMode = document.body.dataset.dashboardMode || "admin";

function setText(id, value) {
  const node = $(id);
  if (node) node.textContent = value;
}

function fmtDateTime(ms) {
  if (!ms) return "-";
  return new Date(ms).toLocaleString("zh-CN", { hour12: false });
}

function fmtDateTimeInput(ms) {
  if (!ms) return "";
  const date = new Date(ms);
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 16);
}

function parseDateTimeInput(value) {
  if (!value) return 0;
  const ms = new Date(value).getTime();
  return Number.isFinite(ms) ? ms : 0;
}

function fmtTime(ms) {
  if (!ms) return "-";
  return new Date(ms).toLocaleTimeString("zh-CN", { hour12: false });
}

function fmtAge(ms) {
  if (!ms) return "-";
  const sec = Math.max(0, Math.round((Date.now() - ms) / 1000));
  if (sec < 60) return `${sec}秒前`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}分钟前`;
  const hour = Math.floor(min / 60);
  if (hour < 48) return `${hour}小时前`;
  return `${Math.floor(hour / 24)}天前`;
}

function fmtDuration(sec) {
  const value = Math.max(0, Number(sec || 0));
  const days = Math.floor(value / 86400);
  const hours = Math.floor((value % 86400) / 3600);
  const mins = Math.floor((value % 3600) / 60);
  if (days) return `${days}天${hours}小时`;
  if (hours) return `${hours}小时${mins}分`;
  if (mins) return `${mins}分钟`;
  return `${Math.floor(value)}秒`;
}

function fmtTempValue(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return `${Number(value).toFixed(1)}℃`;
}

function maxTemp(metric) {
  const zones = metric?.thermal || [];
  if (!zones.length) return null;
  return Math.max(...zones.map((z) => Number(z.temp_millic || 0))) / 1000;
}

function fmtBytes(bytes) {
  const value = Number(bytes || 0);
  if (value >= 1024 ** 3) return `${(value / 1024 ** 3).toFixed(1)}G`;
  if (value >= 1024 ** 2) return `${(value / 1024 ** 2).toFixed(1)}M`;
  if (value >= 1024) return `${(value / 1024).toFixed(1)}K`;
  return `${value}B`;
}

function fmtRate(value) {
  if (!value && value !== 0) return "-";
  const mbps = Number(value) / 1000000;
  if (mbps >= 100) return `${mbps.toFixed(0)} Mbps`;
  if (mbps >= 10) return `${mbps.toFixed(1)} Mbps`;
  return `${mbps.toFixed(2)} Mbps`;
}

function fmtPercent(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return `${Number(value).toFixed(1)}%`;
}

function fmtCount(value) {
  const count = Number(value || 0);
  return Number.isFinite(count) ? count.toLocaleString("zh-CN") : "0";
}

function fmtLossPercent(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  const loss = Number(value);
  if (loss === 0) return "0%";
  if (loss > 0 && loss < 0.01) return "<0.01%";
  if (loss < 1) return `${loss.toFixed(2)}%`;
  if (loss < 10) return `${loss.toFixed(1)}%`;
  return `${loss.toFixed(0)}%`;
}

function fmtRetransmitsPerGb(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  const rate = Number(value);
  if (rate === 0) return "0/GB";
  if (rate < 1) return "<1/GB";
  if (rate < 10) return `${rate.toFixed(1)}/GB`;
  return `${rate.toFixed(0)}/GB`;
}

function fmtTinyPercent(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  const pct = Number(value);
  if (pct === 0) return "0%";
  if (pct > 0 && pct < 0.001) return "<0.001%";
  if (pct < 0.1) return `${pct.toFixed(3)}%`;
  if (pct < 1) return `${pct.toFixed(2)}%`;
  return `${pct.toFixed(1)}%`;
}

function fmtLossOrRetransmit(summary) {
  if (summary?.lost_percent !== null && summary?.lost_percent !== undefined) {
    return fmtLossPercent(summary.lost_percent);
  }
  if (summary?.retransmits !== null && summary?.retransmits !== undefined) {
    const sentBytes = Number(summary.sent_bytes || 0);
    const ratio = sentBytes > 0 ? (Number(summary.retransmits || 0) * 1448 / sentBytes) * 100 : null;
    return `重传 ${fmtCount(summary.retransmits)}${ratio === null ? "" : ` / 约 ${fmtTinyPercent(ratio)}`}`;
  }
  return "-";
}

function firstValue(value, fallback = "-") {
  if (Array.isArray(value)) return value[0] || fallback;
  return value || fallback;
}

function pickIface(metric, name) {
  return metric?.network?.interfaces?.[name] || {};
}

function memText(metric) {
  const total = Number(metric?.system?.mem_total || 0);
  const available = Number(metric?.system?.mem_available || 0);
  if (!total) return "-";
  const used = Math.max(0, total - available);
  return `${fmtPercent((used / total) * 100)} / ${fmtBytes(total)}`;
}

function loadText(metric) {
  const load = metric?.system?.loadavg || [];
  if (!load.length) return "-";
  return load.slice(0, 3).map((v) => Number(v).toFixed(2)).join(" / ");
}

function wifiCounters(board, metric) {
  const wifi = metric?.wifi || board.last_heartbeat?.wifi || {};
  const wlan = pickIface(metric, wifi.interface || "wlan0");
  const rx = Number(wlan.rx_bytes || 0);
  const tx = Number(wlan.tx_bytes || 0);
  const ts = Number(metric?.timestamp_ms || Date.now());
  const key = board.board_id;
  const previous = state.previousWifiCounters.get(key);
  if (!previous) {
    state.previousWifiCounters.set(key, { rx, tx, ts, rxBps: null, txBps: null });
    return { rx, tx, rxBps: null, txBps: null };
  }
  if (ts <= previous.ts) {
    return { rx, tx, rxBps: previous?.rxBps ?? null, txBps: previous?.txBps ?? null };
  }
  if (rx < previous.rx || tx < previous.tx) {
    state.previousWifiCounters.set(key, { rx, tx, ts, rxBps: null, txBps: null });
    return { rx, tx, rxBps: null, txBps: null };
  }
  const elapsed = Math.max(1, (ts - previous.ts) / 1000);
  const rxBps = ((rx - previous.rx) * 8) / elapsed;
  const txBps = ((tx - previous.tx) * 8) / elapsed;
  state.previousWifiCounters.set(key, { rx, tx, ts, rxBps, txBps });
  return {
    rx,
    tx,
    rxBps,
    txBps,
  };
}

function boardHealth(board, metric) {
  const wifi = metric?.wifi || board.last_heartbeat?.wifi || {};
  const bt = metric?.bluetooth || {};
  const temp = maxTemp(metric);
  const problems = [];
  if (!board.online) problems.push("离线");
  if (board.online && wifi.connected === false) problems.push("WiFi未连接");
  if (board.online && bt.available === false) problems.push("BT不可用");
  if (temp !== null && temp >= 85) problems.push("温度过高");
  if (temp !== null && temp >= 75 && temp < 85) problems.push("温度偏高");
  if (problems.length) return { level: temp >= 75 && board.online ? "warn" : "bad", text: problems.join(" / ") };
  return { level: "good", text: "正常" };
}

function btStatus(bt = {}) {
  if (bt.busy && bt.up) return { text: "UP / 测试中", level: "warn" };
  if (bt.busy) return { text: "测试中", level: "warn" };
  if (bt.up) return { text: "UP", level: "good" };
  if (bt.available === true) return { text: "可用", level: "warn" };
  if (bt.available === false) return { text: "不可用", level: "bad" };
  return { text: "-", level: "warn" };
}

function boardCard(board, metric, rates) {
  const wifi = metric?.wifi || board.last_heartbeat?.wifi || {};
  const network = metric?.network || board.last_heartbeat?.network || {};
  const uplink = pickIface(metric, network.uplink_interface || "end0");
  const bt = metric?.bluetooth || {};
  const btState = btStatus(bt);
  const temp = maxTemp(metric);
  const health = boardHealth(board, metric);
  const cls = `board ${health.level === "bad" ? "offline" : health.level === "warn" ? "warn" : ""}`;
  return `
    <article class="${cls}">
      <div class="board-title">
        <h3>${escapeHtml(board.board_id)}</h3>
        <span class="badge ${health.level}">${escapeHtml(health.text)}</span>
      </div>
      <div class="metrics">
        <div class="metric"><span>最高温度</span><strong>${fmtTempValue(temp)}</strong></div>
        <div class="metric"><span>WiFi 信号</span><strong>${wifi.signal_dbm ?? "-"} dBm</strong></div>
        <div class="metric"><span>WiFi 接收</span><strong>${fmtRate(rates.rxBps)}</strong></div>
        <div class="metric"><span>WiFi 发送</span><strong>${fmtRate(rates.txBps)}</strong></div>
        <div class="metric"><span>链路速率</span><strong>${escapeHtml(wifi.tx_bitrate || "-")}</strong></div>
        <div class="metric"><span>BT 状态</span><strong>${escapeHtml(btState.text)}</strong></div>
        <div class="metric"><span>有线上报</span><strong>${escapeHtml(firstValue(uplink.ipv4, board.wired_ip || "-"))}</strong></div>
        <div class="metric"><span>内存</span><strong>${escapeHtml(memText(metric))}</strong></div>
      </div>
      <p class="board-foot">
        ${escapeHtml(board.hostname || "-")} | WiFi ${escapeHtml(board.wifi_ip || "-")} | BT ${escapeHtml(bt.address || "-")} | ${fmtAge(board.last_seen_ms)}
      </p>
    </article>`;
}

function render(snapshot) {
  state.lastSnapshotAt = Date.now();
  const boards = snapshot.boards || [];
  state.boards = boards;
  const metrics = snapshot.latest_metrics || {};
  const results = snapshot.recent_results || [];
  const logs = snapshot.recent_logs || [];
  const rows = boards.map((board) => {
    const metric = metrics[board.board_id] || {};
    return { board, metric, rates: wifiCounters(board, metric) };
  });

  renderHeader(snapshot, rows, results);
  renderSchedule(snapshot.schedule || {});
  renderOverview(snapshot);
  renderBoards(rows);
  renderBoardTable(rows);
  renderWifiTable(rows);
  renderBtTable(rows);
  renderIncidents(snapshot.recent_incidents || []);
  renderEvents(snapshot.recent_events || []);
  renderResults(results, snapshot.result_stats || {});
  renderLogs(logs);
  renderAdminBoards(boards);
}

function renderHeader(snapshot, rows, results) {
  const mode = dashboardMode === "readonly" ? "只读分享" : "主看板";
  setText("mode-badge", mode);
  if (dashboardMode === "readonly") document.body.classList.add("readonly");
  setText("clock", `更新时间 ${fmtDateTime(snapshot.timestamp_ms)}`);
  setText("online-count", rows.filter(({ board }) => board.online).length);
  setText("board-count", rows.length);
  const recentFailures = results.filter((item) => isBadStatus(item.status)).length;
  const failures = snapshot.result_stats?.failed_total ?? recentFailures;
  setText("fail-count", failures);
  const temps = rows.map(({ metric }) => maxTemp(metric)).filter((v) => v !== null);
  setText("max-temp-count", temps.length ? `${Math.max(...temps).toFixed(1)}℃` : "-");
  setText("artifact-count", snapshot.artifact_count || 0);
  const progress = Number(snapshot.schedule?.test_progress_percent || 0);
  setText("progress-count", `${progress.toFixed(1)}%`);
}

function renderSchedule(schedule) {
  const startMs = Number(schedule.test_start_ms || 0);
  const durationSec = Number(schedule.test_duration_sec || 0);
  const endMs = Number(schedule.test_end_ms || 0) || (startMs && durationSec > 0 ? startMs + durationSec * 1000 : 0);
  const now = Date.now();
  const progress = Number(schedule.test_progress_percent || 0);
  let status = "未设置计划";
  if (startMs && now < startMs) status = "等待开始";
  if (startMs && now >= startMs && (!endMs || now < endMs)) status = "运行中";
  if (endMs && now >= endMs) status = "已到期";
  setText("schedule-status", `${status} ${progress.toFixed(1)}%`);
  setText("schedule-start", fmtDateTime(startMs));
  setText("schedule-end", fmtDateTime(endMs));
  setText("schedule-remaining", endMs ? (now >= endMs ? "已超过截止时间" : `剩余 ${fmtDuration((endMs - now) / 1000)}`) : "-");
  setText("schedule-wifi", `${schedule.wifi_epoch_sec || "-"} 秒`);
  const currentMode = schedule.current_wifi_mode || {};
  const nextMode = (schedule.upcoming_wifi_modes || [])[1] || {};
  const modeText = currentMode.type
    ? `当前 ${labelType(currentMode.type)}${nextMode.type ? ` / 下一档 ${labelType(nextMode.type)}` : ""}`
    : `TCP ${schedule.wifi_tcp_sec || "-"} 秒`;
  setText("schedule-wifi-detail", `iperf3 ${schedule.iperf3_port || "-"} / ${modeText}`);
  setText("schedule-bt", `${schedule.bt_period_sec || "-"} 秒`);
  syncScheduleInputs(startMs, endMs);
}

function syncScheduleInputs(startMs, endMs) {
  if (dashboardMode === "readonly") return;
  const startInput = $("schedule-start-input");
  const endInput = $("schedule-end-input");
  if (!startInput || !endInput) return;
  if (document.activeElement !== startInput) {
    startInput.value = fmtDateTimeInput(startMs);
  }
  if (document.activeElement !== endInput) {
    endInput.value = fmtDateTimeInput(endMs);
  }
}

function renderOverview(snapshot) {
  const stats = snapshot.result_stats || {};
  const total = Number(stats.total || 0);
  const passed = Number(stats.passed_total || 0);
  const failed = Number(stats.failed_total || 0);
  const passRate = total ? fmtPercent((passed / total) * 100) : "-";
  const failed24h = Number(stats.failed_24h || 0);
  const firstMs = Number(stats.first_result_ms || 0);
  const udpSamples = Number(stats.udp_loss_samples || 0);
  const typeRows = stats.by_type || [];
  const l2cap = typeRows.find((row) => row.type === "bt_l2test");
  const windowText = firstMs ? `已累计 ${fmtDuration((Date.now() - firstMs) / 1000)}` : "等待结果";

  setText("overview-summary", `累计 ${fmtCount(total)} 条测试 / ${fmtCount(stats.metric_samples)} 条采样 / ${fmtCount(stats.log_entries)} 条日志`);
  setText("overview-total", fmtCount(total));
  setText("overview-window", windowText);
  setText("overview-passed", fmtCount(passed));
  setText("overview-pass-rate", `通过率 ${passRate}`);
  setText("overview-failed", fmtCount(failed));
  setText("overview-failed-24h", `最近24小时 ${fmtCount(failed24h)}`);
  setText("overview-wifi", fmtCount(stats.wifi_total));
  setText("overview-wifi-detail", `TCP ${fmtCount(stats.tcp_total)} / UDP ${fmtCount(stats.udp_total)}`);
  setText("overview-bt", fmtCount(stats.bt_total));
  setText("overview-bt-detail", `L2CAP 数据 ${fmtCount(l2cap?.total)}`);
  setText("overview-tcp-retransmits", Number(stats.tcp_retransmit_samples || 0) ? fmtTinyPercent(stats.tcp_retransmit_segment_ratio_percent) : "-");
  setText(
    "overview-tcp-retransmit-detail",
    Number(stats.tcp_retransmit_samples || 0)
      ? `累计 ${fmtCount(stats.tcp_retransmits_total)} / ${fmtRetransmitsPerGb(stats.tcp_retransmits_per_gb)} / 样本 ${fmtCount(stats.tcp_retransmit_samples)}`
      : "等待 TCP 结果"
  );
  setText("overview-udp-loss", udpSamples ? fmtLossPercent(stats.udp_avg_loss_percent) : "-");
  setText("overview-udp-loss-detail", udpSamples ? `最高 ${fmtLossPercent(stats.udp_max_loss_percent)} / 样本 ${fmtCount(udpSamples)}` : "等待 UDP 结果");
  setText("overview-metrics", fmtCount(stats.metric_samples));
  setText("overview-logs", `日志 ${fmtCount(stats.log_entries)}`);
  setText("overview-artifacts", fmtCount(snapshot.artifact_count));
  setText("overview-type-summary", `${typeRows.length} 类测试`);

  const typeBody = $("overview-types");
  if (!typeBody) return;
  typeBody.innerHTML = typeRows.length
    ? typeRows.map((row) => {
        const rowTotal = Number(row.total || 0);
        const rowFailed = Number(row.failed || 0);
        const failRate = rowTotal ? fmtPercent((rowFailed / rowTotal) * 100) : "-";
        return `
          <tr>
            <td><strong>${escapeHtml(labelType(row.type))}</strong><br><small>${escapeHtml(row.type || "-")}</small></td>
            <td>${fmtCount(rowTotal)}</td>
            <td>${fmtCount(row.passed)}</td>
            <td>${fmtCount(rowFailed)}</td>
            <td>${failRate}</td>
            <td>${fmtAge(row.last_ms)}</td>
          </tr>`;
      }).join("")
    : `<tr><td colspan="6" class="empty">暂无累计测试结果。</td></tr>`;
}

function renderBoards(rows) {
  setText("board-summary", `${rows.length} 块板卡，${rows.filter(({ board }) => board.online).length} 在线`);
  $("boards").innerHTML = rows.length
    ? rows.map(({ board, metric, rates }) => boardCard(board, metric, rates)).join("")
    : `<div class="empty">暂无板卡注册。</div>`;
}

function renderBoardTable(rows) {
  $("board-table").innerHTML = rows.length
    ? rows.map(({ board, metric }) => {
        const temp = maxTemp(metric);
        const health = boardHealth(board, metric);
        const network = metric?.network || board.last_heartbeat?.network || {};
        const uplink = pickIface(metric, network.uplink_interface || "end0");
        return `
          <tr>
            <td><strong>${escapeHtml(board.board_id)}</strong></td>
            <td><span class="mini-status ${health.level}">${escapeHtml(health.text)}</span></td>
            <td>${escapeHtml(board.hostname || "-")}</td>
            <td>${escapeHtml(firstValue(uplink.ipv4, board.wired_ip || "-"))}</td>
            <td>${escapeHtml(board.wifi_ip || "-")}</td>
            <td>${fmtTempValue(temp)}</td>
            <td>${escapeHtml(loadText(metric))}</td>
            <td>${escapeHtml(memText(metric))}</td>
            <td>${fmtDuration(metric?.system?.uptime_sec || 0)}</td>
            <td>${fmtAge(board.last_seen_ms)}</td>
          </tr>`;
      }).join("")
    : `<tr><td colspan="10" class="empty">暂无板卡注册。</td></tr>`;
}

function renderWifiTable(rows) {
  $("wifi-table").innerHTML = rows.length
    ? rows.map(({ board, metric, rates }) => {
        const wifi = metric?.wifi || board.last_heartbeat?.wifi || {};
        const connected = wifi.connected ? "已连接" : "未连接";
        const errors = [
          wifi.tx_retries !== undefined ? `重传累计 ${wifi.tx_retries}` : "",
          wifi.tx_failed !== undefined ? `失败累计 ${wifi.tx_failed}` : "",
          wifi.beacon_loss !== undefined ? `Beacon ${wifi.beacon_loss}` : "",
        ].filter(Boolean).join(" / ") || "-";
        const sampleAge = metric?.timestamp_ms ? `采样 ${fmtAge(metric.timestamp_ms)}` : "等待采样";
        return `
          <tr>
            <td><strong>${escapeHtml(board.board_id)}</strong><br><small>${escapeHtml(wifi.ipv4 || board.wifi_ip || "-")}</small></td>
            <td><span class="mini-status ${wifi.connected ? "good" : "bad"}">${connected}</span></td>
            <td>${escapeHtml(wifi.ssid || "-")}<br><small>${escapeHtml(wifi.bssid || "-")} / ${escapeHtml(wifi.freq_mhz || "-")} MHz</small></td>
            <td>${wifi.signal_dbm ?? "-"} dBm<br><small>质量 ${escapeHtml(wifi.proc_wireless?.link_quality ?? "-")}</small></td>
            <td>${wifi.txpower_dbm ?? "-"} dBm</td>
            <td>TX ${escapeHtml(wifi.tx_bitrate || "-")}<br><small>RX ${escapeHtml(wifi.rx_bitrate || "-")}</small></td>
            <td>RX ${fmtRate(rates.rxBps)}<br><small>TX ${fmtRate(rates.txBps)} / ${escapeHtml(sampleAge)}</small></td>
            <td>${escapeHtml(errors)}<br><small>驱动统计，非测试次数</small></td>
          </tr>`;
      }).join("")
    : `<tr><td colspan="8" class="empty">暂无 WiFi 数据。</td></tr>`;
}

function renderBtTable(rows) {
  $("bt-table").innerHTML = rows.length
    ? rows.map(({ board, metric }) => {
        const bt = metric?.bluetooth || {};
        const status = btStatus(bt);
        const errorText = [
          bt.busy ? "测试命令占用" : "",
          bt.stale ? "沿用上次采样" : "",
          `RX ${bt.rx_errors ?? "-"} / TX ${bt.tx_errors ?? "-"}`,
        ].filter(Boolean).join(" / ");
        return `
          <tr>
            <td><strong>${escapeHtml(board.board_id)}</strong></td>
            <td>${escapeHtml(bt.controller || "-")}</td>
            <td><span class="mini-status ${status.level}">${escapeHtml(status.text)}</span></td>
            <td>${escapeHtml(bt.address || "-")}</td>
            <td>${fmtBytes(bt.rx_bytes)}</td>
            <td>${fmtBytes(bt.tx_bytes)}</td>
            <td>${escapeHtml(errorText)}</td>
          </tr>`;
      }).join("")
    : `<tr><td colspan="7" class="empty">暂无蓝牙数据。</td></tr>`;
}

function renderIncidents(incidents) {
  const visible = incidents.slice(0, 20);
  const open = visible.filter((item) => item.status === "open").length;
  setText("incident-summary", `显示 ${visible.length} 条 / 未恢复 ${open}`);
  const body = $("incidents");
  if (!body) return;
  body.innerHTML = visible.length
    ? visible.map((item) => {
        const summary = item.summary || {};
        const lastMetric = summary.last_metric || {};
        const lastCommand = summary.last_command || {};
        const lastResult = summary.last_result || {};
        const wifi = lastMetric.wifi || {};
        const bt = lastMetric.bluetooth || {};
        const stateText = [
          wifi.connected === undefined ? "" : `WiFi ${wifi.connected ? "已连" : "断开"}`,
          wifi.signal_dbm === undefined || wifi.signal_dbm === null ? "" : `${wifi.signal_dbm} dBm`,
          bt.up === undefined ? "" : `BT ${bt.up ? "UP" : "DOWN"}`,
          lastMetric.max_temp_c === undefined || lastMetric.max_temp_c === null ? "" : `${Number(lastMetric.max_temp_c).toFixed(1)}℃`,
          lastResult.failure_reason ? `前次异常 ${lastResult.failure_reason}` : "",
        ].filter(Boolean).join(" / ") || "-";
        return `
          <tr>
            <td>${fmtTime(item.started_at_ms)}</td>
            <td><strong>${escapeHtml(item.board_id || "-")}</strong></td>
            <td><span class="mini-status ${item.status === "open" ? "bad" : "good"}">${escapeHtml(labelIncidentStatus(item.status))}</span></td>
            <td>${escapeHtml(labelIncidentCategory(item.category))}</td>
            <td>${escapeHtml(item.last_command_id || lastCommand.command_id || "-")}</td>
            <td>${escapeHtml(stateText)}</td>
          </tr>`;
      }).join("")
    : `<tr><td colspan="6" class="empty">暂无离线事故。</td></tr>`;
}

function renderEvents(events) {
  const visible = events.slice(0, 10);
  setText("event-summary", `显示 ${visible.length} 条`);
  const body = $("events");
  if (!body) return;
  body.innerHTML = visible.length
    ? visible.map((item) => `
        <tr>
          <td>${fmtTime(item.timestamp_ms)}</td>
          <td>${escapeHtml(item.board_id || "-")}</td>
          <td><span class="mini-status ${statusLevelForSeverity(item.severity)}">${escapeHtml(labelLevel(item.severity))}</span></td>
          <td>${escapeHtml(labelEventType(item.event_type))}<br><small>${escapeHtml(item.event_type || "-")}</small></td>
          <td>${escapeHtml(item.command_id || "-")}</td>
          <td>${escapeHtml(item.message || summarizeEventData(item.data))}</td>
        </tr>`).join("")
    : `<tr><td colspan="6" class="empty">暂无事件。</td></tr>`;
}

function renderResults(results, stats = {}) {
  const visible = results.slice(0, 50);
  const passed = visible.filter((item) => item.status === "passed").length;
  const failed = visible.filter((item) => isBadStatus(item.status)).length;
  const totalFailed = stats.failed_total ?? failed;
  setText("result-summary", `显示 ${visible.length} 条 / 通过 ${passed} / 当前异常 ${failed} / 累计异常 ${totalFailed}`);
  $("results").innerHTML = visible.length
    ? visible.map((item) => {
        const summary = item.summary || {};
        const loss = fmtLossOrRetransmit(summary);
        const throughput = summary.received_bits_per_second || summary.sent_bits_per_second || summary.bits_per_second;
        const path = summary.wifi_path_ok === undefined ? "" : (summary.wifi_path_ok ? "wlan0" : "非wlan0");
        const peer = item.peer_ip || item.peer_bt_mac || item.peer_board_id || summary.peer_ip || "-";
        const detail = [
          path,
          summary.role ? `角色 ${summary.role}` : "",
          summary.connected === undefined ? "" : (summary.connected ? "已连接" : "未连接"),
          summary.reset_by_peer ? "对端重置" : "",
          summary.connection_aborted ? "连接中止" : "",
          summary.connect_failed ? "连接失败" : "",
          summary.udp_bandwidth ? `目标 ${summary.udp_bandwidth}` : "",
          summary.iperf3_nonfatal_error ? `iperf3 ${summary.iperf3_nonfatal_error}` : "",
          summary.adaptive_selected_bandwidth ? `自适应 ${summary.adaptive_selected_bandwidth}` : "",
          summary.udp_length ? `包长 ${summary.udp_length}B` : "",
          summary.duration_sec ? `${summary.duration_sec}秒` : "",
          summary.delay_ms ? `间隔 ${summary.delay_ms}ms` : "",
          summary.client_delay_sec ? `延迟 ${summary.client_delay_sec}秒` : "",
          summary.duration_ms ? `运行 ${(summary.duration_ms / 1000).toFixed(1)}秒` : "",
          summary.sample_count ? `样本 ${summary.sample_count}` : "",
          summary.activity_seen ? "有链路活动" : "",
          summary.devices_found !== undefined ? `发现 ${fmtCount(summary.devices_found)}` : "",
          summary.controller_up === undefined ? "" : (summary.controller_up ? "控制器UP" : "控制器DOWN"),
          summary.scan_timed_out ? "扫描超时" : "",
          summary.scan_ran_long_enough ? "足时长" : "",
          summary.btmon_captured ? "btmon" : "",
          item.failure_category ? `分类 ${labelFailureCategory(item.failure_category)}` : "",
          item.failed_command ? `命令 ${item.failed_command}` : "",
          item.stderr_excerpt ? `stderr ${item.stderr_excerpt}` : "",
          item.failure_reason ? `原因 ${item.failure_reason}` : "",
          item.error ? String(item.error).slice(0, 80) : "",
        ].filter(Boolean).join(" / ") || "-";
        return `
          <tr>
            <td>${fmtTime(item.timestamp_ms || item.finished_at_ms)}</td>
            <td>${escapeHtml(item.board_id || "-")}</td>
            <td>${escapeHtml(labelType(item.type))}</td>
            <td><span class="mini-status ${statusLevel(item.status)}">${escapeHtml(labelStatus(item.status))}</span></td>
            <td>${escapeHtml(peer)}</td>
            <td>${fmtRate(throughput)}</td>
            <td>${escapeHtml(loss)}</td>
            <td>${escapeHtml(detail)}</td>
          </tr>`;
      }).join("")
    : `<tr><td colspan="8" class="empty">暂无测试结果。</td></tr>`;
}

function renderLogs(logs) {
  const visible = logs.slice(0, 80);
  setText("log-summary", `显示 ${visible.length} 条`);
  $("logs").innerHTML = visible.length
    ? visible.map((item) => `
        <div class="log-row ${escapeHtml(item.level || "")}">
          <span>${fmtTime(item.timestamp_ms)}</span>
          <strong>${escapeHtml(labelLevel(item.level))}</strong>
          <span>${escapeHtml(item.board_id || "-")}: ${escapeHtml(item.message || JSON.stringify(item).slice(0, 160))}</span>
        </div>`).join("")
    : `<div class="empty">暂无日志。</div>`;
}

async function pollTrends() {
  try {
    const response = await fetch(apiUrl("/api/trends?hours=24"), { cache: "no-store" });
    if (!response.ok) throw new Error(`trends ${response.status}`);
    renderTrends(await response.json());
  } catch (error) {
    setText("trend-summary", "趋势数据读取失败");
  }
}

function renderTrends(data) {
  state.lastTrendAt = Date.now();
  const points = data.points || [];
  const results = data.result_points || [];
  const boards = [...new Set(points.map((point) => point.board_id))].sort();
  setText("trend-summary", `最近 ${data.hours || 24} 小时 / ${boards.length} 块板 / ${points.length} 个采样点`);
  drawLineChart("temp-chart", metricSeries(points, boards, "temp_c", (v) => v), { unit: "℃" });
  drawLineChart("signal-chart", metricSeries(points, boards, "signal_dbm", (v) => v), { unit: "dBm", yMin: -95, yMax: -20 });
  drawLineChart("throughput-chart", throughputSeries(points, boards), { unit: "Mbps", minZero: true });
  drawLineChart("memory-chart", metricSeries(points, boards, "mem_used_percent", (v) => v), { unit: "%", yMin: 0, yMax: 100 });
  drawLineChart("result-chart", resultSeries(results), { unit: "Mbps", minZero: true, pointRadius: 3 });
  drawLineChart("bt-error-chart", metricSeries(points, boards, "bt_errors", (v) => v), { unit: "", minZero: true, stepLike: true });
}

function metricSeries(points, boards, field, transform) {
  return boards.map((boardId, index) => ({
    label: boardId,
    color: chartColor(index),
    points: points
      .filter((point) => point.board_id === boardId && point[field] !== null && point[field] !== undefined)
      .map((point) => ({ x: point.timestamp_ms, y: transform(Number(point[field])) })),
  }));
}

function throughputSeries(points, boards) {
  const series = [];
  boards.forEach((boardId, index) => {
    const boardPoints = points.filter((point) => point.board_id === boardId);
    series.push({
      label: `${boardId} RX`,
      color: chartColor(index * 2),
      points: boardPoints
        .filter((point) => point.wifi_rx_bps !== null && point.wifi_rx_bps !== undefined)
        .map((point) => ({ x: point.timestamp_ms, y: Number(point.wifi_rx_bps) / 1000000 })),
    });
    series.push({
      label: `${boardId} TX`,
      color: chartColor(index * 2 + 1),
      points: boardPoints
        .filter((point) => point.wifi_tx_bps !== null && point.wifi_tx_bps !== undefined)
        .map((point) => ({ x: point.timestamp_ms, y: Number(point.wifi_tx_bps) / 1000000 })),
    });
  });
  return series;
}

function resultSeries(results) {
  const boards = [...new Set(results.map((point) => point.board_id))].sort();
  return boards.map((boardId, index) => ({
    label: boardId,
    color: chartColor(index),
    points: results
      .filter((point) => point.board_id === boardId && point.throughput_bps !== null && point.throughput_bps !== undefined)
      .map((point) => ({ x: point.timestamp_ms, y: Number(point.throughput_bps) / 1000000 })),
  }));
}

function drawLineChart(canvasId, series, options = {}) {
  const canvas = $(canvasId);
  if (!canvas) return;
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(320, Math.floor(rect.width || canvas.clientWidth || 320));
  const height = Math.max(180, Math.floor(rect.height || canvas.clientHeight || 220));
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.floor(width * dpr);
  canvas.height = Math.floor(height * dpr);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = "#fffdf7";
  ctx.fillRect(0, 0, width, height);
  const usableSeries = series.filter((item) => item.points.length);
  if (!usableSeries.length) {
    drawEmptyChart(ctx, width, height);
    return;
  }
  const allPoints = usableSeries.flatMap((item) => item.points);
  const xMin = Math.min(...allPoints.map((point) => point.x));
  const xMax = Math.max(...allPoints.map((point) => point.x));
  let yMin = options.yMin ?? Math.min(...allPoints.map((point) => point.y));
  let yMax = options.yMax ?? Math.max(...allPoints.map((point) => point.y));
  if (options.minZero) yMin = Math.min(0, yMin);
  if (yMin === yMax) {
    yMin -= 1;
    yMax += 1;
  }
  const legendRows = Math.ceil(Math.min(usableSeries.length, 6) / 3);
  const pad = { left: 48, right: 16, top: 26 + legendRows * 14, bottom: 30 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  const xScale = (x) => pad.left + ((x - xMin) / Math.max(1, xMax - xMin)) * plotW;
  const yScale = (y) => pad.top + (1 - (y - yMin) / Math.max(1, yMax - yMin)) * plotH;

  drawChartGrid(ctx, width, height, pad, yMin, yMax, xMin, xMax, options.unit || "");
  usableSeries.forEach((item) => {
    ctx.strokeStyle = item.color;
    ctx.lineWidth = 2;
    ctx.beginPath();
    item.points.forEach((point, index) => {
      const x = xScale(point.x);
      const y = yScale(point.y);
      if (index === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
    if (options.pointRadius) {
      ctx.fillStyle = item.color;
      item.points.forEach((point) => {
        ctx.beginPath();
        ctx.arc(xScale(point.x), yScale(point.y), options.pointRadius, 0, Math.PI * 2);
        ctx.fill();
      });
    }
  });
  drawLegend(ctx, usableSeries, pad.left, 18, width - pad.right);
}

function drawChartGrid(ctx, width, height, pad, yMin, yMax, xMin, xMax, unit) {
  ctx.strokeStyle = "rgba(31, 26, 20, 0.18)";
  ctx.fillStyle = "#756d62";
  ctx.lineWidth = 1;
  ctx.font = "12px sans-serif";
  for (let i = 0; i <= 4; i += 1) {
    const y = pad.top + ((height - pad.top - pad.bottom) * i) / 4;
    ctx.beginPath();
    ctx.moveTo(pad.left, y);
    ctx.lineTo(width - pad.right, y);
    ctx.stroke();
    const value = yMax - ((yMax - yMin) * i) / 4;
    ctx.fillText(`${trimNumber(value)}${unit}`, 6, y + 4);
  }
  for (let i = 0; i <= 3; i += 1) {
    const x = pad.left + ((width - pad.left - pad.right) * i) / 3;
    const value = xMin + ((xMax - xMin) * i) / 3;
    ctx.fillText(fmtTime(value), x - 20, height - 24);
  }
}

function drawLegend(ctx, series, x, y, maxX) {
  ctx.font = "12px sans-serif";
  let cursor = x;
  let row = 0;
  series.slice(0, 6).forEach((item, index) => {
    if (index > 0 && index % 3 === 0) {
      row += 1;
      cursor = x;
    }
    const currentY = y + row * 14;
    ctx.fillStyle = item.color;
    ctx.fillRect(cursor, currentY - 8, 14, 4);
    ctx.fillStyle = "#756d62";
    const label = item.label.length > 16 ? `${item.label.slice(0, 16)}...` : item.label;
    ctx.fillText(label, cursor + 18, currentY);
    cursor += Math.min(170, 28 + label.length * 7);
    if (cursor > maxX - 120) {
      row += 1;
      cursor = x;
    }
  });
}

function drawEmptyChart(ctx, width, height) {
  ctx.fillStyle = "#756d62";
  ctx.font = "14px sans-serif";
  ctx.fillText("暂无趋势数据", 18, height / 2);
}

function chartColor(index) {
  const colors = ["#125e8a", "#0c7b4f", "#b42318", "#c46a00", "#4d5f00", "#5c4a72", "#006d77", "#8f2d56"];
  return colors[index % colors.length];
}

function trimNumber(value) {
  const num = Number(value);
  if (Math.abs(num) >= 100) return num.toFixed(0);
  if (Math.abs(num) >= 10) return num.toFixed(1);
  return num.toFixed(2);
}

function renderAdminBoards(boards) {
  const select = $("delete-board-id");
  if (!select || dashboardMode === "readonly") return;
  const current = select.value;
  select.innerHTML = boards.length
    ? boards.map((board) => `<option value="${escapeHtml(board.board_id)}">${escapeHtml(board.board_id)} ${board.online ? "(在线)" : "(离线)"}</option>`).join("")
    : `<option value="">暂无板卡</option>`;
  if (current && boards.some((board) => board.board_id === current)) {
    select.value = current;
  }
}

function initAdminControls() {
  const panel = $("admin-panel");
  if (!panel) return;
  if (dashboardMode === "readonly") {
    panel.hidden = true;
    return;
  }
  $("save-schedule-btn")?.addEventListener("click", saveSchedule);
  $("quick-7d-btn")?.addEventListener("click", quickSevenDays);
  $("clear-schedule-btn")?.addEventListener("click", clearSchedule);
  $("clear-history-btn")?.addEventListener("click", clearHistory);
  $("delete-board-btn")?.addEventListener("click", deleteBoard);
}

function setAdminStatus(message, level = "") {
  const node = $("admin-status");
  if (!node) return;
  node.textContent = message;
  node.className = level;
}

async function adminRequest(path, payload) {
  const response = await fetch(path, {
    method: "POST",
    headers: {
      "content-type": "application/json",
    },
    body: JSON.stringify(payload || {}),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || `HTTP ${response.status}`);
  }
  return data;
}

async function saveSchedule() {
  const startMs = parseDateTimeInput($("schedule-start-input")?.value || "");
  const endMs = parseDateTimeInput($("schedule-end-input")?.value || "");
  if (!startMs || !endMs) {
    setAdminStatus("请填写开始时间和截止时间", "bad");
    return;
  }
  if (endMs <= startMs) {
    setAdminStatus("截止时间必须晚于开始时间", "bad");
    return;
  }
  setAdminStatus("正在保存老化时间...");
  try {
    await adminRequest("/api/v1/admin/schedule", { start_ms: startMs, end_ms: endMs });
    setAdminStatus("老化时间已保存", "good");
    await pollSnapshot();
  } catch (error) {
    setAdminStatus(`保存失败：${error.message}`, "bad");
  }
}

async function quickSevenDays() {
  const startMs = Date.now();
  const endMs = startMs + 7 * 24 * 3600 * 1000;
  const startInput = $("schedule-start-input");
  const endInput = $("schedule-end-input");
  if (startInput) startInput.value = fmtDateTimeInput(startMs);
  if (endInput) endInput.value = fmtDateTimeInput(endMs);
  setAdminStatus("已填入从现在开始 7 天，正在保存...");
  try {
    await adminRequest("/api/v1/admin/schedule", { start_ms: startMs, end_ms: endMs });
    setAdminStatus("已设置为从现在开始 7 天", "good");
    await pollSnapshot();
  } catch (error) {
    setAdminStatus(`保存失败：${error.message}`, "bad");
  }
}

async function clearSchedule() {
  if (!confirm("确认清除老化时间限制？清除后云端会持续下发测试命令。")) return;
  setAdminStatus("正在清除时间限制...");
  try {
    await adminRequest("/api/v1/admin/schedule/clear", {});
    setAdminStatus("已清除时间限制", "good");
    await pollSnapshot();
  } catch (error) {
    setAdminStatus(`清除失败：${error.message}`, "bad");
  }
}

async function clearHistory() {
  if (!confirm("确认清除所有历史 metrics、日志、测试结果和取证文件？板卡注册记录会保留。")) return;
  setAdminStatus("正在清除历史数据...");
  try {
    const data = await adminRequest("/api/v1/admin/history/clear", {});
    setAdminStatus(`已清除：${formatDeleted(data.deleted)}`, "good");
    await pollSnapshot();
  } catch (error) {
    setAdminStatus(`清除失败：${error.message}`, "bad");
  }
}

async function deleteBoard() {
  const boardId = $("delete-board-id")?.value || "";
  if (!boardId) {
    setAdminStatus("没有可删除的板卡", "bad");
    return;
  }
  const deleteHistory = Boolean($("delete-board-history")?.checked);
  const suffix = deleteHistory ? "并删除该板历史数据" : "仅删除注册记录";
  if (!confirm(`确认删除板卡 ${boardId}，${suffix}？在线板卡会在下一次心跳后重新注册。`)) return;
  setAdminStatus(`正在删除 ${boardId}...`);
  try {
    const data = await adminRequest("/api/v1/admin/boards/delete", { board_id: boardId, delete_history: deleteHistory });
    setAdminStatus(`已删除 ${boardId}：${formatDeleted(data.deleted?.history || {})}`, "good");
    await pollSnapshot();
  } catch (error) {
    setAdminStatus(`删除失败：${error.message}`, "bad");
  }
}

function formatDeleted(deleted) {
  if (!deleted) return "-";
  const parts = [];
  for (const key of ["metrics", "logs", "results", "artifacts", "artifact_files"]) {
    if (deleted[key] !== undefined) parts.push(`${key} ${deleted[key]}`);
  }
  return parts.join(" / ") || "-";
}

function isBadStatus(status) {
  return ["failed", "agent_error", "error"].includes(status);
}

function statusLevel(status) {
  if (status === "passed") return "good";
  if (status === "unsupported") return "warn";
  if (isBadStatus(status)) return "bad";
  return "warn";
}

function statusLevelForSeverity(severity) {
  if (severity === "error" || severity === "bad") return "bad";
  if (severity === "warn" || severity === "warning") return "warn";
  return "good";
}

function labelType(type) {
  const labels = {
    wifi_iperf3_tcp: "WiFi TCP 满速",
    wifi_iperf3_udp: "WiFi UDP 压测",
    wifi_tcp_single: "WiFi TCP 单流",
    wifi_tcp_multi: "WiFi TCP 多流",
    wifi_tcp_reverse: "WiFi TCP 反向",
    wifi_tcp_bidir: "WiFi TCP 双向",
    wifi_udp_flood: "WiFi UDP 满载",
    wifi_udp_small: "WiFi UDP 小包",
    wifi_udp_large: "WiFi UDP 大包",
    wifi_ping: "WiFi Ping",
    bt_ble_probe: "蓝牙 BLE 探测",
    bt_ble_advertise: "蓝牙 BLE 广播",
    bt_ble_scan: "蓝牙 BLE 扫描",
    bt_bredr_inquiry: "蓝牙经典发现",
    bt_l2ping: "蓝牙 L2CAP Ping",
    bt_l2test: "蓝牙 L2CAP 数据",
  };
  return labels[type] || type || "-";
}

function labelEventType(type) {
  const labels = {
    heartbeat: "心跳",
    metric_sample: "指标采样",
    command_started: "命令开始",
    command_progress: "命令运行中",
    command_finished: "命令结束",
    command_deferred: "命令延后",
    agent_log: "Agent 日志",
    log_snapshot: "日志快照",
    agent_registered: "Agent 注册",
    board_offline_incident: "离线事故",
  };
  return labels[type] || type || "-";
}

function labelIncidentStatus(status) {
  const labels = { open: "未恢复", closed: "已恢复" };
  return labels[status] || status || "-";
}

function labelIncidentCategory(category) {
  const labels = {
    lost_during_command: "命令中断",
    board_offline: "板卡离线",
  };
  return labels[category] || category || "-";
}

function labelFailureCategory(category) {
  const labels = {
    board_offline: "板卡离线",
    iperf3_busy: "iperf3占用",
    iperf3_control: "iperf3控制链路",
    wifi_loss: "WiFi全丢包",
    udp_loss: "UDP丢包",
    bt_mgmt_timeout: "BT管理超时",
    bt_link_down: "BT链路断开",
    agent_error: "Agent异常",
    test_failed: "测试失败",
  };
  return labels[category] || category || "-";
}

function labelStatus(status) {
  const labels = {
    passed: "通过",
    failed: "失败",
    unsupported: "不支持",
    agent_error: "Agent 异常",
  };
  return labels[status] || status || "-";
}

function labelLevel(level) {
  const labels = {
    info: "信息",
    warn: "警告",
    warning: "警告",
    error: "错误",
    debug: "调试",
  };
  return labels[level] || level || "-";
}

function summarizeEventData(data) {
  if (!data || typeof data !== "object") return "-";
  if (data.resource) return `资源 ${data.resource}`;
  if (data.runtime_sec) return `运行 ${data.runtime_sec} 秒`;
  if (data.status) return `状态 ${data.status}`;
  return JSON.stringify(data).slice(0, 160);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function pollSnapshot() {
  try {
    const response = await fetch(apiUrl("/api/snapshot"), { cache: "no-store" });
    if (!response.ok) throw new Error(`snapshot ${response.status}`);
    render(await response.json());
    setText("connection-state", "轮询中");
  } catch (error) {
    setText("connection-state", "连接中断");
  }
}

function connectWebSocket() {
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${protocol}://${location.host}${dashboardBase}/ws/dashboard`);
  state.socket = socket;
  socket.onopen = () => {
    setText("connection-state", "实时连接");
  };
  socket.onmessage = (event) => {
    render(JSON.parse(event.data));
    setText("connection-state", "实时连接");
  };
  socket.onclose = () => {
    setText("connection-state", "重新连接中");
    setTimeout(connectWebSocket, 2000);
  };
  socket.onerror = () => {
    socket.close();
  };
}

initAdminControls();
connectWebSocket();
setInterval(() => {
  if (Date.now() - state.lastSnapshotAt > 3000) pollSnapshot();
}, 3000);
pollSnapshot();
