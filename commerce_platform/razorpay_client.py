"""Razorpay integration — the real rail under the registry.

What is genuinely live on a plain `rzp_test_*` key, and what is not, was probed
rather than assumed:

    POST /v1/orders          200  → used for the reserve block and every purchase
    POST /v1/payment_links   200  → used to make a collect request actually openable
    POST /v1/payments/create/upi   400  → S2S UPI collect is NOT enabled
    POST /v1/plans                 401  → subscriptions / native e-mandate NOT enabled

So the honest split, and the one this module implements:

  * **Real Razorpay objects.** Authorizing a reserve creates a real Order carrying
    the block amount. Every agent purchase creates a real Order, and a collect
    request creates a real Payment Link the buyer can actually open and pay.
    Those ids (`order_...`, `plink_...`) are returned by Razorpay, not minted here.

  * **Modelled on top.** *Single Block Multi Debit* (UPI Reserve Pay) and native
    e-mandate need account enablement, so the block-and-debit semantics — holding
    ₹10,000 and drawing it down across many purchases — are enforced in our own
    ledger against a real Order that represents the block. Say "modelled", never
    "integrated", about that specific part.

Every call degrades: if the network or the key fails, `ok=False` comes back with a
reason and the caller carries on in simulated mode. A demo must never die because
an upstream API blipped, and the UI is told which mode produced a given object so
it can label it truthfully.
"""

import hashlib
import hmac
import os
import threading
import time

import razorpay
import requests

_API = "https://api.razorpay.com/v1"

# Live vs simulated is decided once, lazily, by an actual round-trip — not by the
# mere presence of an env var, which proves nothing about whether the key works.
_probe_lock = threading.Lock()
_probed = False
_live = False
_mode_reason = "not probed yet"


def _creds() -> tuple[str, str] | None:
    kid = os.getenv("RAZORPAY_KEY_ID", "").strip()
    ks = os.getenv("RAZORPAY_KEY_SECRET", "").strip()
    return (kid, ks) if kid and ks else None


def _client() -> razorpay.Client | None:
    c = _creds()
    return razorpay.Client(auth=c) if c else None


def probe() -> dict:
    """One round-trip that decides live vs simulated. Cached for the process."""
    global _probed, _live, _mode_reason
    with _probe_lock:
        if _probed:
            return mode()
        _probed = True
        c = _creds()
        if not c:
            _live, _mode_reason = False, "RAZORPAY_KEY_ID / _KEY_SECRET not set"
            return mode()
        try:
            r = requests.get(f"{_API}/payments", auth=c, params={"count": 1}, timeout=8)
            if r.status_code == 200:
                _live, _mode_reason = True, "key authenticated against Razorpay"
            elif r.status_code in (401, 403):
                _live, _mode_reason = False, "Razorpay rejected the key"
            else:
                _live, _mode_reason = False, f"Razorpay returned {r.status_code}"
        except Exception as e:                                    # noqa: BLE001
            _live, _mode_reason = False, f"Razorpay unreachable ({type(e).__name__})"
        return mode()


def mode() -> dict:
    kid = os.getenv("RAZORPAY_KEY_ID", "")
    return {
        "live": _live,
        "reason": _mode_reason,
        # Only ever the public key id, and only its mode prefix — the secret never
        # leaves this process.
        "key_mode": ("test" if kid.startswith("rzp_test") else
                     "live" if kid.startswith("rzp_live") else "unknown"),
        "capabilities": {
            # Probed, not assumed. See the module docstring.
            "orders": _live,
            "payment_links": _live,
            "upi_s2s_collect": False,
            "emandate_subscriptions": False,
            "reserve_pay_block": False,
        },
    }


def _fail(reason: str) -> dict:
    return {"ok": False, "simulated": True, "reason": reason}


