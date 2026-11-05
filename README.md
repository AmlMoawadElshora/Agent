# WhatsApp Real-Estate Outreach & Qualification Agent

A production-ready, fully automated WhatsApp agent for the US real-estate market.
It imports prospect phone numbers, opens the conversation, qualifies each lead over
several natural turns, and pushes a structured lead card to the acquisitions
manager the moment all three qualification slots are known.

```
                 ┌──────────────────────────────────────────────────────┐
  prospect list  │                  FastAPI service                     │
  (numbers/CSV) ─┼─▶ /api/campaigns/launch ──▶ WhatsApp (Twilio)         │
                 │            ▲                        │                │
                 │            │ inbound                │ opener         │
                 │  /webhooks/twilio/whatsapp          ▼                │
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
3. [Quick start (5 minutes, no Twilio needed)](#quick-start)
4. [Twilio WhatsApp setup](#twilio-whatsapp-setup)
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
| Send the opening WhatsApp message | `app/services/campaign.py` → Twilio Programmable Messaging |
| Two-way, human-like English conversation | `app/agent/graph.py` state machine + `app/agent/prompts.py` persona (optional LLM rewrite) |
| Collect 3 mandatory slots, one question at a time | `price` → `condition` → `size`, tracked per lead in `leads.questions_asked` |
| Never repeat a question already answered | `collected` slots drive `next_slot`; out-of-order answers are honoured |
| Immediate handoff on qualification | `app/services/handoff.py` sends the summary card to `ADMIN_WHATSAPP_NUMBER` (default `01020645167`) |
| Persistent lead state | PostgreSQL (SQLite for local dev) — `NEW → CONTACTED → IN_PROGRESS → QUALIFIED → HANDED_OFF`, plus `OPTED_OUT` / `FAILED` |
| Audit trail | Full transcript in `messages`, every handoff attempt in `handoffs` |
| Robustness | Twilio signature validation, `MessageSid` idempotency, per-lead locks, retries, guardrails, dry-run mode |

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
│   ├── whatsapp.py          sender protocol, Twilio sender, dry-run sender
│   ├── handoff.py           summary card + admin delivery + retry semantics
│   ├── campaign.py          outbound opener, rate limiting, skip rules
│   └── locks.py             Redis / in-process per-lead locks
├── api/
│   ├── webhook.py           Twilio webhook (signature → engine → TwiML)
│   ├── leads.py             admin CRUD, transcript, manual message, handoff, campaign
│   ├── health.py            /health, /ready, /api/stats
│   └── deps.py              X-API-Key auth
scripts/
├── campaign.py              CLI campaign runner (numbers or CSV)
└── init_db.py               schema creation
sql/schema.sql               reference PostgreSQL DDL
tests/                       137 offline tests (no Twilio, no LLM, no network)
```

### The turn lifecycle

1. Twilio POSTs to `/webhooks/twilio/whatsapp`.
2. The `X-Twilio-Signature` header is verified (HMAC-SHA1 over the public URL + sorted form).
3. `ConversationEngine.handle_inbound` takes a per-lead lock and ignores duplicate `MessageSid`s.
4. The LangGraph machine runs five pure nodes: `extract → guardrail → merge → qualify → compose`.
5. Newly heard slots are persisted; the question counter for the next slot is incremented.
6. If all three slots exist, `perform_handoff` sends the card to the admin, records it, and
   flips the lead to `HANDED_OFF`. A failed card leaves the lead `QUALIFIED` so the next
   inbound turn retries it automatically.
7. The reply is returned as TwiML (no second API call) or sent via REST (`REPLY_MODE=api`).

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
but never transmitted, so you can demo the whole flow without a Twilio account.

### 5. Simulate a conversation

```bash
# Twilio is not involved: post the same form Twilio would send.
# (signature validation is skipped automatically when TWILIO_AUTH_TOKEN is unset)
curl -s -X POST http://localhost:8000/webhooks/twilio/whatsapp \
  -d 'From=whatsapp:01020645168' \
  -d 'Body=I want $450k for it' \
  -d 'ProfileName=John Smith' \
  -d 'MessageSid=SM-demo-1' \
  -d 'NumMedia=0'
```

The response is TwiML containing the next question. Continue with
`Body=It needs a full rehab` and `Body=about 2000 sq ft`; the third reply triggers
the handoff card in your logs (`whatsapp[DRY-RUN] -> +11020645167 | NEW QUALIFIED LEAD`).

---


## Twilio WhatsApp setup

### Sandbox (fastest way to test on a real phone)

1. Create a free Twilio account and open **Messaging → Try it → Send a WhatsApp message**.
2. Note the sandbox sender (`whatsapp:+14155238886`) and join the sandbox from your
   phone by sending the shown code to that number.
3. Expose your local server: `ngrok http 8000` (or any HTTPS tunnel).
4. In the sandbox settings, set **"WHEN A MESSAGE COMES IN"** to
   `https://<your-tunnel>/webhooks/twilio/whatsapp` (method `POST`).
5. Put the credentials in `.env`:

```bash
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_WHATSAPP_FROM=whatsapp:+14155238886
PUBLIC_BASE_URL=https://<your-tunnel>     # must match the URL Twilio calls
DRY_RUN=false
```

6. Restart the API and send a WhatsApp message from your phone. You should get the
   first qualification question back within a second.

> The sandbox can only message numbers that joined it, and it expires after 72
> hours of inactivity. For anything real, use a production sender.

### Production sender

1. Buy or port a number that supports WhatsApp, or register a WhatsApp Business
   Account (WABA) in **Twilio Console → Messaging → Senders → WhatsApp**.
