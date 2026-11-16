如果 VPS 上没有 monitor 用户，可以创建：

sudo useradd --system --home /opt/binance-trader-monitor --shell /usr/sbin/nologin monitor
sudo chown -R monitor:monitor /opt/binance-trader-monitor

如果你使用自己的 Linux 用户运行，编辑 systemd 文件里的 User 和 Group。
