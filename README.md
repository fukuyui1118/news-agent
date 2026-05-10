# News Agent

A long-running agent that monitors insurance and reinsurance news, classifies each headline into P1 / P2 / P3 / DROPPED, and emails curated Japanese-language summaries to a Tokyo investment desk. Runs on a t4g.nano EC2 instance for ~$4/month.

## What it does, in one paragraph

Twice a day at **07:00 and 19:00 JST**, the agent runs one full pipeline tick: fetches news from RSS feeds (13 native + 14 user-curated Inoreader keyword tags), asks **Claude Opus 4.7 + web_search** to research the past 12 hours of insurance-sector news via a structured two-stage prompt, then makes **one Opus call to classify** every fresh item into P1 (Japan / regulatory / financial), P2 (global insurer business news), or P3 (everything else — sports sponsorships, naming-rights events, noise). Items are persisted with the AI-assigned priority. A **second Opus call** drafts a curated Japanese-language digest from the last 12 hours of P1+P2 items, ranks Tier-1 events first, deduplicates same-event clusters, and emails it. P3 items go to the dashboard only.

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
   │ 1. Fetch (parallel) │  RSS × 13 native + 14 Inoreader keyword tags
   │                     │  (Inoreader via REST API → real publish times)
   │                     │  + Claude Research × 1 (cadence-gated 12h)
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
   │ 5. AI classify (Opus)│ one batched Claude Opus call across ALL fresh items
   │                     │  → P1 = Japan / regulatory / financial
   │                     │  → P2 = global insurer business news
   │                     │  → P3 = everything else (sports, sponsorships, noise)
   │                     │  Watchlist entities injected into the prompt as
   │                     │  context. Falls back to all-P3 on parse/API failure.
   └────────┬────────────┘
            ▼
   ┌─────────────────────┐
   │ 6. Persist          │  INSERT OR IGNORE on url_hash with AI priority
   └────────┬────────────┘
            ▼
   ┌─────────────────────┐
   │ 7. Compose + email  │  one Claude Opus call: dedup same-event clusters,
   │   (Opus)            │  rank P1 first, ≤15 entries with JP headlines + bullets
   │                     │  → single digest email per tick (07:00 / 19:00 JST)
   │                     │  P3 → dashboard only, no email
   │                     │  Falls back to per-row Haiku summarize on parse failure
   └─────────────────────┘
```

Source-tier rules:

- **Tier 1** — Claude Research source. Tier-1 was historically the "skip relevance gate" tier; with the AI classifier the gate concept is gone, but the Tier-1 label is preserved on the source for telemetry.
- **Tier 2** — business-focused industry sites (Reinsurance News, Artemis) + Inoreader keyword tags. AI classifier judges per-item.
- **Tier 3** — generalist insurance press (Insurance Journal, Carrier Management, PR/Globe Newswire). AI classifier judges per-item.

A single source failure never crashes a cycle — every `fetch()` is wrapped in try/except, logged, and the cycle continues.

---

## Email setup

### What gets emailed

| Priority | Email path | Cadence |
|---|---|---|
| **P1** (Japan-HQ insurer impact) | Included in the curated digest. | Twice daily, 07:00 and 19:00 JST |
| **P2** (Global majors) | Included in the curated digest. | Twice daily, 07:00 and 19:00 JST |
| **P3** | Never emailed. Visible only in the dashboard. | — |
| **DROPPED** | Never emailed. Row exists only so dedup catches the URL on the next tick. | — |

Each digest is produced by **one Claude Haiku call** that takes the last 12 hours of P1+P2 items and returns a ranked, deduplicated list (≤15 entries) with one-line Japanese headlines and 3–5-bullet Japanese summaries. Subject: `【ニュースエージェント】ダイジェスト MM/DD HH:00`. Same-event items from multiple outlets are collapsed to one entry.

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
python -m news_agent --fetch-and-digest-now            # one full pipeline tick (mirrors a 7AM/7PM JST run)
python -m news_agent --fetch-and-digest-now --dry-run  # full pipeline, print email instead of sending
python -m news_agent --digest-now                      # send digest from existing seen.db (no fetch)
python -m news_agent --digest-now --dry-run            # preview digest without sending
python -m news_agent --once                            # one fetch cycle only, no email
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
| **Full pipeline tick** | **07:00 and 19:00 JST** | `CronTrigger(hour='7,19', minute=0, timezone='Asia/Tokyo')` | Fetch all RSS + Claude Research (if cadence open) → classify → curate via Haiku → email digest. ~3 min wall time. |
| **Claude Research cadence** | **12h** between calls | self-skip in `ClaudeResearchSource` based on `api_usage` table | Aligns naturally with the 12h scheduler cadence (last call ~12h ago, opens for the next). ~$1–3/day. |
| **Recency filter** | per-tick | inline | RSS: 24h. Claude Research: 72h (allows fallback items). |
| **Cadence reset** | manual | n/a | To force a Claude Research call before its 12h window opens, delete the most recent `api_usage` row for that query and rerun `--once`. |

---

## Configuration files

| File | Purpose |
|---|---|
| [`config/config.yaml`](config/config.yaml) | Storage path, logging, scheduler intervals, recency filter. |
| [`config/feeds.yaml`](config/feeds.yaml) | RSS feed list (native + Inoreader tag URLs) + Claude Research query config. |
| [`config/watchlists.yaml`](config/watchlists.yaml) | P1 (Japan-HQ) and P2 (global) entities. Injected into the AI classifier prompt as context. |
| [`config/relevance.yaml`](config/relevance.yaml) | Legacy regex-based relevance keywords. Unused since Phase 9.2 — kept for reference. |
| [`.env`](.env.example) | Secrets (Anthropic API key, Gmail SMTP, Inoreader API). Never commit. |

---

## Inoreader API setup (one-time, ~10 min)

The 14 Inoreader keyword tags in `feeds.yaml` are fetched via Inoreader's REST API, not the public-RSS export. The API exposes the article's **true publish time** (the public RSS only shows Inoreader's ingestion timestamp), so the 24h recency filter actually works. If credentials are missing, the agent transparently falls back to the public-RSS path.

### Setup steps

1. **Register an app** at <https://www.inoreader.com/developers/>. Click "Create application", fill in:
   - **Name**: `News Agent` (or whatever)
   - **Description**: Personal automation
   - **Redirect URI**: `http://localhost:8765/callback` (paste EXACTLY — Inoreader rejects out-of-band URIs)
   - **Scopes**: `read`
