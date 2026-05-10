# CLAUDE.md — News_Agent

Long-running agent monitoring insurance/reinsurance news. **Fetches every 1 hour**, classifies into P1/P2/P3 (or DROPPED). Sends Japanese-language emails: P1 batched every 3 hours, daily 07:00 JST digest of P1+P2.

**Recency rule (Phase 5):** only items whose source-stated `published_at` is within the last 24 hours are persisted. Items with no publication date are skipped with a `source.no_pubdate` warning. Older items are skipped silently with a `story.too_old` info log.

## Setup
```bash
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # fill in ANTHROPIC_API_KEY + Gmail SMTP creds
```

## Run
```bash
python -m news_agent --once                  # one fetch cycle, exit
python -m news_agent --p1-batch-now          # force P1 batch (3-hour cadence) now
python -m news_agent --p1-batch-now --dry-run # preview P1 batch email
python -m news_agent --digest-now            # force daily digest (P1+P2)
python -m news_agent --digest-now --dry-run  # preview digest
python -m news_agent                         # long-running scheduler
python -m news_agent --dry-run               # scheduler in dry-run mode
```

## Email cadence (Phase 4)
- **P1 (Japan-impact)**: batched every 3 hours. Subject `[News Agent P1] MM/DD HH:00 (N件)`. Title-similarity dedup against the last 24h of emailed P1 titles (Jaccard, threshold 0.55) — same event from two sources → one entry.
- **Daily digest**: 07:00 JST, includes P1+P2 from the last 24h (P1 stories may have already been sent in the 3-hour batches; the digest is a curated daily recap). Subject `Daily Insurance news MM/DD`.
- **P3**: never emailed. Visible only in the dashboard.
- **DROPPED**: never emailed. Persisted only so dedup catches them next cycle.

## Pipeline (data flow)
1. Fetch → 2. Canonicalize URL → 3. Hash → 4. Dedup check
5. **Entity classify** (P1 = Japan, P2 = global) — matched stories bypass the gate
6. **Relevance gate** (only for unmatched stories):
   - Tier 1 source: always relevant → P3
   - Tier 2/3 source: requires ≥1 business keyword → P3, else DROPPED
7. Persist with priority ∈ {P1, P2, P3, DROPPED}
8. **P1**: summarize via Claude (Japanese) + email immediately (or print under `--dry-run`)
9. **P2/P3**: wait for the daily digest
10. **DROPPED**: never emailed; row exists only for dedup

## Architecture (one-line each)
- `agent.py` — orchestrates `fetch_cycle()` + `run_digest_now()`. Per-source recency override: claude_research items get a 72h gate; all other sources get 24h.
- `scheduler.py` — APScheduler `BlockingScheduler`: 60-min `IntervalTrigger` for fetch, 3-hour `IntervalTrigger` for P1 batch, `CronTrigger(7:00 Asia/Tokyo)` for digest.
- `digest.py` — runs `digest_eligible_stories()` query → summarize each → batch into one email → mark each emailed.
- `sources/rss.py` — feedparser-based; `trust_freshness` flag for RDF feeds without per-item pubdate.
- `sources/claude_research.py` — Claude Opus 4.7 + `web_search`, two-stage prompt (Phase 8). Cadence-gated 12h via `api_usage` table; surfaces COVERAGE_NOTES (`searches_run`, `tier1_aggregators_hit`, `fallback_used`, `gaps`) into telemetry columns.
- `store.py` — SQLite. `seen` columns: `url_hash PK, url, title, source, collected_at, published_at, priority, summary, emailed_at, dropped_reason, content_hash`. `api_usage` columns include `searches_run`, `tier1_aggregators_hit`, `fallback_used`. **`collected_at`** = UTC ISO8601 when our agent first saw the story. **`published_at`** = source's own pub date.
- `classifier.py` — word-boundary regex against `config/watchlists.yaml`. P1 wins over P2.
- `relevance.py` — word-boundary regex against `config/relevance.yaml`. Tier-1 sources skip the gate.
- `summarizer.py` — Anthropic SDK, default `claude-haiku-4-5`. **Returns Japanese `Summary(headline, bullets)`.**
- `mailer.py` — Gmail SMTP via STARTTLS. Subjects/bodies in Japanese. `send_p1()` and `send_digest(payload)`.

