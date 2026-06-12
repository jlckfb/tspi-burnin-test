# 泰山派 3M 老化测试

用于泰山派 3M RK3576 开发板的 WiFi / Bluetooth 老化测试系统，重点覆盖 AP6256 WiFi 与 BT 长时间压测、板卡状态采集、测试结果汇总和云端 Web 看板展示。

本项目是独立测试工具，不需要合入 Linux SDK。SDK 负责系统、内核和设备树构建；本项目负责云端看板和板端压测 Agent。

## 功能

- WiFi 老化测试：TCP 单流、多流、反向、双向，UDP 满载、小包、大包，ping RTT / 丢包 / 抖动。
- UDP 自适应速率：先低速探测，再选择满足丢包阈值的速率，避免固定 UDP 速率把链路直接打爆。
- TCP 重传统计：展示 TCP 重传次数和估算段重传率，不把 TCP 重传误写成 UDP 式丢包率。
- 蓝牙老化测试：BLE 广播 / 扫描、BR/EDR inquiry、L2CAP ping、L2CAP 数据收发。
- 板卡实时状态：在线状态、温度、WiFi 信号、吞吐、BT 控制器状态、内存、负载、运行时长。
- 数据总览：累计测试数、通过数、异常数、WiFi/BT 分类、TCP 重传率、UDP 丢包率、指标采样和取证文件数。
- Web 看板：主看板可设置老化测试时间、清除历史、删除板卡；只读看板只能查看。
- 板端缓存：有线链路断开或云端不可达时，板端会本地缓存上报数据，恢复后再上传。

## 架构

```text
┌──────────────┐     wired end0      ┌──────────────────────┐
│ TaishanPi #1 │ ─────────────────▶ │ Cloud FastAPI Server │
│ wlan0 / hci0 │ ◀───────────────── │ Web dashboard + API  │
└──────┬───────┘                     └──────────┬───────────┘
       │                 WiFi LAN                │
       │ iperf3 / ping / BT peer tests           │ HTTP 8080
       ▼                                         ▼
┌──────────────┐                         Browser dashboard
│ TaishanPi #2 │
│ wlan0 / hci0 │
└──────────────┘
```

- 云端：FastAPI Web/API，默认端口 `8080`。
- 主看板：`/tspi-burnin/`。
- 只读看板：`/tspi-burnin-view/`。
- 板端：`tspi-burnin-agent.service` 负责采集和执行命令。
- WiFi 服务端：`tspi-burnin-iperf3.service` 在 `wlan0` 上常驻 iperf3 server。
- 有线 `end0`：推荐作为稳定上报链路。
- WiFi `wlan0`：作为被测链路，WiFi 压测强制绑定到该接口。

## 目录

```text
agent/                     板端 Python Agent
cloud/                     云端 FastAPI 服务、Dockerfile、Compose、前端静态文件
config/tspi-burnin.example.env  开源配置模板
config/tspi-burnin.env          本地私有配置，已被 .gitignore 忽略
scripts/                   打包和配置公共脚本
systemd/                   板端 systemd service
build.sh                   一键构建板端 deb
dist/                      deb 输出目录，已被 .gitignore 忽略
```

## 准备条件

云端服务器：

- Linux 服务器，推荐 Debian / Ubuntu。
- Docker 与 Docker Compose plugin。
- 公网或局域网可访问的 IP。
- 放通 Web/API 端口，默认 `8080/tcp`。

板端：

- 泰山派 3M RK3576 或兼容 Linux 板卡。
- 推荐 Ubuntu / Debian 系 rootfs。
- `wlan0` 可连接测试路由器。
- `end0` 可访问云端服务器。
- 需要可用的 `sudo` 和 apt 源。

板端 deb 依赖：

```text
python3, iperf3, iw, bluez, network-manager, iproute2, iputils-ping
```

## 快速开始

### 1. 准备本地私有配置

开源仓库只提交模板文件，不提交真实服务器地址和 token。

```bash
cp config/tspi-burnin.example.env config/tspi-burnin.env
nano config/tspi-burnin.env
```

至少修改：

```env
BURNIN_SERVER_URL=http://YOUR_SERVER_IP:8080
BURNIN_API_TOKEN=change-me-to-a-random-token
```

`config/tspi-burnin.env` 已在 `.gitignore` 中，不能提交到公开仓库。

### 2. 启动云端

```bash
cd cloud
docker compose --env-file ../config/tspi-burnin.env up -d --build
```

检查健康状态：

```bash
curl http://127.0.0.1:8080/api/v1/health
```

