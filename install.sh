#!/bin/sh
# Installs the heating agent services on an LXC (run as root in /opt/kurenie)
set -e
cd /opt/kurenie
timedatectl set-timezone Europe/Bratislava 2>/dev/null || ln -sf /usr/share/zoneinfo/Europe/Bratislava /etc/localtime
[ -f config.env ] || cp config.env.example config.env
chmod 600 config.env

unit() {  # $1 name, $2 description, $3 script, $4 extra Environment
cat > /etc/systemd/system/$1.service <<EOF
[Unit]
Description=$2
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/kurenie
Environment=PYTHONUNBUFFERED=1 TZ=Europe/Bratislava $4
ExecStart=/usr/bin/python3 /opt/kurenie/$3
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
}
unit kurenie-collector "Kurenie - zberac dat z ESP m5stack-tc" collector.py "TC_DB=/opt/kurenie/tc.db TC_END=0"
unit kurenie-monitor "Kurenie - povodny monitor (port 8099)" monitor/web.py "TC_DB=/opt/kurenie/tc.db TC_PORT=8099"
unit kurenie-web "Kurenie - web agenta (port 80)" app.py ""
unit kurenie-agent "Kurenie - agent (OpenRouter)" agent.py ""
systemctl daemon-reload
systemctl enable --now kurenie-collector kurenie-monitor kurenie-web kurenie-agent
systemctl restart kurenie-collector kurenie-monitor kurenie-web kurenie-agent
sleep 3
systemctl --no-pager --no-legend list-units 'kurenie-*'
echo "Ďalšie aktualizácie z gitu: cd /opt/kurenie && ./update.sh (náhľad: ./update.sh --dry-run)"
