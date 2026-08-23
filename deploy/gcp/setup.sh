#!/usr/bin/env bash
# One-time provisioning for the EdgeRunner live VM (Debian 12, GCP e2-micro).
# Run as root on a fresh instance:  sudo bash setup.sh
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/manamsriram/EdgeRunner.git}"
APP_DIR=/opt/edgerunner
DUCKDNS_DOMAIN="${DUCKDNS_DOMAIN:-edgerunner-live}"
LIVE_BRANCH="${LIVE_BRANCH:-live}"
DUCKDNS_TOKEN="${DUCKDNS_TOKEN:?set DUCKDNS_TOKEN from duckdns.org}"

echo "==> packages"
apt-get update -qq
apt-get install -y -qq python3.11 python3.11-venv python3-pip git curl \
	debian-keyring debian-archive-keyring apt-transport-https

echo "==> swap (4G)"
# The app OOM'd at 512 MB on Render. This box has 1 GB; swap is the shock
# absorber for per-tick allocation spikes, NOT a substitute for real RAM.
# swappiness=10 keeps the hot path resident and only spills genuine overflow.
if [[ ! -f /swapfile ]]; then
	fallocate -l 4G /swapfile
	chmod 600 /swapfile
	mkswap /swapfile
	swapon /swapfile
	echo '/swapfile none swap sw 0 0' >>/etc/fstab
fi
sysctl -w vm.swappiness=10
grep -q '^vm.swappiness' /etc/sysctl.conf || echo 'vm.swappiness=10' >>/etc/sysctl.conf

echo "==> user + dirs"
id -u edgerunner &>/dev/null || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin edgerunner
mkdir -p "$APP_DIR/state" /var/log/caddy

echo "==> app"
# Full clone, not --depth 1: deploy.sh needs real history to diff and roll back.
# This box tracks the `live` branch, never `main` — see deploy.sh.
if [[ ! -d "$APP_DIR/app/.git" ]]; then
	git clone "$REPO_URL" "$APP_DIR/app"
fi
cd "$APP_DIR/app"
git fetch --force --prune origin "+refs/heads/*:refs/remotes/origin/*"
if ! git rev-parse -q --verify "origin/${LIVE_BRANCH}^{commit}" >/dev/null; then
	echo "!! Branch 'origin/${LIVE_BRANCH}' does not exist. Create it from main first:"
	echo "     git checkout -b ${LIVE_BRANCH} main && git push -u origin ${LIVE_BRANCH}"
	exit 1
fi
echo "   pinning to origin/${LIVE_BRANCH} ($(git rev-parse --short "origin/${LIVE_BRANCH}"))"
git checkout -q --detach "origin/${LIVE_BRANCH}"
python3.11 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/app/requirements.txt"

echo "==> env file"
if [[ ! -f "$APP_DIR/gcp.env" ]]; then
	echo "!! Copy gcp.env to $APP_DIR/gcp.env and fill the REPLACE_ placeholders, then rerun."
	exit 1
fi
if grep -q 'REPLACE_WITH' "$APP_DIR/gcp.env"; then
	echo "!! $APP_DIR/gcp.env still has REPLACE_WITH placeholders. Fill them first."
	exit 1
fi
chown -R edgerunner:edgerunner "$APP_DIR"
chmod 600 "$APP_DIR/gcp.env"

echo "==> caddy"
if ! command -v caddy &>/dev/null; then
	curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' |
		gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
	curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
		>/etc/apt/sources.list.d/caddy-stable.list
	apt-get update -qq && apt-get install -y -qq caddy
fi
sed "s/edgerunner-live\.duckdns\.org/${DUCKDNS_DOMAIN}.duckdns.org/" "$APP_DIR/app/deploy/gcp/Caddyfile" >/etc/caddy/Caddyfile
chown -R caddy:caddy /var/log/caddy

echo "==> duckdns updater"
# The VM uses an ephemeral external IP (a reserved static IP bills when the VM
# is stopped; ephemeral does not). The IP changes across stop/start, so refresh
# the DNS record every 5 min.
# DuckDNS answers 200 with a body of "KO" when the token or domain is wrong,
# so curl's exit status says nothing. Check the body or the failure is silent
# and the only symptom is Caddy failing to get a cert ten minutes later.
cat >/usr/local/bin/duckdns-update <<EOF
#!/bin/sh
resp=\$(curl -fsS "https://www.duckdns.org/update?domains=${DUCKDNS_DOMAIN}&token=${DUCKDNS_TOKEN}&ip=")
[ "\$resp" = "OK" ] || { echo "duckdns update failed: \$resp" >&2; exit 1; }
EOF
chmod 700 /usr/local/bin/duckdns-update
if ! /usr/local/bin/duckdns-update; then
	echo "!! DuckDNS rejected the update (bad token or the domain is not on this account)."
	echo "   Verify with:  curl \"https://www.duckdns.org/update?domains=${DUCKDNS_DOMAIN}&token=<token>&ip=\""
	echo "   It must print OK. Then rerun this script with the correct DUCKDNS_TOKEN."
	exit 1
fi
cat >/etc/cron.d/duckdns <<'EOF'
*/5 * * * * root /usr/local/bin/duckdns-update
EOF

# Caddy's ACME HTTP-01 challenge fails if the name doesn't resolve to this box
# yet, and a failed issuance backs off for minutes. Wait for propagation first.
MY_IP=$(curl -fsS -H 'Metadata-Flavor: Google' \
	'http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip')
echo "   VM external IP: $MY_IP"
for _ in $(seq 1 30); do
	RESOLVED=$(getent hosts "${DUCKDNS_DOMAIN}.duckdns.org" | awk '{print $1}' | head -1 || true)
	[[ "$RESOLVED" == "$MY_IP" ]] && break
	echo "   waiting for DNS (${DUCKDNS_DOMAIN}.duckdns.org -> ${RESOLVED:-none})"
	sleep 10
done
if [[ "${RESOLVED:-}" != "$MY_IP" ]]; then
	echo "!! DNS still not pointing at $MY_IP. Check the token/domain, then rerun."
	exit 1
fi

echo "==> services"
cp "$APP_DIR/app/deploy/gcp/edgerunner.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now edgerunner caddy
systemctl restart caddy

echo
echo "Done. Watch it start:  journalctl -u edgerunner -f"
echo "Health:                curl https://${DUCKDNS_DOMAIN}.duckdns.org/"
