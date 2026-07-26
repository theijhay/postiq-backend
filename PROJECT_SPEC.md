# PostIQ — Social Media Performance & Insights Platform
### Project Specification & Technical Architecture (v1)
**Prepared for:** handoff to Claude Code
**Author:** Isaac (Olawale Isaac John)
**Status:** Draft — ready for build
**Last updated:** 2026-07-25

> Working name only — rename freely. This doc is written to be dropped straight into a repo root as `PROJECT_SPEC.md` and read by Claude Code as the source of truth for scope, architecture, and build order.

---

## 1. Executive Summary

PostIQ is a SaaS analytics and decision-support tool for small businesses and niche communities running Meta (Facebook/Instagram) accounts. It connects to a business's Meta account via the official Graph API, ingests post and ad performance data, and turns raw metrics into **specific, actionable recommendations** — not just dashboards.

v1 targets Meta (Facebook Page + Instagram Business) only. Multi-platform support (TikTok, X) and the niche "mini recommendation platform" concept (Angle 3 — see §12) are explicitly deferred to v2+.

---

## 2. Problem Statement

Small business owners and community operators post on social media with no reliable feedback loop. They can see raw counts (likes, views) inside the native app, but they can't easily answer:

- Which content format actually drives engagement for *my* audience specifically?
- What time should I actually post?
- Is this ad spend working, or should I kill it?

Existing tools (Buffer, Hootsuite, Later, Sprout Social, Metricool) solve scheduling and surface-level reporting well, but are generic, agency-priced, and stop short of turning data into a decision. That gap — insight → recommendation — is the wedge for this product.

---

## 3. Competitive Landscape (for context, not to re-solve)

| Product | Strength | Gap PostIQ exploits |
|---|---|---|
| Buffer / Later | Scheduling UX | Shallow analytics, no recommendation layer |
| Hootsuite / Sprout Social | Enterprise features, unified inbox | Priced for agencies, generic across all verticals |
| Metricool | Decent free-tier analytics | Still descriptive, not prescriptive |

**Differentiation strategy:** go deep on decision-support (prescriptive, not just descriptive analytics) and eventually go deep on one vertical (fintech/crypto communities) rather than staying horizontal like the incumbents.

---

## 4. Goals (v1)

1. Business owner connects their Meta account in under 3 minutes (OAuth).
2. Within 24 hours of first sync, they see at least one specific, non-obvious recommendation (e.g. "posts with a face in the thumbnail get 2.1x the saves of product-only shots").
3. Weekly recurring insight delivered automatically (in-app + email) with zero manual digging required.
4. Paying conversion: 10 paying customers at $29–49/mo within 60 days of launch (validation target, not vanity).

## 5. Non-Goals (v1)

- **No scheduling/publishing.** Read-only insights only in v1 — publishing is a solved problem elsewhere and adds OAuth scope/complexity that isn't needed to prove the core value.
- **No multi-platform support.** Meta only (Facebook Page + linked Instagram Business account). TikTok/X are v2.
- **No automated ad-buying or campaign creation.** v1 reads ad performance (if ad account is connected) for reporting only; it does not create, pause, or modify campaigns.
- **No AI content generation** (caption writing, image generation). Out of scope — stay focused on the analytics wedge.
- **No white-label/agency multi-client management.** Single business per account in v1.

---

## 6. Target User

**Primary persona:** Solo operator or small team (1–5 people) running a business Instagram/Facebook presence — e-commerce, local service business, or niche community — who currently posts by gut feel and has no analyst on staff.

**Secondary persona (v2 signal):** P2P crypto / fintech community operators, given founder's domain expertise — informs vertical-specific insight templates later, not v1 architecture.

---

## 7. User Stories