看板地址：

```text
主看板:   http://<server-ip>:8080/tspi-burnin/
只读看板: http://<server-ip>:8080/tspi-burnin-view/
```

### 3. 打包板端 deb

回到项目根目录：

```bash
./build.sh
```

产物：

```text
dist/tspi-burnin-agent_<version>_all.deb
dist/SHA256SUMS
```

打包时会读取 `config/tspi-burnin.env`，并把板端配置写入 deb 内的 `/etc/tspi-burnin/config.toml`。

### 4. 板端连接测试 WiFi

每块板先连接同一个测试路由器。例如：

```bash
sudo nmcli dev wifi connect "YOUR_WIFI_SSID" password "YOUR_WIFI_PASSWORD" ifname wlan0
```

确认 WiFi：

```bash
iw dev wlan0 link
ip -4 addr show wlan0
```

确认有线上报链路：

```bash
ip -4 addr show end0
ping -c 3 <server-ip>
```

### 5. 安装板端 deb

把 deb 复制到每块板，然后安装：

```bash
sudo apt install -y ./tspi-burnin-agent_<version>_all.deb
```

安装后会自动启动：

```text
tspi-burnin-agent.service
tspi-burnin-iperf3.service
```

检查状态：

```bash
systemctl status tspi-burnin-agent.service
systemctl status tspi-burnin-iperf3.service
journalctl -u tspi-burnin-agent.service -f
```

## 配置说明

所有运行配置统一在 `config/tspi-burnin.env`。开源时只提交 `config/tspi-burnin.example.env`。

### 基础配置

| 变量 | 说明 |
|------|------|
| `BURNIN_SERVER_URL` | 板端访问云端的 URL，例如 `http://1.2.3.4:8080` |
| `BURNIN_HTTP_PORT` | 云端容器映射到宿主机的端口 |
| `BURNIN_API_TOKEN` | Agent 到云端 API 的共享 token |
| `BURNIN_DASHBOARD_PATH` | 主看板路径，默认 `/tspi-burnin` |
| `BURNIN_PUBLIC_DASHBOARD_PATH` | 只读看板路径，默认 `/tspi-burnin-view` |
| `BURNIN_ONLINE_TIMEOUT_SEC` | 超过该秒数未上报则认为离线 |
| `BURNIN_METRICS_RETAIN_HOURS` | metrics 自动保留小时数，建议不少于完整老化周期 |
| `BURNIN_EVENTS_RETAIN_HOURS` | 事件时间线自动保留小时数，建议不少于完整老化周期 |

### WiFi 配置

| 变量 | 说明 |
|------|------|
| `BURNIN_WIFI_EPOCH_SEC` | WiFi 测试轮转周期 |
| `BURNIN_WIFI_TCP_SEC` | TCP 单轮测试时长 |
| `BURNIN_WIFI_TCP_PARALLEL` | TCP 多流并发数 |
| `BURNIN_IPERF3_PORT` | iperf3 服务端口 |
| `BURNIN_WIFI_UDP_FLOOD_BANDWIDTH` | UDP 满载目标上限 |
| `BURNIN_WIFI_UDP_SMALL_BANDWIDTH` | UDP 小包目标上限 |
| `BURNIN_WIFI_UDP_LARGE_BANDWIDTH` | UDP 大包目标上限 |
| `BURNIN_WIFI_UDP_FLOOD_RATES` | UDP 满载自适应探测速率列表 |
| `BURNIN_WIFI_UDP_SMALL_RATES` | UDP 小包自适应探测速率列表 |
| `BURNIN_WIFI_UDP_LARGE_RATES` | UDP 大包自适应探测速率列表 |
| `BURNIN_WIFI_UDP_ADAPTIVE_PROBE_SEC` | 每个 UDP 速率探测时长 |
| `BURNIN_WIFI_UDP_ADAPTIVE_MAX_LOSS_PERCENT` | UDP 自适应允许的最大丢包率 |
| `BURNIN_WIFI_UDP_MIN_SEC` | UDP 最短正式测试时长 |

### 蓝牙配置

| 变量 | 说明 |
|------|------|
| `BURNIN_BT_PERIOD_SEC` | 蓝牙测试轮转周期 |
| `BURNIN_BT_DURATION_SEC` | BLE / BR-EDR 基础测试时长 |
| `BURNIN_BT_L2TEST_DURATION_SEC` | L2CAP 数据测试时长 |
| `BURNIN_BT_L2TEST_FRAMES` | L2CAP 数据测试帧数上限 |
| `BURNIN_BT_L2TEST_BYTES` | L2CAP 单帧字节数 |
| `BURNIN_BT_L2TEST_DELAY_MS` | L2CAP 发包间隔，过小可能触发流控或断连 |
| `BURNIN_BT_L2TEST_PSM` | L2CAP PSM，空值使用工具默认 |