2. Complete Meta business verification and submit message templates for the
   **opening** message (business-initiated conversations require an approved
   template outside the 24-hour customer-service window).
3. Set `TWILIO_WHATSAPP_FROM=whatsapp:+1XXXXXXXXXX`.
4. Keep `VALIDATE_TWILIO_SIGNATURE=true` and make sure `PUBLIC_BASE_URL` matches the
   exact public URL (scheme, host, path), otherwise every webhook is rejected with
   403 by design.

> Replies to a prospect are free-form within 24h of their last message (the normal
> case here). The campaign opener is business-initiated, so map
> `GREETING_TEMPLATE` in `app/agent/prompts.py` to an approved template.

---


## Running an outreach campaign

### CLI

```bash
# explicit numbers, dry-run
python -m scripts.campaign --numbers 01020645167,01131043304

# CSV: phone[,name][,source]  (headers are auto-detected)
python -m scripts.campaign --file prospects.csv --limit 200

# live send
python -m scripts.campaign --file prospects.csv --live
```

Exit codes: `0` success, `1` at least one failure, `2` nothing to send,
`3` live requested while the transport is dry-run.

Safety rules enforced by `app/services/campaign.py`:

* terminal leads (`HANDED_OFF`, `OPTED_OUT`, `FAILED`) are skipped;
* a lead whose opener was already sent is never messaged twice;
* sends are rate-limited by `OUTBOUND_RATE_LIMIT_PER_SECOND`;
* one bad number cannot abort the campaign.

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
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` | unset | both required to transmit |
| `TWILIO_WHATSAPP_FROM` | `whatsapp:+14155238886` | sandbox sender by default |
| `ADMIN_WHATSAPP_NUMBER` | `01020645167` | normalised to E.164 at load time |
| `ADMIN_API_KEY` | unset | if empty, the admin API is unauthenticated (dev only) |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | must match Twilio's URL for signature checks |
| `VALIDATE_TWILIO_SIGNATURE` | `true` | ignored when no auth token is set |
| `REPLY_MODE` | `twiml` | `api` sends the reply through the REST API instead |
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
cp .env.example .env          # fill in Twilio + admin key, set DRY_RUN=false when ready
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
`PUBLIC_BASE_URL` at that hostname, then set the Twilio webhook to
`https://<host>/webhooks/twilio/whatsapp`.

**Worker count:** keep `--workers 1` unless `REDIS_URL` is configured. Locks are
in-process otherwise, and two workers could double-ask a question.

### Migrations

`init_db()` creates missing tables (safe to run repeatedly). For controlled
schema changes in production, adopt Alembic against the same `DATABASE_URL`;
`sql/schema.sql` is the reference DDL of the current version.

### Production checklist

- [ ] `DRY_RUN=false` and Twilio credentials are set.
- [ ] `ADMIN_API_KEY` is a long random value and the admin API is behind HTTPS/VPN.
- [ ] `PUBLIC_BASE_URL` exactly matches the Twilio webhook URL.
- [ ] `VALIDATE_TWILIO_SIGNATURE=true`.
- [ ] `DATABASE_URL` points at PostgreSQL with backups enabled.
- [ ] Opening message approved as a WhatsApp template (business-initiated).
- [ ] Opt-out keywords honoured (`STOP` is also enforced by WhatsApp itself).
- [ ] `MAX_QUESTIONS_PER_SLOT` and `OUTBOUND_RATE_LIMIT_PER_SECOND` tuned to your volume.
- [ ] Alerting on `handoffs.status='FAILED'` and on 5xx from the webhook.

---


## Testing

```bash
python -m pytest            # 137 tests, ~3 seconds, fully offline
python -m pytest -v
```

Coverage highlights:

| File | What it proves |
|---|---|
| `tests/test_phones.py` | E.164 normalisation, including trunk-zero numbers and `whatsapp:` prefixes |
| `tests/test_extraction.py` | price/condition/size variants, no false positives, midpoint ranges, LLM merge |
| `tests/test_guardrails.py` | opt-out, negotiation, legal and human-handoff detection |
| `tests/test_state_machine.py` | full qualification, no repeated questions, out-of-order answers, all-slots-in-one-message, guardrails, opt-out, escalation, idempotency, handoff retry — run through **both** LangGraph and the deterministic runner |
| `tests/test_webhook.py` | signature accept/reject, TwiML output, duplicate `MessageSid`, delivery receipts, end-to-end handoff card |
| `tests/test_api.py` | auth, lead CRUD, bulk import, campaign launch/skip rules, transcript, manual message, handoff 409→200, stats |

No network, no Twilio account, no LLM key is required.

---


## Operations & troubleshooting

| Symptom | Cause / fix |
|---|---|
| 403 on every webhook | `PUBLIC_BASE_URL` doesn't match the URL Twilio calls, or the auth token is wrong. Check the exact scheme/host/path and restart. |
| No reply to a prospect | `DRY_RUN=true` (check `/health`), lead is `OPTED_OUT`, or the 24h messaging window expired. |
| Lead never qualifies | Check `questions_asked` and the transcript. `"not sure yet"` extracts nothing by design — the agent nudges, then escalates after 3 attempts. |
| Handoff card not received | `GET /api/handoffs` shows `FAILED` + error. The admin number must have joined the sandbox (or be a valid WhatsApp number). The lead stays `QUALIFIED` and retries automatically. |
| Same question twice | Ensure a single worker, or set `REDIS_URL` so per-lead locks are shared. |
| Duplicate replies | Verify Twilio isn't pointing two webhook URLs at the app; `MessageSid` dedupe covers retries within one URL. |
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