2. Copy the resulting **App ID** and **App Secret** into your `.env`:

   ```env
   INOREADER_APP_ID=your-app-id
   INOREADER_APP_SECRET=your-app-secret
   ```

3. **Run the bootstrap script** to obtain a long-lived refresh token:

   ```bash
   .venv/bin/python scripts/inoreader_oauth_bootstrap.py
   ```

   The script binds `http://localhost:8765/callback`, opens the auth URL in your default browser, and waits for the redirect. Log in to Inoreader and click "Allow"; your browser briefly hits localhost (showing a "✅ Authorization captured" page), then the script prints:

   ```
   INOREADER_REFRESH_TOKEN=<long opaque string>
   ```

4. **Copy that line into your `.env`** (both laptop and EC2). Refresh tokens are long-lived (~1 year). The agent auto-refreshes the short-lived access token internally.

### How it works

- `agent.py::build_sources` detects URLs of form `https://www.inoreader.com/stream/user/<id>/tag/<name>` and routes them to `InoreaderSource` instead of `RSSSource`.
- `InoreaderSource` calls the API, maps each item's `published` field (unix seconds) to `RawItem.published_at`, and prefers `canonical[0].href` (publisher URL) over `alternate[0].href` (Google News wrapper).
- The shared `InoreaderClient` in `inoreader_oauth.py` caches the access token for 1 hour and refreshes on 401. Rotated refresh tokens are persisted back to `.env` automatically.

### Disabling

Just remove `INOREADER_REFRESH_TOKEN` from `.env`. The agent will log `inoreader.fallback_to_rss` for each tag and use the public-RSS export instead (with the recency-leakage caveat).

---

## Run modes

```bash
python -m news_agent --once                       # one fetch cycle, no email
python -m news_agent --once --dry-run             # one cycle, no email regardless of mailer config
python -m news_agent --fetch-and-digest-now       # one full pipeline tick (mirrors a 07:00/19:00 JST run)
python -m news_agent --fetch-and-digest-now --dry-run
python -m news_agent --digest-now                 # send digest from existing seen.db (no fetch)
python -m news_agent --digest-now --dry-run       # preview digest, no send
python -m news_agent                              # long-running scheduler (production)
python -m news_agent --dry-run                    # scheduler in dry-run
python -m news_agent --stats                      # one-shot DB/feed/api_usage summary
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
| Anthropic — Claude Research (Opus 4.7 + Haiku 4.5, 2 calls/day) | ~$30–60 |
| Anthropic — AI Classifier (Opus 4.7 batched, 2 calls/day) | ~$12 |
| Anthropic — AI Email Composer (Opus 4.7, 2 calls/day) | ~$30 |
| Anthropic — Inoreader Pro (paid externally) | $7.50/mo annual |
| Gmail SMTP | free |
| **Total** | **~$83–113/month** |

Cost-down options if needed:
- AI Classifier → Haiku 4.5: saves ~$11/mo (yes/no per item doesn't need Opus reasoning).
- Disable Claude Research entirely (`enabled: false`): saves $30–60/mo, falls back to RSS-only.