### 板端 Agent 配置

| 变量 | 说明 |
|------|------|
| `BURNIN_AGENT_BOARD_ID` | 固定板卡 ID，留空则从接口 MAC 自动生成 |
| `BURNIN_AGENT_UPLINK_INTERFACE` | 上报链路接口，默认 `end0` |
| `BURNIN_AGENT_REQUIRE_UPLINK_INTERFACE` | 是否要求上报链路必须可用 |
| `BURNIN_AGENT_WIFI_INTERFACE` | 被测 WiFi 接口，默认 `wlan0` |
| `BURNIN_AGENT_BT_CONTROLLER` | 蓝牙控制器，默认 `hci0` |
| `BURNIN_AGENT_HEARTBEAT_INTERVAL_SEC` | 心跳间隔 |
| `BURNIN_AGENT_METRICS_INTERVAL_SEC` | 指标采集间隔 |
| `BURNIN_AGENT_LOG_FLUSH_INTERVAL_SEC` | 日志上报间隔 |
| `BURNIN_AGENT_COMMAND_POLL_INTERVAL_SEC` | 命令轮询间隔 |
| `BURNIN_AGENT_COMMAND_WORKERS` | 并发命令 worker 数 |
| `BURNIN_AGENT_DATA_DIR` | 板端状态和缓存目录 |
| `BURNIN_AGENT_MAX_SPOOL_FILES` | 离线上报缓存最大文件数 |
| `BURNIN_AGENT_REQUEST_TIMEOUT_SEC` | HTTP 请求超时 |
| `BURNIN_AGENT_ARTIFACT_MAX_BYTES` | 单个取证附件最大字节数 |
| `BURNIN_AGENT_BTMON_CAPTURE_SEC` | 蓝牙失败时 btmon 抓取时长 |

## Web 看板

主看板：

- 查看在线板卡、温度、WiFi、BT、系统负载和内存。
- 设置老化测试开始时间和截止时间。
- 一键设置“现在起 7 天”。
- 清除历史数据。
- 删除注册板卡。
- 查看累计数据总览、最近测试结果和最近日志。

只读看板：

- URL 默认 `/tspi-burnin-view/`。
- 不显示管理操作。
- 适合发给其他人查看测试状态。

注意：Web 看板默认没有登录认证。Agent API 通过 `BURNIN_API_TOKEN` 保护，但看板页面是开放的。公网部署时建议至少使用云厂商安全组限制来源 IP，或自行加反向代理访问控制。

## 测试行为

### WiFi

云端按 `BURNIN_WIFI_EPOCH_SEC` 轮转下发：

- `wifi_tcp_single`：TCP 单流。
- `wifi_tcp_multi`：TCP 多流。
- `wifi_tcp_reverse`：TCP 反向。
- `wifi_tcp_bidir`：TCP 双向。
- `wifi_udp_flood`：UDP 满载。
- `wifi_udp_small`：UDP 小包。
- `wifi_udp_large`：UDP 大包。
- `wifi_ping`：ping RTT / 丢包 / 抖动。

板端执行前会检查：

- `wlan0` 有 IPv4。
- WiFi 已连接。
- 到对端 WiFi IP 的路由走 `wlan0`。
- iperf3 客户端绑定到 `wlan0`。

结果里会带 `wifi_path_ok`、`route_dev`、`bound_dev` 等字段，用于确认流量确实走 WiFi。

### UDP

UDP 支持自适应速率。流程是：

1. 按配置的速率列表做短时间探测。
2. 选择丢包率不超过 `BURNIN_WIFI_UDP_ADAPTIVE_MAX_LOSS_PERCENT` 的最高速率。
3. 如果全部超过阈值，选择丢包最低的速率。
4. 用选择后的速率跑正式测试。

UDP 丢包率是 iperf3 的 `lost_percent`。

### TCP

TCP 没有 UDP 那种 `lost_percent`。看板展示的是：

- TCP 重传次数。
- 估算 TCP 段重传率。
- 每 GB 发送数据对应的重传次数。

估算段重传率使用常见 MTU 1500 下约 `1448B` TCP payload 换算，只用于观察趋势和严重程度。

### Bluetooth

云端按 `BURNIN_BT_PERIOD_SEC` 轮转下发：

