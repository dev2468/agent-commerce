# Agent Commerce — an Agent Registry for UPI commerce

Submission for the **Razorpay AI Buildathon 2026, Track 1 (AI Growth & Agentic Commerce)**.

**The one-line pitch:** merchants register with Razorpay and get a merchant ID; *nobody registers
the agents*. This project is that missing half — an agent registers once and receives a signed
**Agent Passport** stating who it acts for and exactly what it may spend. Any merchant verifies
that passport locally against the registry's public key, and every rupee settles over UPI.

The differentiator is the **registry + passport**, not the payment rail. Razorpay already owns the
rail (UPI Reserve Pay, Route). We ride it and add the identity/trust layer on top.

---

## Read this first: what was deliberately deleted

This codebase went through a hard pivot. These are **gone on purpose** — do not reintroduce them,
and do not "restore" them when you find a dangling reference:

| Removed | Why |
| --- | --- |
| **The buyer approval queue** (`/buyer`, `buyer.html`, `/api/approvals*`) | The user killed it. Per-purchase approval now happens in the buyer's **UPI app**, not our own queue. |
| **Prepaid-wallet / PPI KYC tiers** (min ₹10k vs full ₹2L) | Reserve Pay is a *bank-account block*, not a PPI wallet. One ceiling now: ₹10,000. |
| **All non-UPI payment methods** — cards, netbanking, EMI, BNPL, payment-priority ordering | Settlement is UPI, full stop. `registry.SETTLEMENT` has exactly one allowed value. |
| **The two-track model** (approval-track vs wallet-track) | Collapsed to one model. There is no `payment_model` fork any more. |
| **Unauthenticated merchant mutation endpoints** (`POST /api/merchants`, `/api/merchants/{id}/products` write verbs) | Duplicated the token-authenticated `/api/merchant/*` routes *and* let anyone edit any catalog. |
| **The global UPI inbox** (`GET /api/upi/requests` with no auth) | It returned *every* pending collect request on the platform to anyone who asked — one buyer's purchases, and an approve button for them, in front of another. Now requires a reserve token and is scoped to that reserve; approve/decline verify ownership. |
| **`POST /api/agents/{id}/revoke` unauthenticated** | Anyone could revoke anyone's agent. Replaced by `POST /api/reserve/agents/{id}/revoke`, scoped to the owning reserve. |

Naming note: the SQLite tables are still called `wallets` / `wallet_ledger`, and several DB helpers
are still `*_wallet*`. **They hold reserves.** The storage names were left alone to avoid a risky
rename; the concept everywhere else is "reserve".

---

## Stack

- Python **3.13**, single venv at `venv/`. The venv is **not** on PATH — always `venv\Scripts\python.exe`.
- **FastAPI** gateway serving *both* the JSON API and the static HTML pages from one process.
- **SQLite** at `data/agent_commerce.db`. `cryptography` for Ed25519. `google-adk` + `mcp` for the agent.
- No build step for the frontend — plain HTML/CSS/JS, one shared stylesheet.

## Commands

Run everything from the project root.

```
venv\Scripts\python.exe -m uvicorn commerce_platform.gateway:app --reload --port 8000   # the app
venv\Scripts\python.exe -m commerce_platform.seed --reset                                 # WIPE + seed (do this before a demo)
venv\Scripts\python.exe -m agent.run                                                     # the ADK agent REPL
venv\Scripts\python.exe tests\test_registry.py                                           # 23 checks
venv\Scripts\python.exe tests\test_passport_live.py                                      # 5 checks
venv\Scripts\python.exe tests\test_collect.py                                            # 11 checks
venv\Scripts\python.exe tests\test_scoping.py                                            # 17 checks
venv\Scripts\python.exe tests\test_guardrails.py                                         # 29 checks
```

**Run the server in your own terminal**, not from an agent session — background processes get torn
down between turns, which kills it mid-demo. If HTTP is unreachable, the server stopped; restart it,
or test the backend directly in Python (the tests do exactly that, no server needed).

---

## Architecture

```
 Buyer                          Registry                       Merchant
   │                                │                              │
   │ 1. authorize reserve           │                              │
   │    (one UPI PIN, ≤₹10,000) ───▶│                              │
   │                                │                              │
   │ 2. register agent ────────────▶│ issue_passport()             │
   │                                │   Ed25519-signed, carries    │
   │◀── Agent Passport ─────────────│   caps + scope + expiry      │
   │                                │                              │
        agent shops ────────────────┼──── search across catalog ──▶│
                                    │                              │
        agent buys                  │                              │
          └─▶ pay_from_reserve() ───┤                              │
                 ├─ registry.authorize_purchase()  ← passport gate │
                 ├─ policy.check_spend_guards()    ← monthly, velocity
                 └─ default: UPI collect request                   │
   │◀─── approve in UPI app ────────┤                              │
                 └─ db.wallet_transfer()  reserve ──▶ merchant payout
```