def create_reserve_block(amount_paise: int, owner_name: str, upi_vpa: str,
                         reserve_id: str) -> dict:
    """Create the real Razorpay Order that represents the reserve block.

    Real UPI Reserve Pay would place a lien on the payer's bank balance. That
    needs enablement, so what is real here is the Order carrying the block amount;
    the hold-and-draw-down is enforced in our ledger against it.
    """
    if not probe()["live"]:
        return _fail(_mode_reason)
    client = _client()
    try:
        order = client.order.create({
            "amount": int(amount_paise),
            "currency": "INR",
            "receipt": f"reserve_{reserve_id}"[:40],
            "notes": {
                "kind": "upi_reserve_block",
                "reserve_id": reserve_id,
                "owner": owner_name[:64],
                "upi_vpa": upi_vpa[:64],
                "model": "single_block_multi_debit (modelled)",
            },
        })
        return {"ok": True, "simulated": False,
                "razorpay_order_id": order["id"],
                "amount": order["amount"], "status": order["status"],
                "created_at": order.get("created_at")}
    except Exception as e:                                        # noqa: BLE001
        return _fail(f"{type(e).__name__}: {str(e)[:200]}")


def create_purchase_order(amount_paise: int, order_id: str, product_name: str,
                          merchant_name: str, agent_id: str) -> dict:
    """A real Razorpay Order for one agent purchase."""
    if not probe()["live"]:
        return _fail(_mode_reason)
    client = _client()
    try:
        order = client.order.create({
            "amount": int(amount_paise),
            "currency": "INR",
            "receipt": order_id[:40],
            "notes": {
                "kind": "agent_purchase",
                "order_id": order_id,
                "product": product_name[:80],
                "merchant": merchant_name[:64],
                "agent_id": agent_id,
                "settlement": "upi",
            },
        })
        return {"ok": True, "simulated": False,
                "razorpay_order_id": order["id"],
                "amount": order["amount"], "status": order["status"]}
    except Exception as e:                                        # noqa: BLE001
        return _fail(f"{type(e).__name__}: {str(e)[:200]}")


def _e164(phone: str) -> str:
    """Razorpay wants +91XXXXXXXXXX. A bare 10-digit Indian number is assumed."""
    digits = "".join(c for c in str(phone or "") if c.isdigit())
    if not digits:
        return ""
    if len(digits) == 10:
        return "+91" + digits
    if len(digits) == 12 and digits.startswith("91"):
        return "+" + digits
    return "+" + digits


def create_collect_link(amount_paise: int, order_id: str, product_name: str,
                        merchant_name: str, buyer_name: str, buyer_email: str,
                        upi_vpa: str, expire_minutes: int = 30,
                        buyer_phone: str = "", notify: bool = False) -> dict:
    """A real Razorpay Payment Link standing in for the UPI collect request.

    S2S UPI collect (which would push straight into the payer's UPI app) is not
    enabled on this key, so the closest real object is a Payment Link: an actual
    Razorpay-hosted UPI checkout the buyer can open and pay. The simulated phone
    UI approves the same order locally; this link is what makes it real.
    """
    if not probe()["live"]:
        return _fail(_mode_reason)
    c = _creds()
    # Razorpay rejects a link whose expiry is under ~15 minutes out.
    expire_by = int(time.time()) + max(expire_minutes, 16) * 60
    payload = {
        "amount": int(amount_paise),
        "currency": "INR",
        "accept_partial": False,
        "description": f"{product_name} · {merchant_name}"[:120],
        "reference_id": order_id[:40],
        "expire_by": expire_by,
        "customer": {"name": (buyer_name or "Buyer")[:60],
                     "email": buyer_email or "buyer@example.com",
                     **({"contact": _e164(buyer_phone)} if buyer_phone else {})},
        # Off by default: a demo must not text real people every time someone
        # clicks. Turned on per-reserve when the owner supplied their own
        # contact, which is the only case where notifying is theirs to consent to.
        "notify": {"sms": bool(notify and buyer_phone),
                   "email": bool(notify and buyer_email)},
        "reminder_enable": False,
        "notes": {"kind": "agent_upi_collect", "order_id": order_id,
                  "payer_vpa": upi_vpa[:64]},
    }
    try:
        r = requests.post(f"{_API}/payment_links", auth=c, json=payload, timeout=12)
        if r.status_code not in (200, 201):
            return _fail(f"payment_links {r.status_code}: {r.text[:180]}")
        d = r.json()
        return {"ok": True, "simulated": False,
                "payment_link_id": d.get("id", ""),
                "short_url": d.get("short_url", ""),
                "status": d.get("status", ""),
                # What Razorpay says it actually did, not what we asked for.
                "notified": d.get("notify", {}),
                "expire_by": d.get("expire_by")}
    except Exception as e:                                        # noqa: BLE001
        return _fail(f"{type(e).__name__}: {str(e)[:200]}")