## Config files
- `config/config.yaml` — `scheduler`, `collection` (recency_hours, fetch_concurrency), paths.
- `config/feeds.yaml` — RSS feed list + Claude Research query config (model, cadence_hours, max_search_uses).
- `config/watchlists.yaml` — P1 (Japan-HQ) / P2 (global) entities with `aliases` and `exclude` lists. **Drives entity injection into the Claude Research prompt AND classification.**
- `config/relevance.yaml` — business keywords for the **post-fetch relevance gate** (Tier 2/3 sources only).
- `.env` — secrets. **Must contain a Gmail app password (16 chars) for SMTP_PASSWORD, not your regular Gmail password.**

## How searches work — two layers

Two independent layers run in each hourly cycle, in parallel via `asyncio.to_thread` + `Semaphore(10)`:

### Layer 1 — Native RSS (every cycle, free)
13 feeds defined in `feeds.yaml::native_rss`: 5 English trade press (Reinsurance News, Artemis, Insurance Journal × 2, Carrier Management), 3 Insurance Business regional editions, 2 press-release wires (PR Newswire, GlobeNewswire), 3 Japanese (Nikkei Asia, 東洋経済, 朝日新聞経済). Cycle time ~80-95s.

### Layer 2 — Claude Research (twice daily, paid)
One source: `Claude Research: insurance sector research (JP-focused)`. Cadence-gated to 12h via `api_usage` table — the hourly cycle invokes it but it self-skips if the last successful call was within 12 hours. Tier 1 (skips relevance gate).

Two-stage prompt in `src/news_agent/sources/claude_research.py`:
- **Stage 1 (discovery)**: Opus 4.7 + `web_search` (max 12 calls). Tier 1/2/3 priority taxonomy (T1=ratings/capital-markets/M&A/regulator/large-loss; T2=earnings/leadership/renewals/products; T3=excluded). Watchlist entities injected from `watchlists.yaml`. 24h strict window with 72h fallback if in-window <5. `published_confidence` label per item (`verified|inferred_high|inferred_low`); `inferred_low` is dropped. Mandatory `COVERAGE_NOTES:` block at end (parsed into `api_usage`).
- **Stage 2 (structuring)**: Haiku 4.5, no tools, ~30s. Converts bullet list to strict JSON with `category`, `tier`, `entity`, `summary_ja`, `other_sources`. Enforces title-prefix dedup.

The `apply_recency_filter` in `agent.py` runs once per source: 72h for `ClaudeResearchSource` (so fallback items survive), 24h for everything else. No source-side `when:` operator — that was the dropped Google News architecture.

**To expand coverage:**
- More watched companies → add to `watchlists.yaml` (auto-injected into Stage-1 entity list).
- New RSS feeds → add to `feeds.yaml::native_rss`.
- Stronger / different research scope → edit `DISCOVERY_PROMPT_TEMPLATE` in `sources/claude_research.py` (Tier definitions, search strategy).

## Source tiers
- **Tier 1** — Claude Research source. Gate is skipped because the prompt itself has its own quality filter (Tier 1/2/3 + confidence taxonomy).
- **Tier 2** — business-focused industry sites (Reinsurance News, Artemis). Gate applies but signal is strong.
- **Tier 3** — generalist insurance press (Insurance Journal, Carrier Management, Insurance Business, PR/Globe Newswire). Gate does heavy lifting.

## Conventions
- One source failure must not crash a cycle — wrap each `fetch()` in try/except, log, continue.
- All DB writes use `INSERT OR IGNORE` on `url_hash` (idempotent retries).
- `insert_if_new` returns `url_hash` on insert, `None` on duplicate.
- Emails are Japanese; Subjects too (`【ニュースエージェント P1】...`, `【ニュースエージェント】日次ダイジェスト ...`).
- Logs to `logs/agent.log` (rotating, JSON) + stdout (console).