### Guardrails beyond the two gates

- **Idempotency.** `pay_from_reserve(idempotency_key=...)` returns the original order on a replay,
  and a *unique partial index* on `orders.idempotency_key` is what actually enforces it — the
  pre-check alone loses a race. A retried click must never become a second debit.
- **Price drift.** `expected_amount` (integer paise) is the price the buyer was shown. If the
  catalog moved since, the purchase is refused with `price_changed` rather than settling at a
  number nobody agreed to.
- **Signed offers.** A discount is a merchant-signed document, not a mutable column. Both sides of
  a transaction now carry a verifiable credential.
- **SSRF + prompt injection** on the paste-a-link path — see `url_intel`. A fetched page can
  influence *what we search for* and nothing else; it can never name a product id or an amount.

### The two gates — each rule lives in exactly one place

Do not duplicate a check across both. That is how they drift.

1. **`registry.authorize_purchase()`** — the passport gate. Signature (re-verified *every*
   purchase), expiry, revocation, UPI-only settlement, per-transaction cap, daily cap, category
   scope. Returns a `Decision` with a stable refusal `code`.
2. **`policy.check_spend_guards()`** — only what a signed credential *cannot* carry, because it
   depends on live history: **monthly aggregate** and **velocity** (5 orders / 10 min).

### Purchase modes

`pay_from_reserve(..., mode=...)`:

- **`"collect"` (DEFAULT)** — pushes a UPI collect request to the buyer's app. Order sits at
  `awaiting_upi_approval`, **nothing is debited**. Settles only on `complete_upi_request()`.
- **`"autonomous"`** — opt-in; settles straight from the reserve with no prompt.

Both run the passport gate at initiation; collect re-runs it at approval, so authority that lapsed
in between (expired, revoked, day cap now breached) still stops the payment.

---

## RBI grounding (researched, not invented)

Constants live in `registry.py` and are enforced, not decorative:

| Constant | Value | Rule it encodes |
| --- | --- | --- |
| `RESERVE_BLOCK_CAP` | ₹10,000 | UPI Reserve Pay — max single block |
| `EMANDATE_PER_TXN_CAP` | ₹15,000 | e-mandate AFA-exempt per-transaction ceiling |
| `MAX_PASSPORT_DAYS` | 90 | Reserve Pay token validity |
| `SETTLEMENT` | `"upi"` | the only settlement rail issued |

**The honest pitch line** (use this exact framing — it survives scrutiny, "we bypass RBI 2FA" does
not): *consent is captured once at reserve authorization; autonomous spend happens inside RBI's own
e-mandate lane; anything larger or out of scope is refused.*

UI copy carries this honesty split via two callout styles in `theme.css`: **`.pnote`** (blue) =
"in production", **`.pnote.live`** (green) = "already enforced". Keep that distinction — blurring
it is the dishonest version.

---

## Module map

| File | What it owns |
| --- | --- |
| `commerce_platform/registry.py` | **The differentiator.** Passport issuance, Ed25519 signing, canonical JSON (integer-paise only — floats refused pre-sign), verification, the purchase gate. |
| `commerce_platform/payments.py` | Purchase orchestration: gates → order → collect-or-settle. `_settle()` is the only place money moves. |
| `commerce_platform/db.py` | SQLite. Schema + idempotent `_MIGRATIONS` list. Atomic `wallet_transfer` (the money math). |
| `commerce_platform/policy.py` | Monthly + velocity guards, and `get_budget` for the agent tool. |
| `commerce_platform/auth.py` | Secret hashing + three JWT scopes: agent, `merchant`, `wallet` (reserve). A scope cannot be used on another's endpoints. |
| `commerce_platform/gateway.py` | FastAPI: all endpoints + serves the pages. Has a **no-store middleware** — the browser must never cache a page (stale HTML calling dead endpoints cost hours). |
| `commerce_platform/mcp_server.py` | The agent's tool layer over stdio: `search_products`, `get_product_details`, `purchase_product`, `check_order_status`, `check_budget`, `view_audit_log`. |
| `commerce_platform/agent_runner.py` | The **same tools over Gemini function calling, in-process**, so the browser can drive an agent. A purchase here goes through `pay_from_reserve`, so both gates run exactly as for ADK. Returns a step transcript the console renders. |
| `commerce_platform/razorpay_client.py` | Real Razorpay. Orders + Payment Links are live; everything degrades to simulated rather than throwing. |
| `commerce_platform/offers.py` | **The seller's half of the credential.** Ed25519-signed merchant offers: a committed price, an expiry, optional bundle. Verified in the money path exactly like a passport. Refuses to sign a price above list, a >90% discount, or a float. |
| `commerce_platform/url_intel.py` | Paste-a-link → spec + price benchmark → parallel catalog search. Read its docstring before touching it: it is deliberately paranoid about SSRF and prompt injection. |
| `agent/run.py` | Google ADK + Gemini REPL that connects to the MCP server. |
| `static/theme.css` | The shared glass design system. Every page links it; pages add only page-specific rules. |

