from __future__ import annotations

import html
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any

from .config import Settings
from .events import classify_direction, format_timestamp


class EmailNotifier:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def send_orders(
        self, rows: list[dict[str, Any]], recipient: str | None = None,
        unsubscribe_url: str | None = None,
    ) -> None:
        if not self.settings.email_enabled:
            raise RuntimeError(
                "SMTP 未配置：请在 .env 中填写 SMTP_HOST、MAIL_FROM、MAIL_TO"
            )

        subject = (
            f"{self.settings.mail_subject_prefix} "
            f"检测到 {len(rows)} 条新交易记录"
        )

        text_lines = [
            f"带单员：{self.settings.trader_name}",
            f"Portfolio ID：{self.settings.portfolio_id}",
            f"检测到新增订单：{len(rows)} 条",
            "",
        ]

        html_rows = []
        for row in rows:
            try:
                raw = row.get("raw_json")
                if isinstance(raw, str):
                    import json
                    raw = json.loads(raw)
                elif not isinstance(raw, dict):
                    raw = row
            except Exception:
                raw = row

            symbol = raw.get("symbol", "未知")
            direction = classify_direction(raw)
            side = raw.get("side", "")
            position_side = raw.get("positionSide", "")
            qty = raw.get("executedQty", "")
            price = raw.get("avgPrice", "")
            pnl = raw.get("totalPnl", "")
            order_type = raw.get("type", "")
            timestamp = format_timestamp(
                raw.get("orderTime") or raw.get("orderUpdateTime"),
                self.settings.timezone,
            )

            text_lines.extend([
                f"{symbol} | {direction}",
                f"订单方向：{side} / {position_side}",
                f"成交数量：{qty}",
                f"平均价格：{price}",
                f"订单类型：{order_type}",
                f"totalPnl：{pnl}",
                f"订单时间：{timestamp}",
                "",
            ])

            cells = [
                symbol, direction, f"{side}/{position_side}",
                str(qty), str(price), str(pnl), timestamp,
            ]
            html_rows.append(
                "<tr>" + "".join(
                    f"<td>{html.escape(str(value))}</td>"
                    for value in cells
                ) + "</tr>"
            )

        text_body = "\n".join(text_lines)
        if unsubscribe_url:
            text_body += f"\n退订通知：{unsubscribe_url}\n"
        unsubscribe_html = (
            f'<p><a href="{html.escape(unsubscribe_url, quote=True)}">退订通知</a></p>'
            if unsubscribe_url else ""
        )
        html_body = f"""
        <html><body style="font-family:Arial,'Microsoft YaHei',sans-serif">
          <h2>{html.escape(self.settings.trader_name)}：发现新交易记录</h2>
          <p>本轮检测到 <b>{len(rows)}</b> 条新增订单。</p>
          <table cellpadding="8" cellspacing="0" border="1"
                 style="border-collapse:collapse;font-size:13px">
            <thead><tr>
              <th>交易对</th><th>方向</th><th>Side</th>
              <th>成交数量</th><th>平均价格</th><th>totalPnl</th><th>订单时间</th>
            </tr></thead>
            <tbody>{''.join(html_rows)}</tbody>
          </table>
          <p style="color:#777;font-size:12px">
            方向字段是基于 side / positionSide 的判断，无法仅凭公开订单记录
            确定首次开仓、加仓或部分/全部平仓。
          </p>
          <p><a href="{html.escape(
              self.settings.binance_referer, quote=True
          )}">打开币安带单员主页</a></p>
          {unsubscribe_html}
        </body></html>
        """

        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self.settings.mail_from
        message["To"] = recipient or self.settings.mail_to
        message.set_content(text_body)
        message.add_alternative(html_body, subtype="html")

        self._send(message)

    def send_confirmation(self, recipient: str, url: str) -> None:
        message = EmailMessage()
        message["Subject"] = f"{self.settings.mail_subject_prefix} 确认邮件订阅"
        message["From"] = self.settings.mail_from
        message["To"] = recipient
        message.set_content(f"请打开以下链接并确认订阅：\n{url}\n\n如果不是你操作，请忽略此邮件。")
        self._send(message)

    def send_unsubscribe(self, recipient: str, url: str) -> None:
        message = EmailMessage()
        message["Subject"] = f"{self.settings.mail_subject_prefix} 退订邮件通知"
        message["From"] = self.settings.mail_from
        message["To"] = recipient
        message.set_content(f"请打开以下链接并确认退订：\n{url}\n\n如果不是你操作，请忽略此邮件。")
        self._send(message)

    def _send(self, message: EmailMessage) -> None:
        security = self.settings.smtp_security
        if security == "ssl":
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(
                self.settings.smtp_host,
                self.settings.smtp_port,
                timeout=20,
                context=context,
            ) as server:
                self._login_if_needed(server)
                refused = server.send_message(message)
        elif security == "starttls":
            with smtplib.SMTP(
                self.settings.smtp_host,
                self.settings.smtp_port,
                timeout=20,
            ) as server:
                server.ehlo()
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
                self._login_if_needed(server)
                refused = server.send_message(message)
        elif security == "none":
            with smtplib.SMTP(
                self.settings.smtp_host,
                self.settings.smtp_port,
                timeout=20,
            ) as server:
                self._login_if_needed(server)
                refused = server.send_message(message)
        else:
            raise RuntimeError(
                "SMTP_SECURITY 仅支持 ssl、starttls、none"
            )
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)

    def _login_if_needed(self, server: smtplib.SMTP) -> None:
        if self.settings.smtp_username:
            server.login(
                self.settings.smtp_username,
                self.settings.smtp_password,
            )

    def send_test(self) -> None:
        self.send_orders([{
            "raw_json": (
                '{"symbol":"TESTUSDT","side":"BUY","positionSide":"LONG",'
                '"executedQty":"1","avgPrice":"1.0","totalPnl":"0",'
                '"type":"TEST","orderTime":0}'
            )
        }])
