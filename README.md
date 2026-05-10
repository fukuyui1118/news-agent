# News Agent

A long-running agent that monitors insurance and reinsurance news, classifies each headline into P1 / P2 / P3, and emails a curated Japanese-language digest to a Tokyo investment desk. Runs on a t4g.nano EC2 instance plus ~$72-114/month Anthropic spend.

## What it does, in one paragraph

Twice a day at **07:00 and 19:00 JST**, the agent runs one full pipeline tick: fetches news from RSS feeds (13 native + 14 user-curated Inoreader keyword tags), asks **Claude Opus 4.7 + web_search** to research the past 12 hours of insurance-sector news via a structured two-stage prompt, then makes **one Opus call to classify** every fresh item into P1 (Japan / regulatory / financial), P2 (global insurer business news), or P3 (everything else — sports sponsorships, naming-rights events, noise). Items are persisted with the AI-assigned priority. A **second Opus call** drafts a curated Japanese-language digest from the last 12 hours of P1+P2 items, ranks Tier-1 events first, deduplicates same-event clusters, and emails it. P3 items go to the dashboard only.

---

## Search strategy

Three layers run in parallel each tick:

### Layer 1 — Native RSS (every tick, free)

13 feeds defined under `native_rss` in [`config/feeds.yaml`](config/feeds.yaml). Three groups:

| Group | Feeds |
|---|---|
| Core English trade press | Reinsurance News, Artemis, Insurance Journal (International + National), Carrier Management |
| Insurance Business regional | America, UK, Asia |
| Press-release wires (noisy, AI classifier filters) | PR Newswire, GlobeNewswire |
| Japanese press | Nikkei Asia, 東洋経済オンライン, 朝日新聞 (経済) |

### Layer 2 — Inoreader REST API (every tick, ~$7.50/mo Pro plan)