### Pages

| Route | File | Purpose |
| --- | --- | --- |
**There are two flows, User and Merchant.** `/wallet`, `/agents`, `/merchants` and
`/merchants/dashboard` are the superseded four-way split — still routed so old links work, but
`/user`, `/home` and `/merchant` are what the demo uses. Do not send people back to the old four.

| `/` | `index.html` | Landing — the two flows |
| `/user` | `user.html` | **Flow 1, onboarding.** Identity → reserve (OTP + funding) → agent + passport. Shows the **reserve** secret too, or the user can never sign back in. Puts the reserve token in `sessionStorage.rs_token`. |
| `/home` | `home.html` | **Flow 1, where you live.** Reserve, agents, passports, orders, ledger — and the **agent console**, the thing that makes the demo a demo: type a request, watch the tool trace, watch a collect request appear. |
| `/merchant` | `merchant.html` | **Flow 2, all of it.** Sign in → dashboard, or onboard → auto-signed into the same dashboard. The tag icon on a product row signs a **discount** (see `offers.py`). |
| `/pay/{order_id}` | `pay.html` | **Razorpay Checkout** for one order — openable on a real phone, pays over real UPI, verified by HMAC server-side. |
| `/wallet` `/agents` `/merchants` `/merchants/dashboard` | (legacy) | Superseded by the above. |
| `/verify` | `verify.html` | **The hero screen.** Paste a passport → verified, or refused with the exact code. Has a "tamper the cap" button that flips it to `bad_signature`. |
| `/upi` | `upi.html` | Simulated UPI app — pending collect requests, PIN pad, approve/decline |

---

## Invariants

1. **The passport signature is re-verified on every purchase.** Never trust a passport because it
   was issued once. A passport tampered *in the database* is refused — there is a test for it.
2. **Amounts are integer paise everywhere.** `canonical_json` refuses a float before signing;
   the gate refuses a float amount. Never introduce a float into a money path.
3. **Settlement is UPI only.** `registry.SETTLEMENT` is a single value and is checked after
   signature verification (defense in depth — a validly-signed non-UPI passport is still refused).
4. **`_settle()` is the only function that moves money**, and `db.wallet_transfer` is atomic —
   debit, credit and both ledger rows commit together or not at all, and it can never go negative.
5. **Every refusal has a stable `code`.** The attack matrix asserts on those codes; the MCP server
   surfaces them so the model can explain *why* rather than retrying blindly.
6. **An agent cannot exist without a reserve.** Registration requires a valid reserve with a UPI
   VPA — that VPA is what goes into the passport.

## Tests — the proof, keep it up

**Tests write to a scratch DB in the temp dir, never `data/agent_commerce.db`.** They create
merchants and products; running them repeatedly was what filled the demo catalog with "USB Cable"
x13. Keep that redirect at the top of any new test file.

85 checks across five files, all script-style (exit non-zero on failure, no server needed):

- `tests/test_registry.py` — **23-check attack matrix**: tamper, forge, strip signature, float
  smuggling, expiry, revocation, and every RBI bound refused at issuance.
- `tests/test_passport_live.py` — 5 checks that the passport is active in the money path.
- `tests/test_collect.py` — 11 checks: collect default, approve, decline, autonomous, gating.
- `tests/test_guardrails.py` — 29 checks: idempotent replay, price drift, forged/tampered/expired
  offers, and SSRF (loopback, cloud metadata, private ranges, odd schemes).
- `tests/test_scoping.py` — 17 checks: one reserve cannot see or settle another's UPI requests,
  revocation bites a request already sitting in the inbox, and the browser console is not a softer
  path to money than the ADK agent.

Security-by-testing-what-fails is the strongest evidence in this submission. When you add a rule,
add the refusal test with it.

---

## What is really Razorpay, and what is modelled

Probed against the live `rzp_test_*` key, not assumed. Re-probe before claiming anything more:

| Razorpay API | Status | Used for |
| --- | --- | --- |
| `POST /v1/orders` | **works** | the reserve block, and every agent purchase |
| `POST /v1/payment_links` | **works** | the collect request — a link the buyer can genuinely open and pay |
| `POST /v1/payments/create/upi` | **400, not enabled** | would be a true S2S UPI collect pushed into the payer's app |
| `POST /v1/plans` | **401, not enabled** | would be native e-mandate / subscriptions |
| **Razorpay Checkout** + HMAC verify | **works, no cap** | `/pay/{order_id}` — the real payable path |

