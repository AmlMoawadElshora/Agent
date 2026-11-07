# WhatsApp Real-Estate Outreach & Qualification Agent

A production-ready, fully automated WhatsApp agent for the US real-estate market.
It imports prospect phone numbers, opens the conversation, qualifies each lead over
several natural turns, and pushes a structured lead card to the acquisitions
manager the moment all three qualification slots are known.

```
                 ┌──────────────────────────────────────────────────────┐
  prospect list  │                  FastAPI service                     │
  (numbers/CSV) ─┼─▶ /api/campaigns/launch ──▶ WhatsApp (Meta Cloud API)   │
                 │            ▲                        │                │
                 │            │ inbound                │ opener         │
                 │  /webhooks/whatsapp                 ▼                │
                 │            │                 ┌─────────────┐         │
                 │            ▼                 │  prospect   │         │
                 │   ConversationEngine         └─────────────┘         │
                 │   (LangGraph state machine)          │               │
                 │   extract → guardrail → merge        │ replies       │
                 │        → qualify → compose ◀─────────┘               │
                 │            │                                         │
                 │            ├── price? condition? sq ft? ── all 3 ──┐ │
                 │            │                                       ▼ │
                 │   PostgreSQL / Redis                 handoff card   │
                 │   (lead state + transcript)          to 01020645167 │
                 └──────────────────────────────────────────────────────┘
```

---

## Table of contents

