#!/bin/sh
# Run on the Pi from the project folder: sets up the venv and the two systemd services.
set -e
cd "$(dirname "$0")/.."
DIR=$(pwd)
USER_NAME=$(id -un)
if [ ! -f config.json ]; then
    echo "config.json is missing: copy config.example.json to config.json and fill it in" >&2
    exit 1
fi
MDNS_NAME=$(python3 -c 'import json; print(json.load(open("config.json")).get("mdns_name") or "tv.local")')
command -v avahi-publish >/dev/null || sudo apt-get install -y avahi-daemon avahi-utils
command -v adb >/dev/null || sudo apt-get install -y adb
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
for unit in tv-remote tv-remote-mdns; do
    sed -e "s#@USER@#$USER_NAME#g" -e "s#@DIR@#$DIR#g" -e "s#@NAME@#$MDNS_NAME#g" \
        "deploy/$unit.service.in" | sudo tee "/etc/systemd/system/$unit.service" >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable --now tv-remote.service tv-remote-mdns.service
sudo systemctl restart tv-remote.service tv-remote-mdns.service