- As a business owner, I want to connect my Instagram/Facebook account with one click so that I don't need to manually export anything.
- As a business owner, I want to see which of my last 30 posts performed best and why, so that I know what to repeat.
- As a business owner, I want to know the best time window to post based on *my own* audience's activity, not generic advice, so that I'm not guessing.
- As a business owner, I want a weekly email summary so that I don't have to remember to check a dashboard.
- As a business owner, I want to see if my boosted posts/ads are actually profitable, so that I stop wasting ad spend.
- As a business owner, I want to disconnect my account and delete my data at any time, so that I trust the platform with my business data.

---

## 8. Requirements

### P0 (Must-Have — MVP cannot ship without these)
- [ ] Meta OAuth connect flow (Facebook Login for Business, `pages_show_list`, `pages_read_engagement`, `instagram_basic`, `instagram_manage_insights`)
- [ ] Scheduled ingestion of Page + IG Business post-level metrics (impressions, reach, engagement, saves, likes, comments, shares)
- [ ] Historical storage of metrics (time-series, not just latest snapshot)
- [ ] Dashboard: top/bottom performing posts (last 30/90 days)
- [ ] Insight engine v1: best posting time-of-day/day-of-week (derived from the account's own historical engagement, not generic benchmarks)
- [ ] Insight engine v1: content-format comparison (image vs. carousel vs. video/reel, using whatever the account has actually posted)
- [ ] Weekly summary email
- [ ] Account disconnect + full data deletion endpoint (data trust is a conversion factor for this audience)
- [ ] Basic auth/account system + subscription billing gate

### P1 (Nice-to-Have — fast follow after launch)
- [ ] Ad account read-only integration (Meta Marketing API) — spend vs. engagement/conversion reporting
- [ ] Anomaly alerting (a post significantly under/over-performing vs. rolling baseline)
- [ ] Competitor benchmark (public page data comparison, rate-limit permitting)
- [ ] Exportable PDF/CSV report

### P2 (Future — architectural insurance only, not built now)
- [ ] Multi-platform ingestion (TikTok Business API, X API)
- [ ] Multi-account/agency mode
- [ ] Angle 3: niche vertical recommendation platform (see §12)

---

## 9. Technical Architecture

### 9.1 Stack (chosen to match founder's existing production stack — no new tooling risk)

| Layer | Choice | Why |
|---|---|---|
| API | **FastAPI** (Python 3.12) | Already primary stack; async support matters for API-bound ingestion workloads |
| DB | **PostgreSQL** | Time-series metrics + relational account data both fit well; use native partitioning for metrics table as volume grows |
| Cache | **Redis** | Cache Graph API responses (respect rate limits), session/token cache, rate-limit counters |
| Queue | **RabbitMQ** | Async ingestion jobs, scheduled polling, webhook processing — matches existing event-driven experience |
| Worker | **Celery** or lightweight custom consumer (`aio-pika`) on RabbitMQ | Scheduled + on-demand ingestion tasks |
| Frontend | **Next.js/React** (minimal v1) or server-rendered Jinja2 dashboard for true MVP | Keep frontend lean — this is a backend-heavy product; ship the ugliest UI that proves the insight is valuable before investing in polish |
| Billing | **Stripe** (international) and/or **Paystack** (NGN, given target market may include Nigerian SMBs) | Founder has direct prior experience integrating a payment/webhook-reconciliation flow (NOWPayments build) — same webhook + polling-fallback pattern applies directly here |
| Hosting | Digital Ocean (App Platform or Droplet + managed Postgres/Redis) | Matches existing ops experience |
| Auth | JWT-based session + OAuth token vault (encrypted at rest) | Meta long-lived tokens must be encrypted, refreshed before ~60-day expiry |

### 9.2 High-Level Architecture

```
                                   ┌─────────────────────┐
                                   │   Meta Graph API      │
                                   │ (Pages + IG Business) │
                                   └─────────▲────────────┘
                                             │ polling + webhooks
                                             │
┌────────────┐   OAuth connect   ┌───────────┴───────────┐
│   Client    │ ─────────────────▶  FastAPI  API Gateway  │
│ (dashboard) │ ◀───────────────── (auth, accounts, reports)│
└────────────┘     REST/JSON      └───────────┬───────────┘
                                               │ publishes ingestion jobs
                                               ▼
                                     ┌──────────────────┐
                                     │     RabbitMQ       │
                                     │  (job queue)        │
                                     └─────────┬──────────┘
                                               │ consumed by
                                               ▼
                                     ┌──────────────────┐
                                     │  Ingestion Workers  │
                                     │ (Celery / aio-pika) │
                                     └────────┬───────────┘
                                              │ writes metrics
                                              ▼
                          ┌──────────────────────────────────┐
                          │            PostgreSQL              │
                          │ accounts / posts / metrics / users │
                          └───────────────┬─────────────────┘
                                          │ read by
                                          ▼
                          ┌──────────────────────────────────┐
                          │         Insight Engine             │
                          │ (best-time, format comparison,     │
                          │  anomaly detection — scheduled job) │
                          └───────────────┬─────────────────┘
                                          │
                                          ▼
                                  ┌───────────────┐
                                  │     Redis       │
                                  │ (cache + rate-  │
                                  │  limit counters)│
                                  └───────────────┘
```

### 9.3 Data Model (PostgreSQL — v1 core tables)

```sql
-- Platform users of PostIQ (the business owner)
users (
  id UUID PK,
  email TEXT UNIQUE NOT NULL,
  password_hash TEXT,
  created_at TIMESTAMPTZ,
  subscription_status TEXT  -- trialing | active | past_due | canceled
);

-- A connected Meta business account
connected_accounts (
  id UUID PK,
  user_id UUID FK -> users.id,
  platform TEXT DEFAULT 'meta',
  fb_page_id TEXT,
  ig_business_id TEXT,
  access_token_encrypted TEXT,      -- long-lived token, encrypted at rest
  token_expires_at TIMESTAMPTZ,
  connected_at TIMESTAMPTZ,
  status TEXT                       -- active | token_expired | disconnected
);

-- Individual posts pulled from the API
posts (
  id UUID PK,
  connected_account_id UUID FK,
  platform_post_id TEXT,
  post_type TEXT,          -- image | carousel | video | reel
  caption TEXT,
  posted_at TIMESTAMPTZ,
  permalink TEXT,
  UNIQUE (connected_account_id, platform_post_id)
);

-- Time-series metric snapshots per post (append-only; consider partitioning by month at scale)
post_metrics_snapshots (
  id UUID PK,
  post_id UUID FK,
  captured_at TIMESTAMPTZ,
  impressions INT,
  reach INT,
  likes INT,
  comments INT,
  shares INT,
  saves INT,
  engagement_rate NUMERIC
);

-- Precomputed insight results (so dashboard reads are cheap)
insights (
  id UUID PK,
  connected_account_id UUID FK,
  insight_type TEXT,        -- best_time | format_comparison | anomaly
  payload JSONB,
  generated_at TIMESTAMPTZ
);
```

### 9.4 Core API Endpoints (v1)

```
POST   /auth/register
POST   /auth/login
GET    /auth/meta/connect              -> redirects to Meta OAuth
GET    /auth/meta/callback             -> exchanges code, stores long-lived token

GET    /accounts                        -> list connected accounts for current user
DELETE /accounts/{id}                   -> disconnect + purge data

GET    /accounts/{id}/posts             -> paginated post list with latest metrics
GET    /accounts/{id}/insights/best-time
GET    /accounts/{id}/insights/format-comparison
GET    /accounts/{id}/insights/summary  -> weekly digest payload (also used to render email)

POST   /billing/checkout                -> Stripe/Paystack session
POST   /billing/webhook                 -> payment provider webhook (signature-verified)
```

### 9.5 Ingestion & Rate-Limit Handling

- Meta Graph API standard tier: ~200 calls/hour/user (varies by app usage tier — verify current limits at build time, they change).
- Ingestion runs as a scheduled job (e.g. every 4–6 hours per account) rather than per-request live calls, to stay well under rate limits and because engagement metrics settle over the first 24–48h anyway.
- Cache raw Graph API responses in Redis with a short TTL as a safety net against duplicate calls within a run.
- Use exponential backoff + a dead-letter queue in RabbitMQ for failed ingestion jobs (token expired, rate-limited, page unpublished, etc.) — surface `token_expired` status to the user rather than silently failing.

### 9.6 Security & Compliance Notes

- Encrypt access tokens at rest (e.g. `pgcrypto` or application-level AES-GCM with a key from a secrets manager, not hardcoded).
- Meta App Review is required before requesting `instagram_manage_insights`/`pages_read_engagement` in production for other people's accounts — budget time for this in the timeline; use test accounts in development mode in the meantime.
- Support full account disconnect + data purge (not just token revoke) — required both for user trust and to avoid holding data you no longer have a lawful basis to retain.

---

## 10. Success Metrics

**Leading (days–weeks):**
- OAuth connect completion rate (started → completed)
- % of connected accounts with at least 10 posts ingested (enough data for insights to be non-trivial)
- Weekly email open rate

**Lagging (weeks–months):**
- Trial → paid conversion rate
- Monthly churn rate
- Net new paying customers/month

---

## 11. Build Phases

**Phase 0 — Setup (few days)**
- Repo scaffold, FastAPI project structure, Postgres + Redis + RabbitMQ locally (docker-compose), Meta developer app created (development mode)

**Phase 1 — Auth & Ingestion (core)**
- User auth, Meta OAuth connect flow, token storage/encryption
- Ingestion worker: pull Page + IG posts and metrics on schedule
- Data model migrations

**Phase 2 — Insight Engine & Dashboard**
- Best-time-to-post computation
- Format comparison computation
- Minimal dashboard (even a server-rendered page is fine for MVP validation)

**Phase 3 — Billing & Retention Loop**
- Stripe/Paystack checkout + webhook handling
- Weekly summary email job

**Phase 4 — P1 additions**
- Ad account read integration
- Anomaly alerts
- PDF/CSV export

**Phase 5+ — v2 (see below, separate spec when reached)**

---

## 12. v2 — Angle 3: Niche Recommendation Platform (Deferred, Documented for Architectural Awareness Only)

Not built in v1. Documented here so v1 architecture doesn't accidentally foreclose it.

**Concept:** Rather than helping businesses analyze their *existing* Meta presence, build a standalone niche platform (e.g., for P2P crypto traders) with PostIQ's own content feed and ranking engine — monetized via subscription/premium placement/lead-gen rather than analytics-as-a-service.

**Why deferred:** Different product entirely (own feed, own ranking model, own network effects/cold-start problem) rather than an extension of the v1 codebase. Validate v1's core insight-engine value and willingness-to-pay first; v2 is a separate build with its own spec, informed by whichever insight types prove most valuable in v1.

**Architectural note for v1 build:** keep the insight-engine logic (post scoring, engagement-rate calculation, time-series aggregation) in a module that's reasonably decoupled from the Meta-ingestion-specific code, since a v2 recommendation engine would reuse the scoring/ranking logic against a different data source (an internal content feed instead of Graph API data).

---

## 13. Open Questions

- [ ] **Engineering:** Meta App Review timeline — how long will approval realistically take for the required permission set? (Blocking for production launch, not for dev-mode build.)
- [ ] **Business:** Stripe or Paystack as primary billing, or both from day one?
- [ ] **Product:** Is a server-rendered MVP dashboard sufficient for the first 10 validation customers, or does a polished frontend matter for this buyer from day one?
- [ ] **Product:** Confirm pricing — $29 vs $49/mo — via direct conversations with first prospects before building billing tiers.

---

## 14. Notes for Claude Code

- Follow build phases in order (§11); do not start Phase 2 work before Phase 1 ingestion is proven against a real (development-mode) Meta test account.
- Keep the insight-engine module decoupled per §12's architectural note.
- Ask before introducing new infrastructure dependencies not listed in §9.1 — the stack is deliberately chosen to match existing production experience.
- Treat §5 (Non-Goals) as a hard boundary unless the user explicitly re-scopes.
