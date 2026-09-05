# Agent Commerce

**An agent registry for UPI commerce.**
Razorpay AI Buildathon 2026 · Track 1 (AI Growth & Agentic Commerce)

> Merchants register with Razorpay and get a merchant ID. **Nobody registers the agents.**

This is that missing half. An agent registers once and receives a signed **Agent Passport**
stating who it acts for and exactly what it may spend. Any merchant verifies that passport
locally against the registry's public key — and every rupee still settles over UPI.

The differentiator is the **registry and the credential**, not the payment rail. Razorpay already
owns the rail. We ride it and add the identity and trust layer on top.

---

## The problem

An AI agent that shops on your behalf has no identity a merchant can check, and no spending
mandate anyone can enforce. Today a merchant cannot tell a legitimate shopping agent from a
script, and a buyer has no way to bound what their agent may do beyond hoping the prompt holds.

"Trust me, my agent is well-behaved" is not a security model. A signed, verifiable, revocable
credential is.

## What this does

- **Issues Agent Passports** — Ed25519-signed, carrying per-transaction cap, daily cap, category
  scope, settlement rail and expiry.
- **Enforces them on every purchase.** The signature is re-verified each time; a passport tampered
  with *in our own database* is refused, and there is a test for exactly that.
- **Settles over UPI** from a pre-authorized reserve, with per-purchase approval in the buyer's
  own UPI app by default.
- **Lets merchants sign offers**, so both sides of a transaction carry a verifiable credential:
  the buyer's passport bounds what the agent may spend, the merchant's offer commits the price.

---

## Architecture

```
 Buyer                          Registry                       Merchant
   │                                │                              │
   │ 1. authorize reserve           │                              │
   │    (one UPI PIN, ≤ ₹10,000) ──▶│                              │
   │                                │                              │
   │ 2. register agent ────────────▶│  issue_passport()            │
   │                                │    Ed25519-signed, carries   │
   │◀── Agent Passport ─────────────│    caps + scope + expiry     │
   │                                │                              │
        agent shops ────────────────┼──── search across catalog ──▶│
                                    │                              │
        agent buys                  │                              │
          └─▶ pay_from_reserve() ───┤                              │
                 ├─ registry.authorize_purchase()  ← passport gate │
                 ├─ policy.check_spend_guards()    ← monthly, velocity
                 └─ default: UPI collect request                   │
   │◀─── approve in UPI app ────────┤                              │
                 └─ db.wallet_transfer()   reserve ──▶ merchant payout
```

### Two gates, each rule in exactly one place

1. **`registry.authorize_purchase()`** — the passport gate. Signature, expiry, revocation,
   UPI-only settlement, per-transaction cap, daily cap, category scope. Returns a decision with a
   stable refusal `code`.
2. **`policy.check_spend_guards()`** — only what a signed credential *cannot* carry because it
   depends on live history: monthly aggregate and velocity (5 orders / 10 min).

### Purchase modes

- **`collect`** (default) — pushes a UPI collect request to the buyer's app. The order sits at
  `awaiting_upi_approval` and **nothing is debited** until they approve.
- **`autonomous`** — opt-in; settles straight from the reserve with no prompt.

Both run the passport gate at initiation. Collect re-runs it at approval, so authority that
lapsed in between — expired, revoked, day cap now breached — still stops the payment.

---

## What is really Razorpay, and what is modelled

Probed against a live `rzp_test_*` key rather than assumed:

| Razorpay API | Status | Used for |
| --- | --- | --- |
| `POST /v1/orders` | **works** | the reserve block, and every agent purchase |
| **Checkout** + HMAC verify | **works, no cap** | `/pay/{order_id}` — the real payable path |
| `POST /v1/payment_links` | **works**, 30/account lifetime cap | optional collect link |
| `POST /v1/payments/create/upi` | **400, not enabled** | would be true S2S UPI collect |
| `POST /v1/plans` | **401, not enabled** | would be native e-mandate |

**Real:** every `order_...` and `pay_...` id, Razorpay Checkout, and HMAC signature verification.

**Modelled:** *Single Block Multi Debit* (UPI Reserve Pay) and merchant settlement. Holding ₹10,000
and drawing it down lives in our own ledger against a real Razorpay Order, because Reserve Pay
needs account enablement. Merchant payout is a local ledger move where production would use
Razorpay Route.

The UI enforces this distinction: it renders a Razorpay badge only when an id actually came back,
and says "block modelled locally" otherwise.

### RBI grounding

Constants in `registry.py`, enforced rather than decorative:

| Constant | Value | Rule |
| --- | --- | --- |
| `RESERVE_BLOCK_CAP` | ₹10,000 | UPI Reserve Pay — max single block |
| `EMANDATE_PER_TXN_CAP` | ₹15,000 | e-mandate AFA-exempt per-transaction ceiling |
| `MAX_PASSPORT_DAYS` | 90 | Reserve Pay token validity |
| `SETTLEMENT` | `upi` | the only settlement rail issued |

The honest framing: *consent is captured once at reserve authorization; autonomous spend happens
inside RBI's own e-mandate lane; anything larger or out of scope is refused.*

---

## Security model

The interesting question is not "does a purchase work" but "what can't happen".

- **Amounts are integer paise everywhere.** A float is refused before signing, at any nesting
  depth — `₹99.999` rounding differently on two sides is the classic agentic-payment attack.
