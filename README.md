# 熬鹰资本 Binance 合约带单监控

一个只读的服务端监听程序：定期读取 Binance Futures Copy Trading 网页公开订单记录，发现新订单后通过 SMTP 邮件提醒。

- 带单员：熬鹰资本
- Portfolio ID：`5075281354358777856`
- 默认每 5 秒轮询
- 每轮先获取最新 10 条
- 只有最新页全部是新记录时，才通过网页 `indexValue` 游标补充最多 3 页
- 首次启动建立基线，不把历史订单误报为新交易
- SQLite 持久化去重和邮件发送状态
- 提供 `/healthz`、`/api/status`、`/api/events`

## 1. 运行环境

推荐 Python 3.11 或更高版本。

```bash
python -m venv .venv
```

Windows：

```powershell
.venv\Scripts\Activate.ps1
```

Linux：

```bash
source .venv/bin/activate
```

安装依赖：

```bash
python -m pip install -r requirements.txt
```

## 2. 配置

复制配置模板：

Windows：

```powershell
Copy-Item .env.example .env
```

Linux：

```bash
cp .env.example .env
```

编辑 `.env`，至少填写邮件参数：

```dotenv
SMTP_HOST=smtp.qq.com
SMTP_PORT=465
SMTP_SECURITY=ssl
SMTP_USERNAME=你的发件邮箱
SMTP_PASSWORD=邮箱 SMTP 授权码
MAIL_FROM=你的发件邮箱
MAIL_TO=接收通知的邮箱
```

不要把 `.env` 提交到 GitHub，也不要把邮箱授权码发到聊天或公开仓库。

常用 SMTP：

| 邮箱 | SMTP_HOST | SMTP_PORT | SMTP_SECURITY |
|---|---|---:|---|
| QQ 邮箱 | smtp.qq.com | 465 | ssl |
| Gmail | smtp.gmail.com | 465 | ssl |
| 163 邮箱 | smtp.163.com | 465 | ssl |

多数邮箱需要先在邮箱设置里启用 SMTP，并使用 SMTP 授权码/应用密码，不是网页登录密码。

其余默认值：

```dotenv
POLL_INTERVAL_SECONDS=5
PAGE_SIZE=10
LOOKBACK_DAYS=30
MAX_CATCHUP_PAGES=3
DATABASE_PATH=./data/monitor.db
```

`POLL_INTERVAL_SECONDS` 最低限制为 3 秒。建议先使用 5 秒，观察币安接口稳定性后再决定是否调整。

## 3. 测试邮件

```bash
python test_email.py
```

确认收到测试邮件后再启动监听。

## 4. 启动服务

```bash
python run.py
```

首次启动时，程序读取最新一页订单建立基线，不发送历史订单邮件。

后续发现新的订单记录时：
1. 先写入 SQLite；
2. 将新记录合并成一封邮件；
3. 邮件发送成功后标记为已通知；
4. 邮件发送失败时保留待发送状态，稍后重试。

## 5. 状态接口

默认只绑定本机 `127.0.0.1:8765`。

- `http://127.0.0.1:8765/`：网页状态与最近订单
- `http://127.0.0.1:8765/healthz`
- `http://127.0.0.1:8765/api/status`
- `http://127.0.0.1:8765/api/events?limit=20`
- `http://127.0.0.1:8765/docs`

如果配置 `STATUS_API_TOKEN`，访问 `/api/status` 和 `/api/events` 时需要：

```http
Authorization: Bearer <STATUS_API_TOKEN>
```

如果需要从本地电脑查看 VPS 状态，推荐 SSH 隧道：

```bash
ssh -L 8765:127.0.0.1:8765 用户名@服务器IP
```

然后在本地打开 `http://127.0.0.1:8765/api/status`。如果需要从公网访问状态接口，不要直接无认证暴露；应配置 `STATUS_API_TOKEN`，并使用 HTTPS 和访问控制的反向代理。

网页访客可填写邮箱订阅或退订通知。服务器需配置 `PUBLIC_BASE_URL` 为网页的完整公开地址，用于生成确认和退订链接。两种操作都须通过邮件确认；订阅后只接收新订单。每个地址每小时最多申请一次同类邮件，服务器每小时最多发送 20 封同类申请邮件。

## 6. Linux systemd 部署

假设项目放在 `/opt/binance-trader-monitor`：

```bash
cd /opt/binance-trader-monitor
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env
python test_email.py
```

复制服务文件：

```bash
sudo cp deploy/binance-trader-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now binance-trader-monitor
```

查看状态：

```bash
sudo systemctl status binance-trader-monitor
sudo journalctl -u binance-trader-monitor -f
```

## 7. Windows 后台运行

先在 PowerShell 中激活虚拟环境并运行：

```powershell
python run.py
```

稳定后可以使用 NSSM 或 Windows 任务计划程序配置开机启动。不要同时启动多个实例，否则虽然 SQLite 会去重，但会增加币安接口请求量。

## 8. 监控范围与限制

