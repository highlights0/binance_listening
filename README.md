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