**Payment links are capped at 30 for the lifetime of a test account, and ours is used up.**
Cancelling them does not free quota. So links are now created *lazily* (`ensure_payment_link`, or
opt in globally with `RZP_AUTO_LINK=1`), and the real payable path is **Razorpay Checkout** at
`/pay/{order_id}` — no cap, and what a real merchant ships anyway. A payment there is accepted only
after **two** independent proofs: the HMAC signature (forgeable only with the key secret, which
never leaves the server) *and* a server-side read confirming the payment is captured, belongs to
that order, and is for the right amount. If the passport refuses *after* Razorpay took the money,
the order parks at `paid_unreconciled` and audits loudly — that is a refund, not a shrug.

So: **the Order and Payment Link ids are real** (`order_...`, `plink_...`, and the `rzp.io/rzp/...`
short URL). **Single Block Multi Debit is modelled** — holding ₹10,000 and drawing it down lives in
our ledger against a real Order, because Reserve Pay needs account enablement. Say "modelled" about
the block and "real" about the ids, and the claim survives scrutiny. The UI enforces this: it only
renders a Razorpay badge when an id actually came back, and shows "block modelled locally" otherwise.

## The agent's brain: OpenRouter first

`agent_runner` routes **OpenRouter → Google SDK → deterministic**, and the transcript always says
which one ran. Set `OPENROUTER_API_KEY` (and optionally `OPENROUTER_MODEL`, default
`google/gemini-3.7-flash`). OpenRouter is OpenAI-compatible, so it is a separate code path from the
Google SDK — same `TOOLS`, different wire format. Verified working end to end.

## Gemini model names shift — do not "fix" them blind

`agent/run.py` and `agent_runner.py` use **`gemini-3.6-flash`**. `gemini-2.5-flash` is already
closed to new keys (the API 404s with a message naming 3.6 as the replacement), and `models.list()`
does **not** list 3.6 even though it works. A model name that looks wrong here probably is not —
verify with an actual call before changing it. `agent_runner` walks a fallback chain and caches
whichever model answers, so a single 404 or 504 does not drop the turn onto the fallback planner.

Calls from this machine intermittently die with `WinError 10053` / 504. That is local, not Google.
The runner retries, then degrades to a deterministic planner **and says so in the transcript** —
never let it pass a for-loop's choice off as the model's.

## Repo

`https://github.com/dev2468/agent-commerce` (**private** — flip with
`gh repo edit --visibility public --accept-visibility-change-consequences`).

`.gitignore` excludes `data/*.key`. **`data/registry_ed25519.key` is the registry's private
signing key — the root of trust for every passport.** Publishing it lets anyone mint a passport
that verifies. It was one `git add -A` away from being committed; check before you stage.

## Known open issues

Do not spend a session rediscovering these.
- **Seeded merchants have no `merchant_secret_hash`**, so they cannot sign into the merchant
  dashboard. Their catalogs still work for agent search. Only merchants created through
  `/api/merchants/onboard` can log in.
- **Demo prompts must match the catalog.** The cheapest audio product is the Sony WH-1000XM5 at
  ₹24,990, so "buy headphones under ₹5,000" correctly finds nothing and looks broken. Prompts that
  land: *"Buy a face oil under ₹2,000"* (Forest Essentials ₹1,995), *"Buy dumbbells"* (Domyos
  ₹1,999). And the best beat — *"Buy the Sony WH-1000XM5"* against a ₹5,000 mandate refuses with
  `per_txn_exceeded`.
- **`agent/run.py`'s ADK loop still has not been watched end to end.** `agent_runner.py` now proves
  the same tools work against Gemini, which removes most of the risk, but the ADK+MCP subprocess
  path itself is still un-demoed.
- **The reserve→merchant transfer is a local ledger move.** In production this is Razorpay Route
  splitting the debit to the merchant's linked account. Say "modelled", not "integrated".
- **Merchant onboarding still has cosmetic fields**: settlement cycle, accepted methods, returns
  window and auto-accept are stored but never enforced, and `razorpay_account` is never validated
  or used. Razorpay **Route** is the missing real piece — it would make the merchant a linked
  account rather than a row in our ledger.
- **The UPI/Card choice on the funding step is cosmetic** — both load the reserve identically.
  Real Reserve Pay is a UPI-PIN hold; card is there to make the step feel like a real checkout.
- **`db.update_merchant` has no caller.** Left in as a plausible CRUD helper; it is not load-bearing.
- **`.env` holds live test credentials** (`RAZORPAY_KEY_ID/SECRET`, `GOOGLE_API_KEY`, `JWT_SECRET`).
  Never commit it; never paste its contents into a response.