1. [What it does](#what-it-does)
2. [Architecture](#architecture)
3. [Quick start (5 minutes, no Meta account needed)](#quick-start)
4. [Meta WhatsApp Cloud API setup](#meta-whatsapp-cloud-api-setup)
5. [Running an outreach campaign](#running-an-outreach-campaign)
6. [Admin API](#admin-api)
7. [Conversation design](#conversation-design)
8. [Configuration reference](#configuration-reference)
9. [Deployment](#deployment)
10. [Testing](#testing)
11. [Operations & troubleshooting](#operations--troubleshooting)
12. [Compliance notes](#compliance-notes)

---

## What it does

| Capability | Implementation |
|---|---|
| Ingest prospect numbers | `POST /api/leads`, `POST /api/leads/bulk`, `POST /api/campaigns/launch`, `python -m scripts.campaign` (CSV or `--numbers`) |
| Send the opening WhatsApp message | `app/services/campaign.py` → Meta Graph API, using an approved message template |
| Two-way, human-like English conversation | `app/agent/graph.py` state machine + `app/agent/prompts.py` persona (optional LLM rewrite) |
| Collect 3 mandatory slots, one question at a time | `price` → `condition` → `size`, tracked per lead in `leads.questions_asked` |
| Never repeat a question already answered | `collected` slots drive `next_slot`; out-of-order answers are honoured |
| Immediate handoff on qualification | `app/services/handoff.py` sends the summary card to `ADMIN_WHATSAPP_NUMBER` (default `01020645167`) |
| Persistent lead state | PostgreSQL (SQLite for local dev) — `NEW → CONTACTED → IN_PROGRESS → QUALIFIED → HANDED_OFF`, plus `OPTED_OUT` / `FAILED` |
| Audit trail | Full transcript in `messages`, every handoff attempt in `handoffs` |
| Robustness | `X-Hub-Signature-256` validation, `wamid` idempotency, per-lead locks, retries, guardrails, dry-run mode |

---

## Architecture

```
app/
├── main.py                  FastAPI app factory, lifespan, routers
├── config.py                Pydantic settings (env driven, cached)
├── logging_config.py        key=value console logging
├── domain/
│   ├── enums.py             LeadStatus, Slot, PropertyCondition, MessageKind...
│   ├── phones.py            E.164 normalisation (handles 01020645167 style input)
│   └── schemas.py           Pydantic contracts (ExtractedSlots, SummaryCard, API models)
├── db/
│   ├── base.py              engine/session, init_db, in-memory SQLite support
│   ├── models.py            Lead, Message, Handoff
│   └── repository.py        all SQL in one place
├── agent/
│   ├── prompts.py           persona, system prompt, every prospect-facing string
│   ├── extraction.py        deterministic slot extraction (regex + vocabulary)
│   ├── guardrails.py        opt-out / legal / negotiation / human-request detection
│   ├── llm.py               optional OpenAI-compatible client (extraction + rewording)
│   ├── responder.py         deterministic reply composition (ack + one question)
│   ├── graph.py             LangGraph StateGraph + dependency-free runner
│   └── engine.py            orchestrator: lock → dedupe → graph → persist → handoff → deliver
├── services/
│   ├── whatsapp.py          sender protocol, Meta Cloud API client, dry-run sender
│   ├── handoff.py           summary card + admin delivery + retry semantics
│   ├── campaign.py          outbound opener, rate limiting, skip rules
│   └── locks.py             Redis / in-process per-lead locks
├── api/
│   ├── webhook.py           Meta webhook (verify handshake + JSON → engine)
│   ├── leads.py             admin CRUD, transcript, manual message, handoff, campaign
│   ├── health.py            /health, /ready, /api/stats
│   └── deps.py              X-API-Key auth
scripts/
├── campaign.py              CLI campaign runner (numbers or CSV)
└── init_db.py               schema creation
sql/schema.sql               reference PostgreSQL DDL
tests/                       161 offline tests (no Graph API, no LLM, no network)
```

### The turn lifecycle

1. Meta POSTs a JSON event batch to `/webhooks/whatsapp`.
2. The `X-Hub-Signature-256` header is verified (HMAC-SHA256 of the raw body).
3. `ConversationEngine.handle_inbound` takes a per-lead lock and ignores duplicate `wamid`s.
4. The LangGraph machine runs five pure nodes: `extract → guardrail → merge → qualify → compose`.
5. Newly heard slots are persisted; the question counter for the next slot is incremented.
6. If all three slots exist, `perform_handoff` sends the card to the admin, records it, and
   flips the lead to `HANDED_OFF`. A failed card leaves the lead `QUALIFIED` so the next
   inbound turn retries it automatically.
7. The reply is sent over the Graph API and the webhook answers `200 OK` (Meta requires that).

Everything happens inside one transaction — a crash never leaves a half-updated lead.

### Why the agent is deterministic first

The regex extractor and the template responder are the source of truth. An LLM
(`LLM_ENABLED=true`) can only *fill extraction gaps* and *reword the reply we already
decided*. It receives the exact next question, so it cannot skip a slot, ask two
questions, promise a price, or give legal advice — and any timeout or malformed
response silently falls back to the deterministic text. The whole suite runs with
the LLM off.

| Immediate handoff on qualification | `app/services/handoff.py` sends the summary card to `ADMIN_WHATSAPP_NUMBER` (default `01020645167`) |
| Persistent lead state | PostgreSQL (SQLite for local dev) — `NEW → CONTACTED → IN_PROGRESS → QUALIFIED → HANDED_OFF`, plus `OPTED_OUT` / `FAILED` |
| Audit trail | Full transcript in `messages`, every handoff attempt in `handoffs` |

---

## Quick start

### 1. Install

```bash
cd /workspace
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # defaults are safe: DRY_RUN=true, SQLite
```

Requirements: Python 3.11+. PostgreSQL 16 and Redis 7 are only needed in
production (or via `docker compose`).

### 2. Create the schema

```bash
python -m scripts.init_db
# Schema ready at ./data/agent.db
```

Tables are also created automatically on startup when `AUTO_CREATE_TABLES=true`.

### 3. Run the API

```bash
uvicorn app.main:app --reload --port 8000
```

* Interactive docs: <http://localhost:8000/docs>
* Health: <http://localhost:8000/health>

### 4. Import prospects and send the opener (dry-run)

```bash
python -m scripts.campaign --numbers 01020645167,01131043304,01032547715
```

```
Campaign complete: total=3 sent=3 skipped=0 failed=0 dry_run=True

  +11020645167       SENT
  +11131043304       SENT
  +11032547715       SENT
```

Dry-run messages are logged and stored in the transcript (with a `DRYRUN-` SID)
but never transmitted, so you can demo the whole flow without a Meta account.

### 5. Simulate a conversation

Meta posts JSON, so a signed JSON request is what the webhook expects. With
`META_APP_SECRET` empty the signature check is skipped, which keeps local
testing simple:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8000/webhooks/whatsapp \
  -H 'Content-Type: application/json' \
  -d '{
    "object": "whatsapp_business_account",
    "entry": [{"id": "WABA_ID", "changes": [{"field": "messages", "value": {
      "messaging_product": "whatsapp",
      "metadata": {"display_phone_number": "14155238886", "phone_number_id": "PHONE_ID"},
      "contacts": [{"profile": {"name": "John Smith"}, "wa_id": "11020645168"}],
      "messages": [{"from": "11020645168", "id": "wamid.demo-1",
                    "timestamp": "1735689600", "type": "text",
                    "text": {"body": "I want $450k for it"}}]
    }}]}]
  }'
```

The response is `200 OK` (Meta sends no reply body) and the next qualification
question appears in the application log / dry-run outbox. Continue with
`It needs a full rehab` and `about 2000 sq ft`; the last reply triggers the
handoff card (`whatsapp[DRY-RUN] -> +11020645167 | NEW QUALIFIED LEAD`).

To verify signatures locally, compute the header exactly as Meta does:

```bash
BODY='{"object":"whatsapp_business_account", ...}'
SIG="sha256=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$META_APP_SECRET" | awk '{print $2}')"
curl -X POST http://localhost:8000/webhooks/whatsapp \
  -H "X-Hub-Signature-256: $SIG" -H 'Content-Type: application/json' -d "$BODY"
```

---


## Meta WhatsApp Cloud API setup

### 1. Create the app and sender

1. Go to <https://developers.facebook.com/apps> → **Create app** → type **Business**.
2. Add the **WhatsApp** product.
3. In **WhatsApp → API Setup** you get a test number, a temporary access token and
   the **Phone number ID**. Add your own phone as a recipient and send the test
   message Meta shows you to open a 24-hour conversation window.
4. Note the **WhatsApp Business Account ID** (used for logs only).
5. For production, create a **System User** in Business Manager, assign the app and
   the WABA, generate a permanent token with `whatsapp_business_messaging`, and
   register/verify your own sender number.

### 2. Configure the webhook

1. Expose the service publicly (`https://your-domain.com`, `ngrok http 8000`, …).
2. In **WhatsApp → Configuration → Webhook**, set:
   * **Callback URL**: `https://<your-host>/webhooks/whatsapp`
   * **Verify token**: the exact value of `META_VERIFY_TOKEN`
3. Click **Verify and save** — Meta calls `GET /webhooks/whatsapp?hub.mode=subscribe…`
   and the app must echo `hub.challenge`.
4. Subscribe to the **messages** webhook field (message + status events).

### 3. Fill in `.env`

```bash
META_ACCESS_TOKEN=EAAG...            # temporary or system-user token
META_PHONE_NUMBER_ID=123456789012345 # Phone number ID, not the phone number
META_VERIFY_TOKEN=some-long-random-string
META_APP_SECRET=...                  # App Dashboard → Settings → Basic
META_WELCOME_TEMPLATE_NAME=real_estate_opener
META_TEMPLATE_LANGUAGE=en_US
META_TEMPLATE_PARAMS=first_name
PUBLIC_BASE_URL=https://<your-host>
DRY_RUN=false
```

Restart the API and send a WhatsApp message to your business number. You should get
the first qualification question back within a second.

### 4. Create the campaign template (required for cold outreach)

Business-initiated messages must use an approved template. In **WhatsApp Manager →
Message templates**, create one (category **Marketing**) such as:

> Hi {{1}}, this is Alex with Summit Bridge Properties 👋 I'm reaching out about your
> property. Are you still open to a cash offer?

Then set `META_WELCOME_TEMPLATE_NAME` to the template name, `META_TEMPLATE_LANGUAGE`
to its language, and `META_TEMPLATE_PARAMS=first_name` so `{{1}}` is filled with the
lead's first name. Supported tokens: `first_name`, `full_name`, `agent`, `company`,
`market`.

> Inside an open 24-hour window (the prospect messaged you recently) the opener falls
> back to plain text automatically when no template is configured. Cold lists need
> the template — Meta rejects free-form messages outside that window with error
> `131047`.

> Replies to a prospect are free-form within 24h of their last message (the normal
> case here). Only the campaign opener is business-initiated, which is why it goes
> through the approved template.

---


## Running an outreach campaign

### CLI

```bash
# explicit numbers, dry-run
python -m scripts.campaign --numbers 01020645167,01131043304

# CSV: phone[,name][,source]  (headers are auto-detected)
python -m scripts.campaign --file prospects.csv --limit 200

# live send (uses META_WELCOME_TEMPLATE_NAME for the opener)
python -m scripts.campaign --file prospects.csv --live
```

Exit codes: `0` success, `1` at least one failure, `2` nothing to send,
`3` live requested while the transport is dry-run.

Safety rules enforced by `app/services/campaign.py`:

* terminal leads (`HANDED_OFF`, `OPTED_OUT`, `FAILED`) are skipped;
* a lead whose opener was already sent is never messaged twice;
* sends are rate-limited by `OUTBOUND_RATE_LIMIT_PER_SECOND`;
* one bad number cannot abort the campaign;
* the opener is sent as the approved template configured in
  `META_WELCOME_TEMPLATE_NAME` (plain text only inside an open 24h window).

---

## Admin API

All `/api/*` routes require `X-API-Key: $ADMIN_API_KEY` when the key is configured.

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/leads` | Create / enrich one lead (phone is normalised to E.164) |
| POST | `/api/leads/bulk` | Import up to 1000 leads |
| GET | `/api/leads?status=QUALIFIED&limit=100` | Filter the pipeline |
| GET | `/api/leads/{id}` | Lead detail incl. collected slots |
| GET | `/api/leads/{id}/messages` | Full transcript |
| POST | `/api/leads/{id}/messages` | Human takeover message |
| POST | `/api/leads/{id}/handoff` | Force / retry the handoff card (409 if not qualified) |
| POST | `/api/leads/{id}/opt-out` | Suppress the lead immediately |
| GET | `/api/handoffs` | Handoff audit log |
| POST | `/api/campaigns/launch` | Import + send openers (honours `dry_run`) |
| GET | `/api/stats` | Funnel counts and conversion rate |
| GET | `/health`, `/ready` | Liveness / readiness (no auth) |

```bash
curl -s http://localhost:8000/api/leads?status=HANDED_OFF \
  -H "X-API-Key: $ADMIN_API_KEY" | jq
```

### Lead status model

```
NEW ──opener sent──▶ CONTACTED ──first reply──▶ IN_PROGRESS
                                                   │
                                    all 3 slots ───┴──▶ QUALIFIED ──card sent──▶ HANDED_OFF
                                                                   │
                                                    card failed ───┘ (retried next turn)

Any state ──"stop"/"not interested"──▶ OPTED_OUT        opener failed ──▶ FAILED
```

---


## Conversation design

### Persona

`app/agent/prompts.py` defines "Alex", an acquisitions specialist at a US
investment firm: professional, warm, 1-2 short sentences and **one question** per
message, American English, no hype. The full system prompt (used both as the
documented policy and as the optional LLM prompt) contains hard guardrails:

* no negotiating, counter-offers, price promises, discounts or commissions;
* no legal, tax, financing or investment advice;
* no binding commitments about timelines, repairs or fees;
* no requests for SSNs, bank details or card numbers;
* immediate compliance with "stop" / "not interested" / "human please".

### Slot extraction

`extract_slots()` runs regex + vocabulary rules on every message and can return
zero, one, two or three slots:

| Input | Extracted |
|---|---|
| `$450,000`, `450k`, `1.2 million`, `400-450k` | price 450000 / 1200000 / 425000 (midpoint) |
| `000` / `450` (as an answer to the price question) | 450000 |
| `turnkey`, `move-in ready`, `fully renovated` | `TURNKEY` |
| `minor cosmetic repairs`, `just paint and carpet` | `MINOR_REPAIRS` |
| `full rehab`, `gut job`, `fixer upper`, `down to the studs` | `FULL_REHAB` |
| `2,000 sq ft`, `1.8k sqft`, `1,200-square-foot`, `0.25 acres` | 2000 / 1800 / 1200 / 10890 |
| `"not sure yet"`, `"3 bed 2 bath"` | nothing (never invents) |

Sizes and bed/bath counts are masked before price parsing, so `2,000 sq ft` can
never become `$2,000`.

### One question at a time, never twice

* `leads.questions_asked` counts attempts per slot.
* `next_slot` is the first slot in `QUESTION_ORDER` that is still unknown, so a
  volunteered answer removes it from the queue — no repeat.
* Answering out of order is fine; answering everything in one message qualifies
  immediately.
* After `MAX_QUESTIONS_PER_SLOT` (default 3) attempts on the same slot the agent
  stops looping and offers a human follow-up instead.
* Each retry uses a different phrasing (`QUESTIONS` has variants), and the
  counter is persisted, so phrasing never restarts from the top.

### Handoff

When the last slot lands, the same turn:

1. persists the three values and flips status to `QUALIFIED`;
2. builds a `SummaryCard` with name, phone, source, price, condition, size,
   timestamp, turn count and lead id;
3. sends that text to `ADMIN_WHATSAPP_NUMBER` (default `01020645167`);
4. records the attempt in `handoffs` (`PENDING → SENT` or `FAILED` + error);
5. sets `HANDED_OFF` and confirms to the prospect.

Handoffs are idempotent: an already-delivered card is never re-sent, even if the
prospect keeps chatting. A failed card is retried on the next inbound turn so an
outage cannot lose a lead.

---


## Configuration reference

Everything is environment-driven; see `.env.example` for the full annotated list.

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./data/agent.db` | `postgresql+psycopg2://user:pass@host/db` in production |
| `AUTO_CREATE_TABLES` | `true` | disable when you manage migrations yourself |
| `REDIS_URL` | unset | enables cross-worker per-lead locks |
| `META_ACCESS_TOKEN` | unset | Graph API bearer token (system user in production) |
| `META_PHONE_NUMBER_ID` | unset | sender **phone number ID**, not the phone number |
| `META_VERIFY_TOKEN` | `change-me-verify-token` | echoed during the `hub.challenge` handshake |
| `META_APP_SECRET` | unset | enables `X-Hub-Signature-256` verification |
| `META_API_VERSION` | `v18.0` | Graph API version used in the messages URL |
| `META_GRAPH_BASE_URL` | `https://graph.facebook.com` | override for proxies/tests |
| `META_REQUEST_TIMEOUT_SECONDS` | `15` | per-request HTTP timeout |
| `META_MAX_RETRIES` | `2` | retries on 429/5xx/network errors |
| `META_WELCOME_TEMPLATE_NAME` | unset | approved template for the campaign opener |
| `META_TEMPLATE_LANGUAGE` | `en_US` | template language code |
| `META_TEMPLATE_PARAMS` | empty | comma separated tokens, e.g. `first_name,company` |
| `VALIDATE_META_SIGNATURE` | `true` | ignored when `META_APP_SECRET` is empty |
| `ADMIN_WHATSAPP_NUMBER` | `01020645167` | normalised to E.164 at load time |
| `ADMIN_API_KEY` | unset | if empty, the admin API is unauthenticated (dev only) |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | public base URL of this service |
| `DRY_RUN` | `true` | master kill-switch; logs instead of sending |
| `OUTBOUND_RATE_LIMIT_PER_SECOND` | `1` | spacing between campaign sends |
| `LLM_ENABLED` | `false` | true requires `OPENAI_API_KEY` |
| `OPENAI_BASE_URL` / `OPENAI_MODEL` | OpenAI / `gpt-4o-mini` | any OpenAI-compatible endpoint |
| `DEFAULT_COUNTRY_CODE` | `1` | used for 10-digit and trunk-zero numbers |
| `QUESTION_ORDER` | `price,condition,size` | reorder the qualification flow |
| `MAX_QUESTIONS_PER_SLOT` | `3` | loop protection before human escalation |
| `HANDOFF_NOTIFY_LEAD` | `true` | also confirm completion to the prospect |


---


## Deployment

### Docker Compose (PostgreSQL + Redis + app)

```bash
cp .env.example .env          # fill in Meta + admin key, set DRY_RUN=false when ready
docker compose up -d --build
docker compose logs -f app
```

Compose starts Postgres 16, Redis 7 and the API on port 8000, waits for both
dependencies to be healthy, and keeps data in named volumes. The app container
runs as an unprivileged user with a `/health` healthcheck.

### Any PaaS / VM

```bash
pip install -r requirements.txt
python -m scripts.init_db
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --proxy-headers
```

Put an HTTPS reverse proxy (nginx, Caddy, ALB) in front of it and point
`PUBLIC_BASE_URL` at that hostname, then set the Meta webhook callback URL to
`https://<host>/webhooks/whatsapp`.

**Worker count:** keep `--workers 1` unless `REDIS_URL` is configured. Locks are
in-process otherwise, and two workers could double-ask a question.

### Migrations

`init_db()` creates missing tables (safe to run repeatedly). For controlled
schema changes in production, adopt Alembic against the same `DATABASE_URL`;
`sql/schema.sql` is the reference DDL of the current version.

### Production checklist

- [ ] `DRY_RUN=false` and `META_ACCESS_TOKEN` / `META_PHONE_NUMBER_ID` are set.
- [ ] `ADMIN_API_KEY` is a long random value and the admin API is behind HTTPS/VPN.
- [ ] `PUBLIC_BASE_URL` matches the host used in the Meta webhook callback URL.
- [ ] `VALIDATE_META_SIGNATURE=true` and `META_APP_SECRET` is set.
- [ ] `DATABASE_URL` points at PostgreSQL with backups enabled.
- [ ] Campaign opener approved as a WhatsApp template (`META_WELCOME_TEMPLATE_NAME`).
- [ ] Opt-out keywords honoured (`STOP` is also enforced by WhatsApp itself).
- [ ] `MAX_QUESTIONS_PER_SLOT` and `OUTBOUND_RATE_LIMIT_PER_SECOND` tuned to your volume.
- [ ] Alerting on `handoffs.status='FAILED'` and on 5xx from the webhook.

---


## Testing

```bash
python -m pytest            # 161 tests, ~4 seconds, fully offline
python -m pytest -v
```

Coverage highlights:

| File | What it proves |
|---|---|
| `tests/test_phones.py` | E.164 normalisation, including trunk-zero numbers and `whatsapp:` prefixes |
| `tests/test_extraction.py` | price/condition/size variants, no false positives, midpoint ranges, LLM merge |
| `tests/test_guardrails.py` | opt-out, negotiation, legal and human-handoff detection |
| `tests/test_state_machine.py` | full qualification, no repeated questions, out-of-order answers, all-slots-in-one-message, guardrails, opt-out, escalation, idempotency, handoff retry — run through **both** LangGraph and the deterministic runner |
| `tests/test_webhook.py` | `hub.challenge` verification, `X-Hub-Signature-256` accept/reject, JSON message parsing (text/button/interactive/media), delivery receipts, duplicate `wamid`, end-to-end handoff card |
| `tests/test_meta_transport.py` | exact Graph API payloads for text and template messages, retry/error handling, template parameter rendering, dry-run fallback |
| `tests/test_api.py` | auth, lead CRUD, bulk import, campaign launch/skip rules, transcript, manual message, handoff 409→200, stats |

No network, no Meta account, no LLM key is required.

---


## Operations & troubleshooting

| Symptom | Cause / fix |
|---|---|
| 403 on webhook POST | `X-Hub-Signature-256` mismatch: `META_APP_SECRET` differs from the app, or a proxy rewrote the body. Disable only for local debugging. |
| Meta says "callback verification failed" | `META_VERIFY_TOKEN` differs from the value entered in the dashboard, or the URL path is not `/webhooks/whatsapp`. |
| No reply to a prospect | `DRY_RUN=true` (check `/health`), lead is `OPTED_OUT`, or the 24h window expired (error `131047` — use a template). |
| Lead never qualifies | Check `questions_asked` and the transcript. `"not sure yet"` extracts nothing by design — the agent nudges, then escalates after 3 attempts. |
| Handoff card not received | `GET /api/handoffs` shows `FAILED` + the Graph API error. The admin number must be a valid WhatsApp number (or a test recipient on the app). The lead stays `QUALIFIED` and retries automatically. |
| Same question twice | Ensure a single worker, or set `REDIS_URL` so per-lead locks are shared. |
| Duplicate replies | Make sure only one webhook subscription points at the app; `wamid` dedupe covers Meta's retries. |
| Numbers rejected | They must normalise to 8-15 digits. Pass ambiguous numbers in E.164 (`+14155550123`) or set `DEFAULT_COUNTRY_CODE`. |

Logs are `key=value` lines, e.g.:

```
2026-11-05 07:23:11 INFO app.agent.engine: inbound processed | lead_id=... status=HANDED_OFF qualified=True handoff=True
2026-11-05 07:23:11 INFO app.services.whatsapp: whatsapp sent | to=+1***5167 sid=SM... status=queued
```

---


## Compliance notes

* **Consent & templates** — cold outbound WhatsApp is regulated. Use an approved
  WhatsApp template for the first message and only message numbers you have a
  lawful basis to contact (US: TCPA/CAN-SPAM considerations, plus state rules).
* **Opt-out** — "stop"/"not interested"/"unsubscribe" flips the lead to
  `OPTED_OUT` immediately and permanently; no further messages are generated.
* **Data minimisation** — the agent never asks for SSNs, bank details or card
  numbers, and stores only the three qualification slots plus the transcript.
* **No advice / no negotiation** — enforced in the guardrail layer, not just in
  the prompt, so an LLM cannot be persuaded to promise a price or give legal advice.
* **Auditability** — every inbound/outbound message and every handoff attempt is
  persisted with timestamps and provider SIDs.
* **Retention** — transcripts are personal data; define a retention window and
  delete rows (or the whole lead) according to your policy and applicable law.


