# News Agent — AWS EC2 deployment

Deploy the News Agent and its Streamlit dashboard on a t4g.nano instance for ~$4/month.

## Cost (ap-northeast-1, on-demand)

| Item                     | Monthly |
|--------------------------|---------|
| EC2 t4g.nano             | $3.07   |
| 8 GB gp3 EBS root        | $0.64   |
| Elastic IP (attached)    | free    |
| Outbound transfer (~1GB) | $0.10   |
| **Total**                | **~$3.81** |

Drops to ~$2/mo with a 1-year Reserved Instance commit.

## Architecture on the VM

```
┌──────────────────────────────────────────────┐
│  systemd                                      │
│  ├─ news-agent.service                        │
│  │   └─ python -m news_agent  (scheduler)     │
│  │       ├─ fetch every 30 min                │
│  │       ├─ P1 batch every 3 hours            │
│  │       └─ daily digest 07:00 JST            │
│  └─ news-dashboard.service                    │
│      └─ streamlit run dashboard.py            │
│          (binds 127.0.0.1:8501)               │
│                                               │
│  ~/.../News_Agent/seen.db   (SQLite, shared)  │
└──────────────────────────────────────────────┘
```

The dashboard binds to `127.0.0.1:8501` only — accessed via SSH tunnel from your laptop. Don't expose 8501 publicly.

## One-time deployment steps

### 1. Launch the instance

In the AWS console (or `aws ec2 run-instances`):

- **Region**: `ap-northeast-1` (Tokyo) — closest to JP sources.
- **AMI**: Ubuntu Server 24.04 LTS (arm64).
- **Instance type**: `t4g.nano`.
- **Storage**: 8 GiB gp3.
- **Security group**:
  - Inbound TCP 22 from your IP (SSH).
  - **Nothing else** — the dashboard is reached via SSH tunnel.
- **Key pair**: existing SSH key.

Allocate an Elastic IP and associate it with the instance.

### 2. SSH in and run the bootstrap

```bash
ssh ubuntu@<elastic-ip>
```

On the VM:

```bash
# Either fetch the script directly:
curl -fsSL https://raw.githubusercontent.com/<your-fork>/News_Agent/main/deploy/setup-ec2.sh \
    | sudo REPO_URL=https://github.com/<your-fork>/News_Agent.git bash

# Or scp the script up first:
# (from your laptop)
scp deploy/setup-ec2.sh ubuntu@<elastic-ip>:/tmp/
ssh ubuntu@<elastic-ip>
sudo bash /tmp/setup-ec2.sh
```

The script:
- installs Python 3.12, sqlite3, build tools
- creates a 1 GB swapfile (safety net for 0.5 GB RAM)
- creates a `news-agent` system user with home `/opt/news-agent`
- clones the repo into `/opt/news-agent/News_Agent`
- creates a venv and installs `news-agent[dashboard]` (no browser-use, keeps it lean)
- installs both systemd units and `enables` them
- copies `.env.example` → `.env` (you must edit it)

### 3. Configure secrets

```bash
sudo -u news-agent vim /opt/news-agent/News_Agent/.env
```

Fill in:
```
ANTHROPIC_API_KEY=sk-ant-...
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your.address@gmail.com
SMTP_PASSWORD=<16-char-gmail-app-password>
EMAIL_FROM=your.address@gmail.com
EMAIL_TO=fuku11184649@gmail.com
```

### 4. Start the services

```bash
sudo systemctl start news-agent news-dashboard
sudo systemctl status news-agent
sudo journalctl -u news-agent -f      # follow live
```

The agent runs an immediate fetch on start; you should see `cycle.done` within ~30 seconds. P1 batches fire every 3 hours; daily digest at 07:00 JST.

### 5. View the dashboard

From your laptop:

```bash
ssh -L 8501:localhost:8501 ubuntu@<elastic-ip>
```

Then open http://localhost:8501 in your browser. The tunnel routes through SSH — no firewall change needed.

## Day-2 operations

- **Logs**: `sudo journalctl -u news-agent -n 200`
- **Restart**: `sudo systemctl restart news-agent`
- **Update code**: `sudo -u news-agent git -C /opt/news-agent/News_Agent pull && sudo systemctl restart news-agent news-dashboard`
- **Stop everything**: `sudo systemctl stop news-agent news-dashboard`
- **Disk usage**: `du -sh /opt/news-agent/News_Agent/{seen.db,logs}`

## Troubleshooting

- **OOM kills**: monitor with `sudo dmesg | grep -i kill`. If frequent, upgrade to t4g.micro.
- **Dashboard shows empty**: check `seen.db` is populated — agent must complete at least one fetch cycle first.
- **No emails arriving**: confirm `SMTP_PASSWORD` is a 16-char Gmail app password (not your login password). Test with `--p1-batch-now --dry-run` to see what would be sent.
- **Cost spike**: `aws ce get-cost-and-usage` or look at the Anthropic Console. The relevance gate + suffix queries cap LLM call volume; should stay under $5/mo total Anthropic spend.

## Optional: HTTPS dashboard with nginx + Let's Encrypt

If you want the dashboard accessible from any browser (not via SSH tunnel), add nginx + Certbot. Out of scope for the basic deploy — open the SSH-tunnel approach is simpler and more secure for personal use.
