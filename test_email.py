from __future__ import annotations

import logging

from monitor.config import Settings
from monitor.email_notifier import EmailNotifier


def main() -> int:
    settings = Settings.from_env()
    logging.basicConfig(level=logging.INFO)

    if not settings.email_enabled:
        print(
            "邮件配置不完整。请先填写 .env 中的 SMTP_HOST、MAIL_FROM、MAIL_TO。"
        )
        return 2

    try:
        EmailNotifier(settings).send_test()
    except Exception as exc:
        print(f"测试邮件发送失败：{type(exc).__name__}: {exc}")
        return 1

    print("测试邮件已成功提交 SMTP 服务器，请检查收件箱和垃圾邮件。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
