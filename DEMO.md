 # Demo script — Agent Commerce

Target: **4–5 minutes**. The argument is one sentence, and every scene proves a
piece of it:

> Merchants register with Razorpay. Nobody registers the agents. We are that
> missing half — and the credential is enforced, not decorative.

---

## Before you record

```
venv\Scripts\python.exe -m commerce_platform.seed --reset
```

Then start the server **in your own terminal** and leave it there:

```
venv\Scripts\python.exe -m uvicorn commerce_platform.gateway:app --reload --port 8000
```

- Reseeding prints fresh merchant logins — keep that terminal output.
- Open a **fresh browser tab** (a stale `sessionStorage` token points at a reserve
  that no longer exists after a reset).
- Have your phone ready for the `/pay` scene.
- Catalog after a reset: 4 merchants, 13 products, ₹549 → ₹1,29,999.

**Say "modelled", not "integrated", about UPI Reserve Pay.** It survives a judge's
follow-up; "we integrated Reserve Pay" does not.

---

## Scene 1 · The gap (20s) — `/`

Land on the home page. Read the headline aloud:

> "Merchants register with Razorpay. Nobody registers the agents."

Point at the two cards.

> "An agent today has no identity and no spending mandate. A merchant has no way
> to tell a legitimate shopping agent from a script. We add that layer, and every
> rupee still settles over UPI."

---

## Scene 2 · Becoming an agent's owner (60s) — `/user`

**Step 1** — name, email, mobile. Say the mobile matters: it's who Razorpay
notifies.

**Step 2** — hit `autofill` for the OTP, UPI id `you@okaxis`, reserve **₹8,000**.

> "This is the only time a human authenticates. One factor, once. RBI's Reserve
> Pay caps a single block at ₹10,000 — that ceiling is enforced in code, not
> decoration."

**Step 3** — leave the mandate at **₹5,000**, framework **Google ADK**.

> "This mints the passport: Ed25519-signed, carrying the caps, the category
> scope and a 90-day expiry."

**Done screen** — point at the passport card, then:

> "Save the reserve secret — that's how you get back in."

Click **Open your agent console →**.

---

## Scene 3 · The agent actually shops (75s) — `/home`

This is the scene that matters. Everything before it was setup.

Point at the balance, then the **Razorpay order badge** next to it.

> "That's a real Razorpay order id. The block itself is modelled in our ledger,
> because Single Block Multi Debit needs account enablement — the id is real, the
> hold is ours."

In the console, type:

```
Buy a face oil under ₹2,000
```

Let the trace render. Narrate it as it appears:

> "It checked its own budget, searched across every merchant, and bought. Watch
> the bottom line — a collect request, not a payment. Nothing is debited yet."

**Then the beat that sells it.** Type:

```
Buy the Sony WH-1000XM5 headphones
```

It refuses, in red, with `per_txn_exceeded`.

> "₹24,990 against a ₹5,000 mandate. The agent cannot argue with this — the cap
> is inside a signed credential, and the gate re-verifies that signature on every
> single purchase."

*(Optional, if the room is technical — paste any product URL instead of typing:
it becomes a spec and a price to beat, and the agent returns ranked options from
your merchants. Mention that a fetched page can only change what we search for,
never a product id or an amount.)*

---

## Scene 4 · Proving the passport (35s) — `/verify`

From an agent card, click **Verify passport**. It loads signed.

> "Any merchant can do this against the registry's public key. They never have
> to trust our server."

Click **tamper the cap** → `bad_signature`.

> "Raise the cap after signing and it stops verifying. That's the whole trust
> model in one button."

---

## Scene 5 · Real money, real phone (50s) — `/upi` then `/pay`

Open `/upi`. Your pending request is there — and only yours.

> "This inbox is scoped to my reserve. It used to show every pending request on
> the platform to anyone who loaded the page. That's fixed and there's a test for it."

**Either** approve with the PIN pad (fast), **or** — better — hit **Pay for real**,
open `/pay/ord_...` **on your phone**, and pay with `success@razorpay`.

> "That's Razorpay Checkout with the real order id. When it comes back, the server
> verifies the HMAC signature *and* re-reads the payment from Razorpay before a
> rupee moves. A browser that just claims it paid gets refused."

Return to `/home`: order is **PAID**, balance dropped.

---

## Scene 6 · The merchant side (50s) — `/merchant`

Sign in as **Nykaa** (from your reseed output).

Show Orders — the agent's purchase is there, with revenue.

> "The merchant did nothing special. They listed a product and an agent found it."

Now the tag icon on a product → **Sign a discount** → 20% → **Sign offer**.

> "That discount is Ed25519-signed by the merchant's own key. So both sides of the
> transaction now carry a verifiable credential: the buyer's passport bounds what
> the agent may spend, and the merchant's offer commits the price. Edit one byte
> and the buyer's gate refuses it."

---

## Scene 7 · Close (25s)

> "Consent is captured once, at reserve authorization. Autonomous spend happens
> inside RBI's own e-mandate lane. Anything larger, out of scope, expired or
> revoked is refused — and 94 tests prove the refusals, not just the happy path."

Optionally show the terminal:

```
venv\Scripts\python.exe tests\test_registry.py
```

---

## If something breaks on camera

| Symptom | What to do |
|---|---|
| Console says `deterministic` | Model hiccup. Re-run the prompt; don't claim it was the LLM. |
| Agent finds nothing | Your prompt doesn't match the catalog. Use the face-oil one. |
| `Pay for real` fails | Payment-link quota (30/lifetime) is gone. Use `/pay/` — no cap. |
| UPI inbox empty | Stale token from before the reset. Re-sign in with the reserve secret. |

---

## Lines that survive scrutiny

- **"Modelled"** — the Reserve Pay block, and merchant settlement (Route is the
  missing real piece).
- **"Real"** — Razorpay Orders, Checkout, Payment Links, HMAC verification, and
  every `order_...` / `pay_...` id on screen.
- **Don't say** "we bypass RBI 2FA". Say: consent is captured once at
  authorization; autonomous spend rides the e-mandate lane; anything beyond is refused.