1. 当前公开订单响应没有独立的 `orderId` / `tradeId`，程序使用完整订单 JSON 的 SHA-256 指纹去重。
2. `side` + `positionSide` 可以判断方向，但仅凭公开订单行无法准确区分首次开仓、加仓、部分平仓、全部平仓。
3. 带单员当前仓位展示为关闭时，当前持仓接口返回空数组；本程序以公开订单历史为主要数据源。
4. 这是从币安网页 HAR 确认的网页 BAPI，不是币安承诺长期稳定的公开开发者 API。币安可能调整接口、字段、风控或访问频率。
5. 轮询间隔不代表交易发生到通知送达的保证延迟。实际延迟还取决于网页数据更新速度、接口响应、SMTP 投递。
6. 如果一轮内出现超过最新页数量的连续新订单，程序会使用网页的 `indexValue` 游标向前补页，但最多补 `MAX_CATCHUP_PAGES` 页；达到上限会在日志中体现。
7. 邮件发送成功指 SMTP 服务器接受邮件，不等于收件箱一定即时展示；还受邮件服务商投递影响。

## 9. 测试

```bash
python -m unittest discover -s tests -v
```


## 10. Phase 1：个人 USDⓈ-M 虚拟镜像模拟

Phase 1 只构建虚拟账本和模拟意图，**不连接真实 Futures 账户、不读取 API Key/Secret、不提交真实订单**。

### 配置

在 `.env` 中调整：

```dotenv
MIRROR_ENABLED=true
MIRROR_FIXED_MARGIN_USDT=10
MIRROR_LEVERAGE=3
MIRROR_MAX_ORDER_MARGIN_USDT=10
MIRROR_MAX_TOTAL_MARGIN_USDT=50
MIRROR_MAX_SYMBOL_MARGIN_USDT=20
MIRROR_MAX_PRICE_DEVIATION_BPS=50
MIRROR_MAX_SIGNAL_AGE_SECONDS=120
MIRROR_MAX_DAILY_LOSS_USDT=10
MIRROR_ALLOWED_SYMBOLS=
MIRROR_MARKET_DATA_BASE_URL=https://fapi.binance.com
```

以上均为开发/模拟默认值，不是实盘建议。固定保证金按每条被识别为开仓/加仓的源订单计算；目标数量使用当前标记价格和 USDⓈ-M `exchangeInfo` 过滤规则计算。

### 源仓位同步前置条件

熬鹰公开页面隐藏了当前仓位，当前持仓接口返回空数组。因此，程序默认将源仓位标记为 `UNSYNCED`：

- `UNSYNCED` 时只生成被阻止的模拟预览，不更新镜像仓位。
- 只有获得可信的完整源仓位基准后，才能进行减仓比例模拟。
- 不要仅因为公开持仓接口返回空数组，就认定熬鹰当前仓位为空。
- 如果你能独立确认熬鹰所有仓位确实为空，可以显式初始化为平仓基准：

```bash
python -m mirror.cli --database ./data/monitor.db assume-source-flat --confirm-source-is-flat
```

如果源账户实际仍有仓位，应导入一份完整快照，而不是假设为空。JSON 示例：

```json
[
  {"symbol":"BTCUSDT","side":"LONG","quantity":"0.015","avg_entry_price":"65000"},
  {"symbol":"ETHUSDT","side":"SHORT","quantity":"0.25","avg_entry_price":"3200"}
]
```

导入命令：

```bash
python -m mirror.cli --database ./data/monitor.db seed-source-positions --file source_positions.json --confirm-complete-snapshot
```

这两个命令只修改本地模拟账本，不会访问或修改币安账户。导入快照必须覆盖源账户全部未平仓 LONG/SHORT 仓位；遗漏仓位会导致后续减仓比例错误。

### 模拟 API

默认状态 API 仍绑定本机 `127.0.0.1:8765`。镜像接口也受 `STATUS_API_TOKEN` 保护（如果配置了该 token）：

- `GET /api/mirror/status`：模拟模式、源同步状态、风险参数、计数。
- `GET /api/mirror/positions`：源虚拟仓位和目标模拟仓位。
- `GET /api/mirror/events?limit=50`：源订单事件处理结果。
- `GET /api/mirror/intents?limit=50`：模拟开仓/减仓意图及阻止原因。

### Phase 1 的明确限制

1. 币安公开订单行没有独立 `orderId` / `tradeId`。当前使用 `symbol + side + positionSide + type + orderTime` 构造合成订单键；同键出现不同快照时标记身份歧义并暂停同步，不能声称逐笔成交级别完全可靠。
2. 目前只处理 `positionSide=LONG/SHORT`。`BOTH`、缺少稳定 `orderTime` 的记录会被阻止。
3. 仅凭订单历史无法证明某一条记录是首次开仓还是加仓，因此事件使用 `OPEN_OR_ADD`、`REDUCE_OR_CLOSE` 这样的保守分类。
4. 源仓位基准未同步、源仓位数量下溢、订单身份歧义或镜像账本异常时，系统会 fail-closed，不生成可执行的模拟仓位变更。
5. 开仓按固定保证金和配置杠杆计算数量；减仓按源仓位减仓比例计算目标模拟仓位的减仓量。
6. 标记价格和数量规则来自 USDⓈ-M 公共只读接口；接口不可用时对应模拟意图会被阻止。
7. 本阶段没有真实下单代码。Phase 2 才会单独设计 API Key 管理、用户数据流和 Demo/Testnet 验证；实盘功能不会因修改配置而自动开启。