14 user-curated keyword tags, also listed under `native_rss` in [`config/feeds.yaml`](config/feeds.yaml) but identified by URL prefix (`https://www.inoreader.com/stream/user/...`). Routed through [`InoreaderSource`](src/news_agent/sources/inoreader.py) which calls Inoreader's authenticated REST API and uses the article's **true publish time** (the public-RSS export only carries Inoreader's ingestion timestamp). 14 tags × 50 items/tag = up to **700 items per tick**, with the recency filter typically dropping ~85% as >24h old.

### Layer 3 — Claude Research (twice daily, paid)

One source: `Claude Research: insurance sector research (JP-focused)`. Cadence-gated to 12h via the `api_usage` table — the scheduled tick invokes it but it self-skips if the last successful call was within 12 hours. Tier 1 source label (preserved for telemetry; the AI classifier judges per-item now).

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

## AI agent flow (per scheduled tick)

```
   Cron at 07:00 / 19:00 JST
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

## Prompts in use (verbatim from source)

Four prompts drive the AI portion of the pipeline. All are sent to Claude Opus 4.7 except Stage 2 (Haiku 4.5). `{...}` placeholders are filled at runtime.

### 1. Claude Research — Stage 1: Discovery (Opus 4.7 + `web_search`)

[`src/news_agent/sources/claude_research.py::DISCOVERY_PROMPT_TEMPLATE`](src/news_agent/sources/claude_research.py)

```
あなたは保険・再保険セクターを担当するシニア金融ニュース調査員です。
東京の機関投資家デスク向けに、トレード判断に資するヘッドラインを収集します。

# 時間範囲（厳守）
基準時刻（JST）: {now_jst}
収集対象: {since_iso} 〜 {until_iso}

このウインドウ外の記事は最終出力から除外します。ただし、ウインドウ内の
記事が5件未満となった場合に限り、直近72時間まで遡って補完してよい。
補完した記事には要約末尾に「[T-{時間}h]」を付記すること。

# 優先度（検索予算の配分指針）
Tier 1（必ず探す・見つけたら必ず収録）:
  - 格付けアクション（AM Best / S&P / Moody's / Fitch / R&I / JCR）
  - 資本市場取引（社債・劣後債・ハイブリッド・CATボンド・サイドカー・IPO）
  - M&A、戦略出資、政策保有株売却
  - 金融庁・EIOPA・NAIC・PRA・BMA・IAIS の規制アクションや行政処分
  - 大型損害事象、災害発生に伴う保険金支払見込み

Tier 2（余力があれば収録）:
  - 決算・業績見通し修正
  - 経営体制変更、CEO/CFO 交代
  - 再保険更改の条件
  - 新商品の開示

Tier 3（除外）:
  - 支店開設、スポンサー、CSR、表彰、セミナー告知

# 監視対象エンティティ
日本Tier 1: {p1_entities}
グローバル: {global_entities}

# 検索戦略
合計 8〜15 クエリを目安に、以下の順序で実行:

1. アグリゲータ・スイープ（最優先・少ないクエリで広範カバー）
   - site:release.tdnet.info （TDnet 適時開示、過去24h）
   - site:fsa.go.jp/news 過去30日
   - artemis.bm/news/ と reinsurancene.ws の最新ページを web_fetch
   - 保険毎日新聞、Nikkei 保険業界トップページ

2. 格付け機関の直接照会（Tier 1 必須）
   - site:ambest.com 直近の press release
   - site:spglobal.com/ratings、site:moodys.com、site:fitchratings.com

3. エンティティ別クエリ（スイープで未検出の主要社のみ、3社ずつ束ねる）
   例: "東京海上 OR MS&AD OR SOMPO 社債 OR 格付 2026"

4. 日付フィルタを必ず付与
   - Google系クエリには `after:{since_date}` を付ける
   - 日本語クエリには年月（例: "2026年5月"）を含める

# 重複の扱い
同一イベントが複数媒体に出ている場合は、最も一次情報に近い1件を残し、
他媒体は要約末尾に「他: Reuters, Bloomberg」のように列挙してよい。

# 出力形式（フリーテキスト・箇条書き）
各ヘッドラインを以下のフォーマットで報告してください:

- タイトル: <原語のままのタイトル>
  URL: <絶対URL>
  媒体: <Reuters | Nikkei | TDnet | AM Best | ...>
  公開日時: <ISO 8601 タイムゾーン付き>
  公開日時の確度: <verified | inferred_high | inferred_low>
    verified=ページ上に明示的タイムスタンプ
    inferred_high=日付タグ＋通信社配信時刻で裏付け
    inferred_low=「2 days ago」等の相対表現のみ
  カテゴリ: <rating | capital_markets | m_and_a | regulatory |
            large_loss | earnings | leadership | reinsurance |
            product | other>
  優先度: <T1 | T2>
  対象企業: <主たる関連エンティティ>
  要約: <1〜2文の日本語要約>
  他媒体: <あれば列挙、なければ省略>

# 出力末尾に必ず以下を付記
COVERAGE_NOTES:
  searches_run: <実際の検索回数>
  tier1_aggregators_hit: <TDnet/FSA/Artemis/AM Best のうちアクセスしたもの>
  fallback_used: <true/false ウインドウ拡張の有無>
  gaps: <検索したが該当なしだったTier 1領域があれば記載>

# 除外条件
- ウインドウ外の記事（fallback条件を満たす場合を除く）
- 公開日時の確度が inferred_low の記事
- Tier 3 該当
- 同一イベントの重複（一次情報1件に統合）
最大 {max_headlines} 件。
```

### 2. Claude Research — Stage 2: Structuring (Haiku 4.5, no tools)

[`src/news_agent/sources/claude_research.py::STRUCTURING_PROMPT_TEMPLATE`](src/news_agent/sources/claude_research.py)

```
以下のヘッドライン一覧を、指定のJSONスキーマに正確に変換してください。
出力はJSONのみ。前置き、説明文、Markdownフェンス（```）は一切不要。

スキーマ:
{
  "as_of_jst": "{now_jst}",
  "fallback_used": <bool>,
  "searches_run": <int>,
  "headlines": [
    {
      "title": "...",
      "url": "https://...",
      "source": "...",
      "published_at": "ISO 8601 タイムゾーン付き",
      "published_confidence": "verified | inferred_high",
      "category": "rating | capital_markets | m_and_a | regulatory | large_loss | earnings | leadership | reinsurance | product | other",
      "tier": "T1 | T2",
      "entity": "...",
      "summary_ja": "...",
      "other_sources": ["..."] | null
    }
  ],
  "gaps": "..." | null
}

# ルール
- published_confidence が "inferred_low" の項目は出力に含めない
- published_at が基準時刻 {now_jst} より24h以上前で、fallback_used=false
  の場合はその項目を除外
- titleの先頭40文字が酷似する項目（Jaccard類似度の目安）は最初の1件だけ残し、
  残りはother_sourcesに媒体名のみマージ
- カテゴリ・tier が不明な項目は category="other", tier="T2"

ヘッドライン一覧:
---
{discovery_text}
---

COVERAGE_NOTES:
---
{coverage_notes_text}
---
```

### 3. AI Classifier (Opus 4.7, batched per tick)

[`src/news_agent/ai_classifier.py::CLASSIFIER_PROMPT_TEMPLATE`](src/news_agent/ai_classifier.py)

Receives every post-recency item (typically ~200-300 per tick) numbered with `[idx]`. Returns a JSON object with two index arrays — anything not listed implicitly becomes P3.

```
あなたは保険・再保険セクターを担当するシニア・ニュース・エディターです。
東京の機関投資家デスク向けに、以下のヘッドラインを P1 / P2 / それ以外 (=P3)
に分類してください。

# 分類基準
- P1: 日本の保険会社（{p1_entities}）、または金融規制・財務に関する重要ニュース。
       格付けアクション、資本市場取引（社債・劣後債・ハイブリッド・CATボンド・IPO）、
       M&A、戦略出資・政策保有株売却、金融庁/EIOPA/NAIC/PRA/IAIS/BMA等の規制動向、
       大型損害事象。
- P2: グローバル保険会社（{global_entities}）の事業ニュース。決算、経営体制変更、
       商品、再保険更改条件など。Japanの規制対象でないもの。
- P3 (=出力に含めない): 上記以外。スポーツスポンサー、コンサート会場の名称イベント、
       CSR、表彰、無関係な広告、求人、ノイズ。

# ヘッドライン一覧（インデックス付き）
{rows_text}

# 出力（JSONのみ。前置き・コードフェンス無し）
{
  "p1": [<該当するインデックス>],
  "p2": [<該当するインデックス>]
}

P3 は出力に含めません。p1 と p2 のどちらにも入らないインデックスが
自動的に P3 と判定されます。
```

`{p1_entities}` and `{global_entities}` are joined canonical names from [`config/watchlists.yaml`](config/watchlists.yaml). On parse/API failure, classifier returns `{}` and every item becomes P3 (safer than over-promoting).

### 4. AI Email Composer (Opus 4.7, per digest)

[`src/news_agent/ai_email.py::EMAIL_PROMPT_TEMPLATE`](src/news_agent/ai_email.py)

Receives the last 12h of P1+P2 rows from `digest_eligible_stories`. Returns ranked DigestEntry objects ready for the mailer. Hard-capped at 15 entries.

```
あなたは保険・再保険セクター担当のシニア・エディターです。
過去12時間で収集された P1（日本/規制/金融）と P2（グローバル保険）の
ヘッドラインを、東京の機関投資家デスク向けダイジェストに編集してください。

# タスク
1. 同一イベントの重複（複数媒体の同記事）は1件に統合し、媒体名を集約
2. P1 を上位、P2 を下位に並べる
3. 各イベントに30文字以内の日本語ヘッドラインと、3〜5項目の箇条書き要約を作成
4. 重要度の低い P2 は除外可（最大 {max_entries} 件まで）
5. 宣伝的・誇張的な表現を避け、財務・戦略事実（金額、当事者、日付）を優先

# 入力ヘッドライン（過去12時間、P1優先）
{rows_text}

# 出力（JSONのみ。前置き・コードフェンス無し）
{
  "entries": [
    {
      "priority": "P1 | P2",
      "headline_ja": "...",
      "original_title": "...",
      "source": "...",
      "url": "https://...",
      "summary_bullets": ["...", "...", "..."]
    }
  ]
}
```

On parse/API failure, falls back to per-row Haiku 4.5 `Summarizer.summarize()` so the digest never silent-empties.

### Per-call dump

Every call to the AI Classifier and AI Email Composer is persisted to `logs/ai_classifier/<ts>.json` and `logs/ai_email/<ts>.json` respectively (best-effort, errors logged and swallowed). Stage-1 + Stage-2 dumps land in `logs/claude_research/`. Useful for prompt-drift debugging and post-hoc cost/quality analysis.

---

## Email setup

### What gets emailed

| Priority | Email path | Cadence |
|---|---|---|
| **P1** (Japan / regulatory / financial) | Included in the curated digest, ranked at the top. | Twice daily, 07:00 and 19:00 JST |
| **P2** (Global insurer business news) | Included in the curated digest, ranked below P1. | Twice daily, 07:00 and 19:00 JST |
| **P3** (Everything else: sports sponsorships, naming-rights events, noise) | Never emailed. Visible only in the dashboard. | — |

Each digest is produced by **one Claude Opus 4.7 call** ([`ai_email.compose_email`](src/news_agent/ai_email.py)) that takes the last 12 hours of P1+P2 items and returns a ranked, deduplicated list (≤15 entries) with one-line Japanese headlines and 3-5-bullet Japanese summaries. Subject: `【ニュースエージェント】ダイジェスト MM/DD HH:00`. Same-event items from multiple outlets are collapsed to one entry. If the Opus call fails to parse, falls back to per-row Haiku summarize so the digest never silent-empties.

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

- Sidebar filters: priority (P1/P2/P3), source, title search, date range.
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
| **Full pipeline tick** | **07:00 and 19:00 JST** | `CronTrigger(hour='7,19', minute=0, timezone='Asia/Tokyo')` | Fetch all RSS + Inoreader API + Claude Research (if cadence open) → AI classify (Opus) → AI compose email (Opus) → send digest. ~3 min wall time when Claude Research fires; ~10s when cadence-skipped. |
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
- The shared `InoreaderClient` in `inoreader_oauth.py` caches the access token (Inoreader issues 24h TTLs in practice) and refreshes on 401. Rotated refresh tokens are persisted back to `.env` automatically.

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
| Anthropic — Claude Research Stage 1 (Opus 4.7 + `web_search`, 2 calls/day) | ~$30–60 |
| Anthropic — Claude Research Stage 2 (Haiku 4.5, 2 calls/day) | ~$0.30 |
| Anthropic — AI Classifier (Opus 4.7 batched, 2 calls/day) | ~$12 |
| Anthropic — AI Email Composer (Opus 4.7, 2 calls/day) | ~$30 |
| Inoreader Pro subscription (separate from Anthropic) | $7.50/mo (annual billing) |
| Gmail SMTP | free |
| **Total** | **~$83–114/month** |

Cost-down levers if needed:
- **AI Classifier → Haiku 4.5**: saves ~$11/mo. The yes/no-per-item judgment doesn't really need Opus reasoning. Lowest-risk lever.
- **Disable Claude Research** (`enabled: false` or remove from `feeds.yaml`): saves $30–60/mo, falls back to RSS + Inoreader-only. You lose Claude's web_search-driven discovery of items the keyword feeds don't surface.
- **Drop Inoreader Pro to free tier**: not viable — the API requires Pro for the public-RSS-from-tag export. Would have to switch back to the public-RSS path with the recency-leakage caveat (588 stale items per tick we observed before the API switch).