def verify_payment_signature(razorpay_order_id: str, payment_id: str,
                             signature: str) -> bool:
    """Is this payment genuinely Razorpay's, for this order?

    Razorpay signs `order_id|payment_id` with the key secret. Checking it is what
    stops a browser simply POSTing "I paid" — the client cannot forge the HMAC
    without the secret, which never leaves the server. Compared in constant time.
    """
    c = _creds()
    if not (c and razorpay_order_id and payment_id and signature):
        return False
    expected = hmac.new(c[1].encode(), f"{razorpay_order_id}|{payment_id}".encode(),
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def fetch_payment(payment_id: str) -> dict:
    """Read a payment back — the authoritative check that money actually moved."""
    if not payment_id or not probe()["live"]:
        return _fail("no payment id or not live")
    try:
        r = requests.get(f"{_API}/payments/{payment_id}", auth=_creds(), timeout=12)
        if r.status_code != 200:
            return _fail(f"fetch {r.status_code}")
        d = r.json()
        return {"ok": True, "status": d.get("status", ""),
                "amount": d.get("amount", 0), "method": d.get("method", ""),
                "vpa": d.get("vpa", "") or (d.get("upi") or {}).get("vpa", ""),
                "order_id": d.get("order_id", ""),
                "captured": bool(d.get("captured")),
                "paid": d.get("status") in ("captured", "authorized")}
    except Exception as e:                                        # noqa: BLE001
        return _fail(f"{type(e).__name__}")


def checkout_config(razorpay_order_id: str, amount_paise: int, product: str,
                    merchant: str, name: str, email: str, phone: str) -> dict:
    """What the browser needs to open Razorpay Checkout.

    Only the *public* key id goes to the client. Checkout has none of the
    30-link test-mode cap that payment links do, and it is the integration a
    real merchant would ship.
    """
    return {
        "key_id": os.getenv("RAZORPAY_KEY_ID", ""),
        "order_id": razorpay_order_id,
        "amount": int(amount_paise),
        "currency": "INR",
        "name": merchant or "Agent Commerce",
        "description": product or "Agent purchase",
        "prefill": {"name": name or "", "email": email or "",
                    "contact": _e164(phone) if phone else ""},
        "method": {"upi": True},
        "live": _live,
    }


def cancel_collect_link(payment_link_id: str) -> dict:
    """Cancel a payment link when the buyer declines in the UPI app."""
    if not payment_link_id or not probe()["live"]:
        return _fail("no link or not live")
    c = _creds()
    try:
        r = requests.post(f"{_API}/payment_links/{payment_link_id}/cancel",
                          auth=c, timeout=10)
        return ({"ok": True, "status": r.json().get("status", "cancelled")}
                if r.status_code in (200, 201)
                else _fail(f"cancel {r.status_code}"))
    except Exception as e:                                        # noqa: BLE001
        return _fail(f"{type(e).__name__}")


def fetch_payment_link(payment_link_id: str) -> dict:
    """Read a link back — did the buyer actually pay it on Razorpay's side?"""
    if not payment_link_id or not probe()["live"]:
        return _fail("no link or not live")
    c = _creds()
    try:
        r = requests.get(f"{_API}/payment_links/{payment_link_id}", auth=c, timeout=10)
        if r.status_code != 200:
            return _fail(f"fetch {r.status_code}")
        d = r.json()
        payments = d.get("payments") or []
        return {"ok": True, "status": d.get("status", ""),
                "amount": d.get("amount", 0),
                "amount_paid": d.get("amount_paid", 0),
                "paid": d.get("status") == "paid" or (d.get("amount_paid") or 0) > 0,
                "notify": d.get("notify", {}),
                "customer": d.get("customer", {}),
                "payment_id": (payments[0].get("payment_id", "") if payments else ""),
                "created_at": d.get("created_at"),
                "short_url": d.get("short_url", "")}
    except Exception as e:                                        # noqa: BLE001
        return _fail(f"{type(e).__name__}")
