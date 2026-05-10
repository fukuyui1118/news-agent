# News Agent

A long-running agent that monitors insurance and reinsurance news, classifies each headline into P1 / P2 / P3 / DROPPED, and emails curated Japanese-language summaries to a Tokyo investment desk. Runs on a t4g.nano EC2 instance for ~$4/month.

## What it does, in one paragraph

Every hour, the agent fetches news from 13 RSS feeds. Twice a day (12-hour cadence), it also asks **Claude Opus 4.7 + web_search** to research the past 24 hours of insurance-sector news using a structured two-stage prompt. Every fetched item is dedup-checked, classified by entity (Tokio Marine, Munich Re, etc.) and by relevance keywords, then persisted in `seen.db`. P1 (Japan-impact) items are batched into an email every 3 hours; a daily 07:00 JST digest emails P1+P2 from the last 24 hours. P3 items are visible only in the dashboard. DROPPED items are stored only so dedup catches them in the next cycle.

---

## Search strategy

Two layers, run in parallel each cycle:

### Layer 1 — Native RSS (every cycle, free)

13 feeds defined in [`config/feeds.yaml`](config/feeds.yaml). Three types:

| Group | Feeds |
|---|---|
| Core English trade press | Reinsurance News, Artemis, Insurance Journal (International + National), Carrier Management |
| Insurance Business regional | America, UK, Asia |
| Press-release wires (noisy, gate filters) | PR Newswire, GlobeNewswire |
| Japanese press | Nikkei Asia, 東洋経済オンライン, 朝日新聞 (経済) |

All feeds run in parallel via `asyncio.to_thread` + `Semaphore(10)`. Cycle time ~80–95s.

### Layer 2 — Claude Research (twice daily, paid)

One source: `Claude Research: insurance sector research (JP-focused)`. Cadence-gated to 12h via the `api_usage` table — the hourly cycle invokes it but it self-skips if the last successful call was within 12 hours. Tier 1 (bypasses the relevance gate).

The prompt runs in two stages:

**Stage 1 — Discovery** ([`src/news_agent/sources/claude_research.py::DISCOVERY_PROMPT_TEMPLATE`](src/news_agent/sources/claude_research.py))