- BLE 广播 / 扫描。
- BR/EDR inquiry。
- L2CAP ping。
- L2CAP 数据收发。

L2CAP 数据测试会在板卡之间切换角色。`BURNIN_BT_L2TEST_DELAY_MS` 用于控制发包间隔；如果出现流控、连接重置或失败，可以适当增大该值。

## 常用操作

云端：

```bash
docker compose --env-file config/tspi-burnin.env -f cloud/docker-compose.yml ps
docker compose --env-file config/tspi-burnin.env -f cloud/docker-compose.yml logs -f
curl http://127.0.0.1:8080/api/v1/health
```

Agent/脚本诊断入口：

```bash
curl http://127.0.0.1:8080/api/v1/diagnostics
curl 'http://127.0.0.1:8080/api/v1/diagnostics/failures?limit=100'
curl 'http://127.0.0.1:8080/api/v1/diagnostics/events?event_type=log_snapshot&limit=5'
curl 'http://127.0.0.1:8080/api/v1/diagnostics/boards/<board_id>?limit=300'
curl 'http://127.0.0.1:8080/api/v1/diagnostics/commands/<command_id>'
```

板端：

```bash
systemctl status tspi-burnin-agent.service
systemctl status tspi-burnin-iperf3.service
journalctl -u tspi-burnin-agent.service -f
journalctl -u tspi-burnin-iperf3.service -f
iw dev wlan0 link
ip -4 addr show end0
ip -4 addr show wlan0
hciconfig hci0 -a
pgrep -a iperf3
```

重新安装板端包：

```bash
sudo apt install -y ./tspi-burnin-agent_<version>_all.deb
sudo systemctl restart tspi-burnin-agent.service tspi-burnin-iperf3.service
```

查看板端配置：

```bash
sudo sed -n '1,120p' /etc/tspi-burnin/config.toml
```

## 常见问题

### 看板打不开

检查：

- 云服务器安全组是否放通 `BURNIN_HTTP_PORT`。
- 容器是否启动。
- 健康接口是否正常。

```bash
docker compose --env-file config/tspi-burnin.env -f cloud/docker-compose.yml ps
curl http://127.0.0.1:8080/api/v1/health
```

### 板卡不在线

检查：

- deb 是否安装成功。
- `tspi-burnin-agent.service` 是否运行。
- `end0` 是否能访问 `BURNIN_SERVER_URL`。
- 板端 `/etc/tspi-burnin/config.toml` 的 token 和云端 `BURNIN_API_TOKEN` 是否一致。

### WiFi 结果显示非 wlan0

说明流量可能没有走被测 WiFi。检查：

```bash
ip route get <peer-wifi-ip>
iw dev wlan0 link
```

确保板卡之间的 WiFi IP 在同一个路由器局域网内。

### UDP 经常失败或丢包高

建议：

- 降低 `BURNIN_WIFI_UDP_*_RATES`。
- 降低 `BURNIN_WIFI_UDP_*_BANDWIDTH`。
- 提高 `BURNIN_WIFI_UDP_ADAPTIVE_PROBE_SEC`，让探测更稳定。
- 检查路由器是否限制 UDP 广播/高 PPS 流量。

### TCP 重传偏高

TCP 多流满压下重传增加是正常现象，但如果异常高，应检查：

- WiFi 信号强度。
- 路由器负载。
- 板卡距离和天线状态。
- 是否同时跑了过多 UDP 测试。

### BT L2CAP 数据失败

建议：

- 增大 `BURNIN_BT_L2TEST_DELAY_MS`。
- 降低 `BURNIN_BT_L2TEST_BYTES`。
- 确认两块板的 BT MAC 已被云端正确识别。
- 查看板端 `journalctl -u tspi-burnin-agent.service -f`。

## 开源前检查

发布前建议执行：

```bash
git status --ignored --short
rg -n "<your-real-server-ip>|<your-real-token>|<your-real-password>" . \
  --glob '!config/tspi-burnin.env' \
  --glob '!dist/**' \
  --glob '!build/**'
```

确认不要提交：

- `config/tspi-burnin.env`
- `dist/`
- `build/`
- 真实服务器 IP
- 真实 token
- 板卡登录密码
- 云服务器密码

建议提交：

- `config/tspi-burnin.example.env`
- `agent/config.example.toml`
- 源码、Dockerfile、Compose、systemd service、README

## 许可

请在开源前补充项目许可证文件，例如 `LICENSE`。如果不确定，常用选择是 MIT、Apache-2.0 或 GPL-3.0。