## Known gotchas
- URL canonicalization is the dedup linchpin. `tests/test_canonicalize.py` first if duplicates appear.
- Source feeds use curly apostrophes (`’` U+2019); `_normalize_text` in `agent.py` rewrites these to ASCII `'` before classification so watchlist aliases match.
- Word-boundary regex (`\b<term>\b`) skips when the term is followed by a word char — e.g., `\bCEO\b` doesn't match "CEOs". Add common plurals to `relevance.yaml` if needed.
- **Gmail SMTP needs an app password** (not your Gmail login password). Issue at https://myaccount.google.com/apppasswords (requires 2FA).
- APScheduler timezone is `Asia/Tokyo` for the digest cron.
- `dropped_reason` column was added in Phase 2 via lazy `ALTER TABLE` — `Store.__init__` migrates existing DBs automatically.

## Nikkei integration (Phase 3b)
Nikkei is paywalled; we drive it via Playwright + browser-use + Claude.

**One-time setup:**
```bash
pip install -e .                       # picks up browser-use, playwright deps
.venv/bin/playwright install chromium  # downloads browser
```
Edit `.env`:
```
NIKKEI_USER=<your nikkei.com email>
NIKKEI_PASS=<your nikkei.com password>
BROWSER_USE_MODEL=claude-sonnet-4-5    # or claude-haiku-4-5 for cheaper
```
Then in `config/config.yaml`, flip the Nikkei source's `enabled: false` → `true`.

**Cost:** Sonnet 4.5 ~$15–50/month for Nikkei alone (5–15 LLM calls per 60-min cycle × 24 cycles/day). Switch to Haiku 4.5 to drop to ~$3–10/month. Note: Nikkei is currently disabled by default — the Claude Research layer largely supersedes it for JP-relevant headlines.

**Cookies:** persisted to `storage_state.json` (gitignored) after each successful run; subsequent cycles skip login when valid.

**Failure modes (all logged, none crash the cycle):**
- `nikkei.skipped` — creds missing, source effectively disabled.
- `nikkei.agent.error` with `auth_failed` — captcha/2FA blocked login. Disable source temporarily.
- `nikkei.no_structured_output` — extraction failed; check `errors` field in log.
- `nikkei.fetch.failed` — unexpected error (network, browser crash). Other sources still run.

**Debugging:** to see the browser, edit `src/news_agent/sources/nikkei.py` and pass `headless=False` to `NikkeiSource(...)` in `agent.py::build_sources`. Note: ToS is for personal subscriber use only; never redistribute extracted content.

## Web dashboard
A read-only Streamlit dashboard at `dashboard.py` shows everything in `seen.db` with filters and email-status indicators (✅ sent / 🚫 dropped / ⏳ P1 pending / 🕐 awaiting digest).

```bash
pip install -e ".[dashboard]"   # installs streamlit + pandas
.venv/bin/streamlit run dashboard.py
# opens http://localhost:8501
```

Filters in the sidebar: priority (P1/P2/P3/DROPPED), email status, source, title search, date range. The data refreshes every 30s automatically; click 🔄 Refresh for an immediate reload. Dashboard reads `seen.db` directly — it has no write access and won't interfere with the running agent.

## Phase status
- Phase 1 (MVP, no email): done.
- Phase 2 (relevance gate + Tier 2 sources + P1 email + dry-run): done.
- Phase 3 (digest + scheduler + Japanese summaries + `--digest-now`): done.
- Phase 3b (Nikkei via browser-use → pivoted to Google News RSS → dropped): done.
- Phase 4 (P1 → 3-hour batches with similarity dedup; daily digest = P1+P2; AWS EC2 deploy artifacts): done.
- Phase 5 (24h recency rule with `source.no_pubdate` / `story.too_old` logging): done.
- Phase 7 (cross-source content_hash dedup): done.
- Phase 8 (Claude Opus 4.7 + web_search replaces NewsAPI/Google News): done.
- Phase 8.3 (two-stage prompt rewrite: Tier 1/2/3 system, 72h fallback, COVERAGE_NOTES telemetry, bucket-XML retired): done.
- Phase 8.4 (prompt caching on Stage-1 system block; surface tier/category/gaps in dashboard): not started.

## AWS EC2 deployment
See `deploy/README.md` for full instructions. Bootstrap script at `deploy/setup-ec2.sh`. systemd units at `deploy/news-agent.service` and `deploy/news-dashboard.service`. Target: t4g.nano in ap-northeast-1, ~$4/mo. Browser-use deps are in the `[nikkei]` extra so the slim deploy installs only ~150MB.

Full plan: `~/.claude/plans/you-are-helping-me-harmonic-stallman.md`.