- Opus 4.7 + `web_search` tool, max 12 searches, max ~30 headlines.
- Tier 1/2/3 priority taxonomy:
  - **T1 (must-find)**: rating actions (AM Best / S&P / Moody's / Fitch / R&I / JCR), capital-markets transactions (cat bonds, hybrids, IPOs), M&A and strategic-stake sales, regulator actions (FSA / EIOPA / NAIC / PRA / BMA / IAIS), large-loss events.
  - **T2 (nice-to-have)**: earnings, leadership changes, reinsurance renewals, product launches.
  - **T3 (excluded)**: branch openings, sponsorships, CSR, awards, seminars.
- Watchlist entities are injected dynamically from [`config/watchlists.yaml`](config/watchlists.yaml) (currently 18 JP + 32 global insurers).
- 24-hour strict window, with a 72-hour fallback if in-window items <5 (annotates fallback items with `[T-{n}h]`).
- Aggregator-first search order: TDnet → FSA → Artemis → AM Best → ratings agencies → entity-batch queries.
- Each headline labeled with `published_confidence` (`verified | inferred_high | inferred_low`); `inferred_low` items are dropped.
- Mandatory `COVERAGE_NOTES:` block at end (`searches_run`, `tier1_aggregators_hit`, `fallback_used`, `gaps`) — captured into the `api_usage` table for telemetry.

**Stage 2 — Structuring** ([`src/news_agent/sources/claude_research.py::STRUCTURING_PROMPT_TEMPLATE`](src/news_agent/sources/claude_research.py))

- Haiku 4.5, no tools, ~30s, very cheap.
- Converts the prose bullet list into strict JSON with `category`, `tier`, `entity`, `published_at`, `published_confidence`, `summary_ja`, `other_sources`.
- Enforces title-prefix Jaccard dedup, drops `inferred_low`, drops items >24h old when `fallback_used=false`.

---

## AI agent flow (per fetch cycle)

```
   Schedule (hourly)
        │
        ▼
   ┌─────────────────────┐
   │ 1. Fetch (parallel) │  RSS × 13  +  Claude Research × 1 (cadence-gated)
   └────────┬────────────┘
            ▼
   ┌─────────────────────┐
   │ 2. Canonicalize URL │  strip utm/ref/fbclid/etc., normalize host
   └────────┬────────────┘
            ▼
   ┌─────────────────────┐
   │ 3. Hash + dedup     │  url_hash (PK) + content_hash (cross-source)
   └────────┬────────────┘
            ▼
   ┌─────────────────────┐
   │ 4. Recency filter   │  RSS: 24h    Claude Research: 72h    (per-source)
   └────────┬────────────┘
            ▼
   ┌─────────────────────┐
   │ 5. Entity classify  │  word-boundary regex against watchlists.yaml
   │                     │  P1 (Japan-HQ) wins over P2 (global)
   └────────┬────────────┘
            ▼
   ┌─────────────────────┐
   │ 6. Relevance gate   │  unmatched stories only:
   │                     │    Tier 1 source → always P3 (skip gate)
   │                     │    Tier 2/3 source → ≥1 business keyword → P3, else DROPPED
   └────────┬────────────┘
            ▼
   ┌─────────────────────┐
   │ 7. Persist          │  INSERT OR IGNORE on url_hash; record dropped_reason
   └────────┬────────────┘
            ▼
   ┌─────────────────────┐
   │ 8. Summarize + send │  P1 → Claude Haiku Japanese summary → P1 batch (3-hour cadence)
   │                     │  P2 → wait for daily digest
   │                     │  P3 → dashboard only, no email
   │                     │  DROPPED → never emailed; row exists only for dedup
   └─────────────────────┘
```

Source-tier rules:

- **Tier 1** — carrier IR / press release feeds, Claude Research. Relevance gate skipped.
- **Tier 2** — business-focused industry sites (Reinsurance News, Artemis). Gate applies but signal is strong.
- **Tier 3** — generalist insurance press (Insurance Journal). Gate does heavy lifting.

A single source failure never crashes a cycle — every `fetch()` is wrapped in try/except, logged, and the cycle continues.

---

## Email setup

### What gets emailed

| Priority | Email path | Recipient cadence |
|---|---|---|
| **P1** (Japan-HQ insurer impact) | Batched **every 3 hours**. Subject `【ニュースエージェント P1】MM/DD HH:00 (N件)`. Title-similarity dedup against the last 24h of emailed P1 titles (Jaccard ≥ 0.55) so the same event from two outlets is collapsed to one entry. | Real-time-ish |
| **P2** (Global majors) | **Daily 07:00 JST digest**. Subject `【ニュースエージェント】日次ダイジェスト MM/DD`. Includes P1+P2 from the last 24 hours (P1 stories may already have been sent in 3-hour batches; the digest is a curated daily recap). | Once per day |
| **P3** | Never emailed. Visible only in the dashboard. | — |
| **DROPPED** | Never emailed. Row exists only so dedup catches the URL on the next cycle. | — |

Each P1/P2 entry in an email is summarized in Japanese by Claude Haiku 4.5 (`(headline, bullets)` format).

### SMTP config

Gmail SMTP via STARTTLS. Configured in `.env` (see [`.env.example`](.env.example)):

```
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your.address@gmail.com
SMTP_PASSWORD=<16-char Gmail app password>     # NOT your normal Gmail password
EMAIL_FROM=your.address@gmail.com
EMAIL_TO=fuku11184649@gmail.com
```

Generate an app password at https://myaccount.google.com/apppasswords (requires 2FA enabled on the Gmail account). The 16-character app password is what goes in `SMTP_PASSWORD` — using your login password will fail.

### Manual triggers

```bash
python -m news_agent --p1-batch-now            # force P1 batch now
python -m news_agent --p1-batch-now --dry-run  # preview without sending
python -m news_agent --digest-now              # force daily digest now
python -m news_agent --digest-now --dry-run    # preview without sending
```

---

## Dashboard

Read-only Streamlit dashboard at [`dashboard.py`](dashboard.py). Shows everything in `seen.db` with:

- Sidebar filters: priority (P1/P2/P3/DROPPED), email status, source, title search, date range.
- Email-status indicators: ✅ sent / 🚫 dropped / ⏳ P1 pending / 🕐 awaiting digest.
- Auto-refresh every 30 seconds; manual 🔄 Refresh for immediate reload.

The dashboard reads `seen.db` directly via SQLite — it has no write access and cannot interfere with the running agent.

### Local

```bash
pip install -e ".[dashboard]"
.venv/bin/streamlit run dashboard.py     # binds 127.0.0.1:8501
# open http://localhost:8501
```

### On EC2

The dashboard runs as `news-dashboard.service`, bound to `127.0.0.1:8501` only. Reach it from your laptop via SSH tunnel:

```bash
ssh -L 8501:localhost:8501 -i ~/.ssh/news-agent-key.pem ubuntu@<elastic-ip>
# then open http://localhost:8501 in the laptop's browser
```

Don't expose 8501 publicly — there's no auth. Use the SSH tunnel.

---

## Frequency cheat sheet

All driven by APScheduler in [`src/news_agent/scheduler.py`](src/news_agent/scheduler.py). Configured in [`config/config.yaml`](config/config.yaml).

| Job | Schedule | Trigger | What it does |
|---|---|---|---|
| **Fetch cycle** | every **60 minutes** | `IntervalTrigger(minutes=60)` | Fetch all RSS + Claude Research (if cadence open), classify, persist. ~80–95s wall time. |
| **Claude Research cadence** | **12h** between calls | self-skip in `ClaudeResearchSource` based on `api_usage` table | Inside the hourly cycle, the source self-skips if it ran <12h ago. ~$1–2/day. |
| **P1 batch email** | every **3 hours** | `IntervalTrigger(hours=3)` | Email any unemailed P1 stories from the last 24h. |
| **Daily digest** | **07:00 JST** every day | `CronTrigger(hour=7, minute=0, timezone="Asia/Tokyo")` | Email all P1+P2 stories from the last 24h. |
| **Recency filter** | n/a (per-cycle) | inline | RSS: 24h. Claude Research: 72h (allows fallback items). |
| **Cadence reset on key change** | n/a | manual | Touching the model name in `feeds.yaml` doesn't reset cadence — to force a Claude Research call now, run `python -m news_agent --once` after wiping the relevant `api_usage` row. |

---

## Configuration files

| File | Purpose |
|---|---|
| [`config/config.yaml`](config/config.yaml) | Storage path, logging, scheduler intervals, recency filter. |
| [`config/feeds.yaml`](config/feeds.yaml) | RSS feed list + Claude Research query config. |
| [`config/watchlists.yaml`](config/watchlists.yaml) | P1 (Japan-HQ) and P2 (global) entities with aliases and exclusion terms. **Drives both query generation AND classification.** |
| [`config/relevance.yaml`](config/relevance.yaml) | Business keywords for the post-fetch relevance gate. |
| [`.env`](.env.example) | Secrets (Anthropic API key, Gmail SMTP). Never commit. |

---

## Run modes

```bash
python -m news_agent --once                  # one fetch cycle, exit
python -m news_agent --once --dry-run        # one cycle, no emails
python -m news_agent                         # long-running scheduler (production)
python -m news_agent --dry-run               # scheduler in dry-run
python -m news_agent --p1-batch-now          # force P1 batch
python -m news_agent --digest-now            # force daily digest
```

---

## Deployment

Production runs on a t4g.nano EC2 instance in ap-northeast-1 (or ap-southeast-2 in this deployment). Full instructions in [`deploy/README.md`](deploy/README.md). Bootstrap script at [`deploy/setup-ec2.sh`](deploy/setup-ec2.sh). Two systemd units:

- `news-agent.service` — the scheduler, restarts on failure.
- `news-dashboard.service` — Streamlit on `127.0.0.1:8501`.

Update flow:

```bash
git push origin main
ssh -i ~/.ssh/news-agent-key.pem ubuntu@<host> \
    'sudo -u news-agent git -C /opt/news-agent/News_Agent pull && \
     sudo systemctl daemon-reload && \
     sudo systemctl restart news-agent news-dashboard'
```

---

## Cost summary

| Component | Monthly |
|---|---|
| EC2 t4g.nano + 8GB EBS + outbound | ~$3.81 |
| Anthropic API — Claude Research (Opus 4.7 + Haiku 4.5, 2 calls/day) | ~$30–60 |
| Anthropic API — P1/digest summaries (Haiku 4.5) | ~$1–3 |
| Gmail SMTP | free |
| **Total** | **~$35–67/month** |

Drop Claude Research's `model` to `claude-haiku-4-5` to cut the bulk of the API spend ~10×. Disable Claude Research entirely (`enabled: false`) to fall back to RSS-only at near-zero cost.
