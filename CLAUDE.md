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
- `agent.py` — orchestrates `fetch_cycle()` + `run_digest_now()`. Builds runtime components from `Secrets`.
- `scheduler.py` — APScheduler `BlockingScheduler`: 30-min `IntervalTrigger` for fetch, `CronTrigger(7:00 Asia/Tokyo)` for digest.
- `digest.py` — runs `digest_eligible_stories()` query → summarize each → batch into one email → mark each emailed.
- `sources/` — RSS adapter now (HTML in Phase 3b for carrier IR pages).
- `store.py` — SQLite. Columns: `url_hash PK, url, title, source, collected_at, published_at, priority, summary, emailed_at, dropped_reason`. **`collected_at`** = when our agent first saw the story (UTC ISO8601). **`published_at`** = the article's own pub date from the source feed.
- `classifier.py` — word-boundary regex against `config/watchlists.yaml`. P1 wins over P2.
- `relevance.py` — word-boundary regex against `config/relevance.yaml`. Tier-1 sources skip the gate.
- `summarizer.py` — Anthropic SDK, default `claude-haiku-4-5`. **Returns Japanese `Summary(headline, bullets)`.**
- `mailer.py` — Gmail SMTP via STARTTLS. Subjects/bodies in Japanese. `send_p1()` and `send_digest(payload)`.

## Config files
- `config/config.yaml` — static sources, `scheduler`, `collection` (recency_hours, fetch_concurrency), paths.
- `config/watchlists.yaml` — P1/P2 entities with `aliases` and `exclude` lists. **Drives query generation AND classification.**
- `config/query_buckets.yaml` — thematic keyword buckets used to **generate** Google News queries (Phase 6).
- `config/relevance.yaml` — business keywords for the **post-fetch relevance gate**. Independent from query_buckets.
- `.env` — secrets only. **Must contain a Gmail app password (16 chars) for SMTP_PASSWORD, not your regular Gmail password.**

## How searches work — two-layer query design

**Searches are NOT AI-generated.** Queries are built deterministically at runtime in two complementary layers:

### Layer 1 — Entity × Bucket (Phase 6)
For each entity in `watchlists.yaml` (currently 54: 20 P1 + 34 P2), for each bucket in `query_buckets.yaml` (currently 8): build one Google News query `(entity aliases) (bucket keywords) when:24h`. Total = **432 queries per cycle**. Catches news where a specific watched company is named.

### Layer 2 — Topic queries (Phase 6.y)
Broad sector/regulatory/market-trend queries from `topic_queries.yaml` (currently 13). No entity binding — catches industry-level news where no specific company is mentioned, plus foreign carriers not in the watchlist (e.g. Indian, Florida specialty). Each gets `when:24h` appended.

### Plus
- 3 static English RSS feeds (Insurance Journal, Reinsurance News, Artemis).
- 1 disabled Nikkei browser-use source (kept for future re-enablement).
- All sources fetched in parallel via `asyncio.to_thread` + `Semaphore(10)`. Cycle time ~80-95s.
- **Total: ~448 sources per cycle.**
- Claude is called only at **summary time** (P1 batch + daily digest), never at fetch time.

**To expand coverage:**
- More watched companies → add to `watchlists.yaml` (each entity = 8 entity×bucket queries automatically).
- More themes → add to `query_buckets.yaml` (each bucket = 54 entity×bucket queries automatically).
- Industry/sector/regulation/market-trend topics → add to `topic_queries.yaml` (each = 1 broad query).
- One-off RSS feeds → add to `config.yaml::sources`.

The recency filter applies twice: once at source via Google's `when:Nh` operator (`config.yaml::collection.recency_hours`), once defensively in `agent.py::apply_recency_filter` for any stragglers and items missing pubdate.

## Source tiers
- **Tier 1** — carrier IR / press release feeds. Gate is skipped. Currently none in RSS — Phase 3b adds HTML scraping (Tokio Marine, Sompo, MS&AD, etc.).
- **Tier 2** — business-focused industry sites (Reinsurance News, Artemis). Gate applies but signal is strong.
- **Tier 3** — generalist insurance press (Insurance Journal). Gate does heavy lifting.

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

**Cost:** Sonnet 4.5 ~$30–100/month for Nikkei alone (5–15 LLM calls per 30-min cycle × 48 cycles/day). Switch to Haiku 4.5 to drop to ~$5–20/month.

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
- Phase 3b (Nikkei via browser-use → pivoted to Google News RSS): done.
- Phase 3b+ (expanded JP watchlists + bilingual queries + JP keywords): done.
- Phase 4 (P1 → 3-hour batches with similarity dedup; daily digest = P1+P2; AWS EC2 deploy artifacts): done.
- Phase 4b (Japanese carrier IR HTML scraping, prompt caching, batched digest summaries via single-call): not started.

## AWS EC2 deployment
See `deploy/README.md` for full instructions. Bootstrap script at `deploy/setup-ec2.sh`. systemd units at `deploy/news-agent.service` and `deploy/news-dashboard.service`. Target: t4g.nano in ap-northeast-1, ~$4/mo. Browser-use deps are in the `[nikkei]` extra so the slim deploy installs only ~150MB.

Full plan: `~/.claude/plans/you-are-helping-me-harmonic-stallman.md`.
