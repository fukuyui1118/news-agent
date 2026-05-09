#!/usr/bin/env bash
# Bootstrap a fresh Ubuntu 24.04 (arm64) EC2 t4g.nano instance.
# Idempotent: safe to re-run.
#
# Usage (after SSH'ing into the VM as `ubuntu`):
#   curl -fsSL https://raw.githubusercontent.com/<your-fork>/News_Agent/main/deploy/setup-ec2.sh | sudo bash
# Or scp this file up and `sudo bash setup-ec2.sh`.

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/REPLACE_ME/News_Agent.git}"
APP_USER=news-agent
APP_HOME=/opt/news-agent
APP_DIR="${APP_HOME}/News_Agent"

echo "==> Updating apt and installing system packages"
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
    python3 python3-venv python3-dev \
    git sqlite3 curl ca-certificates build-essential

echo "==> Ensuring 1 GB swapfile (safety net for 0.5 GB RAM)"
if [[ ! -f /swapfile ]]; then
    fallocate -l 1G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    grep -q "^/swapfile" /etc/fstab || echo "/swapfile none swap sw 0 0" >> /etc/fstab
    echo "    swap enabled."
else
    echo "    swap already present."
fi

echo "==> Creating service user ${APP_USER}"
if ! id -u "${APP_USER}" >/dev/null 2>&1; then
    useradd --system --create-home --home-dir "${APP_HOME}" --shell /usr/sbin/nologin "${APP_USER}"
fi
mkdir -p "${APP_HOME}"
chown -R "${APP_USER}:${APP_USER}" "${APP_HOME}"

echo "==> Cloning / updating repo"
sudo -u "${APP_USER}" bash <<EOF
set -euo pipefail
if [[ ! -d "${APP_DIR}/.git" ]]; then
    git clone "${REPO_URL}" "${APP_DIR}"
else
    cd "${APP_DIR}" && git fetch && git reset --hard origin/main
fi
EOF

echo "==> Setting up venv and installing deps"
sudo -u "${APP_USER}" bash <<EOF
set -euo pipefail
cd "${APP_DIR}"
if [[ ! -d .venv ]]; then
    python3 -m venv .venv
fi
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e ".[dashboard]"
mkdir -p logs
EOF

echo "==> Ensuring .env exists (placeholder)"
if [[ ! -f "${APP_DIR}/.env" ]]; then
    sudo -u "${APP_USER}" cp "${APP_DIR}/.env.example" "${APP_DIR}/.env"
    chmod 600 "${APP_DIR}/.env"
    echo "    created ${APP_DIR}/.env from template — edit it now to add real secrets."
fi

echo "==> Installing systemd units"
install -m 0644 "${APP_DIR}/deploy/news-agent.service"     /etc/systemd/system/news-agent.service
install -m 0644 "${APP_DIR}/deploy/news-dashboard.service" /etc/systemd/system/news-dashboard.service
systemctl daemon-reload
systemctl enable news-agent news-dashboard

echo
echo "==> SETUP COMPLETE"
echo
echo "Next steps (run as root or with sudo):"
echo "  1. Edit ${APP_DIR}/.env and fill in ANTHROPIC_API_KEY, SMTP_*, EMAIL_*"
echo "  2. systemctl start news-agent news-dashboard"
echo "  3. systemctl status news-agent"
echo "  4. journalctl -u news-agent -f      # follow logs"
echo
echo "Dashboard binds to 127.0.0.1:8501. To access from your laptop:"
echo "  ssh -L 8501:localhost:8501 ubuntu@<elastic-ip>"
echo "  open http://localhost:8501"