- **Idempotency**, enforced by a unique index rather than a pre-check that loses races. A retried
  click is never a second debit.
- **Price drift** — settle only at the price the buyer was actually shown, else `price_changed`.
- **Ownership scoping** — a reserve can only see and settle its own UPI requests. Revocation is
  owner-scoped and bites a request already sitting in the inbox.
- **Checkout payments need two independent proofs**: the HMAC signature (forgeable only with the
  key secret, which never leaves the server) *and* a server-side read confirming the payment is
  captured, belongs to that order, and matches the amount. A browser that merely claims it paid is
  refused.
- **Paste-a-link is hardened** against SSRF (DNS resolved up front, private/loopback/link-local and
  cloud-metadata ranges blocked, redirects re-checked per hop) and against prompt injection: only
  named structured fields are extracted, never page prose. A hostile page can change *what we
  search for* and nothing else — and the passport gate remains the backstop, so **prompt injection
  cannot become a payment**.

If the passport refuses *after* Razorpay already captured money, the order parks at
`paid_unreconciled` and audits loudly. In production that is a refund, not a shrug.

---

## Quick start

Python 3.13. Everything runs from the project root.

```bash
python -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env        # then fill in your keys
venv\Scripts\python.exe -m commerce_platform.seed --reset
venv\Scripts\python.exe -m uvicorn commerce_platform.gateway:app --reload --port 8000
```

Open <http://localhost:8000>. The reseed prints merchant logins — keep them.

`.env` needs a Razorpay test key and one model key (`OPENROUTER_API_KEY` preferred, else
`GOOGLE_API_KEY`). Without either, the agent still runs on a deterministic planner and says so in
its transcript — a demo that silently degrades from "an LLM chose this" to "a for-loop chose this"
is lying about the interesting part.

### Pages

| Route | Purpose |
| --- | --- |
| `/` | Landing — the two flows |
| `/user` | Authorize a reserve, register an agent, mint its passport |
| `/home` | Your agents, passports, orders, ledger — and the **agent console** |
| `/merchant` | Onboard or sign in; catalog, orders, and **signed discounts** |
| `/verify` | Paste a passport → verified, or refused with the exact code |
| `/upi` | Simulated UPI app — pending collect requests, PIN pad |
| `/pay/{order_id}` | Razorpay Checkout — payable from a real phone |

---

## Project layout

| File | What it owns |
| --- | --- |
| `commerce_platform/registry.py` | **The differentiator.** Passport issuance, Ed25519 signing, canonical JSON, verification, the purchase gate. |
| `commerce_platform/offers.py` | The seller's half — signed merchant offers with expiry and bundles. |
| `commerce_platform/payments.py` | Purchase orchestration: gates → order → collect-or-settle. `_settle()` is the only place money moves. |
| `commerce_platform/policy.py` | Monthly and velocity guards. |
| `commerce_platform/db.py` | SQLite, schema, migrations, atomic `wallet_transfer`. |
| `commerce_platform/razorpay_client.py` | Real Razorpay. Degrades to simulated rather than throwing. |
| `commerce_platform/agent_runner.py` | The agent loop the browser drives — same tools the ADK agent gets. |
| `commerce_platform/url_intel.py` | Paste-a-link → spec + price benchmark. Deliberately paranoid. |
| `commerce_platform/mcp_server.py` | The tool layer over stdio for Google ADK. |
| `commerce_platform/gateway.py` | FastAPI: every endpoint, and serves the pages. |
| `agent/run.py` | Google ADK + Gemini REPL over the MCP server. |

---

## Tests

**94 checks across five files.** Script-style — no server, no framework, exit non-zero on failure.
They write to a scratch database, never the demo one.

```bash
venv\Scripts\python.exe tests\test_registry.py        # 23 — the attack matrix
venv\Scripts\python.exe tests\test_collect.py         # 20 — collect vs autonomous & high-value UPI
venv\Scripts\python.exe tests\test_passport_live.py   #  5 — passport in the money path
venv\Scripts\python.exe tests\test_scoping.py         # 17 — ownership isolation
venv\Scripts\python.exe tests\test_guardrails.py      # 29 — idempotency, drift, offers, SSRF
```

The attack matrix is the strongest evidence here: tamper, forge, strip the signature, smuggle a
float, expire, revoke, and every RBI bound refused at issuance. **Security by testing what fails.**

---

## Known limitations

Stated plainly, because a submission that hides these is worse than one that names them.

- **Merchant settlement is a local ledger move.** Razorpay Route is the missing real piece.
- **Payment links are capped at 30 per test account** for its lifetime; `/pay/` (Checkout) has no
  such cap and is the path to demo.
- **Some merchant-onboarding fields are cosmetic** — settlement cycle, accepted methods, returns
  window; stored but not enforced.
- **Secrets are SHA-256 hashed.** Argon2 or bcrypt is the right answer.
- **No rate limiting** on the auth endpoints yet.
- **`agent/run.py`'s ADK+MCP loop has not been demoed end to end.** `agent_runner.py` proves the
  same tool layer works against a live model; that specific subprocess path has not been filmed.

---

## Licence & notes

Built for the Razorpay AI Buildathon 2026. `data/registry_ed25519.key` is the registry's **private
signing key** and is gitignored — publishing it would let anyone mint a passport that verifies.

See `DEMO.md` for the recording script and `CLAUDE.md` for engineering notes.
