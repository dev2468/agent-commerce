"""Order orchestrator — reserve-backed agent payments over UPI.

The buyer authorizes a reserve once (UPI PIN, in production via Razorpay UPI
Reserve Pay). The signed Agent Passport bounds what the agent may spend.

Two execution modes, both UPI:
  * "collect"  (DEFAULT) — the agent initiates and a collect request is pushed to
     the buyer's UPI app; the buyer approves it there, then it settles. This is the
     safe default: a human taps approve on their own phone for each purchase.
  * "autonomous" — settles straight from the pre-authorized reserve with no per-
     purchase prompt (the mandate lane), for callers that opt in.

The passport gate runs at initiation in BOTH modes, and again at settlement in
collect mode (defense in depth). In production settlement rides Razorpay Route,
splitting the debit to the merchant's linked account; here it is an atomic
reserve->merchant ledger transfer.
"""

import os
import sqlite3
import uuid

from commerce_platform import db, registry, razorpay_client, offers
from commerce_platform.policy import check_spend_guards


def _gate(agent: dict, amount: int, category: str):
    """Run the passport gate. Returns the registry Decision."""
    return registry.authorize_purchase(
        agent["passport"], amount, category,
        daily_spent_paise=db.get_daily_spend(agent["agent_id"]),
        revoked=(agent.get("status") != "active"))


def _settle(order_id: str, agent: dict, product: dict, amount: int) -> dict:
    """Move funds reserve -> merchant atomically and mark the order paid."""
    merchant = db.get_merchant(product["merchant_id"])
    merchant_wallet_id = merchant.get("wallet_id")
    if not merchant_wallet_id:
        merchant_wallet_id = db.create_wallet(
            owner_type="merchant", owner_name=merchant["name"],
            owner_email=merchant.get("email", ""))
        db.set_merchant_wallet(product["merchant_id"], merchant_wallet_id)

    transfer = db.wallet_transfer(
        agent["wallet_id"], merchant_wallet_id, amount,
        order_id=order_id, note=f"{product['name']} via {agent['agent_id']}")
    if not transfer["success"]:
        db.update_order(order_id, status="failed", failure_reason=transfer["error"])
        db.audit("reserve_payment_failed", agent["agent_id"], order_id=order_id, error=transfer["error"])
        return {"success": False, "error": transfer["error"], "code": transfer.get("code")}

    db.record_spend(agent["agent_id"], amount, order_id)
    db.update_order(order_id, status="paid", wallet_txn_id=transfer["txn_id"])
    db.audit("reserve_payment", agent["agent_id"], order_id=order_id, amount=amount,
             txn_id=transfer["txn_id"], merchant=product["merchant_name"])
    return {
        "success": True, "order_id": order_id, "status": "paid",
        "txn_id": transfer["txn_id"], "product": product["name"],
        "merchant": product["merchant_name"],
        "amount": amount / 100, "amount_display": f"₹{amount / 100:,.0f}",
        "reserve_balance": transfer["from_balance"],
        "reserve_balance_display": transfer["from_balance_display"],
    }


def pay_from_reserve(agent_id: str, product_id: str, quantity: int = 1,
                     mode: str = "collect", idempotency_key: str = "",
                     expected_amount: int | None = None,
                     offer: dict | None = None) -> dict:
    """Agent initiates a purchase. Default `collect` pushes a UPI approval request
    to the buyer; `autonomous` settles straight from the reserve.

    `idempotency_key` makes a retry return the original order instead of
    charging twice. `expected_amount` is the price the agent decided on — if the
    catalog has moved since, the purchase is refused rather than silently
    settling at the new price. `offer` is a merchant-signed price that overrides
    the list price once its signature and expiry check out.
    """
    if idempotency_key:
        prior = db.find_order_by_idempotency_key(idempotency_key)
        if prior:
            # Same request, already handled. Never create a second debit.
            return {"success": True, "order_id": prior["order_id"],
                    "status": prior["status"], "product": prior["product_name"],
                    "amount": prior["amount"] / 100,
                    "amount_display": f"₹{prior['amount'] / 100:,.0f}",
                    "idempotent_replay": True}

    product = db.get_product(product_id)
    if not product:
        return {"success": False, "error": "Product not found."}
    agent = db.get_agent(agent_id)
    if not agent:
        return {"success": False, "error": "Agent not found."}
    if not agent.get("wallet_id"):
        return {"success": False, "error": "This agent has no reserve linked to it."}
    if not agent.get("passport"):
        return {"success": False, "error": "This agent holds no Agent Passport."}
    if not db.get_wallet(agent["wallet_id"]):
        return {"success": False, "error": "Linked reserve not found."}

    if quantity < 1:
        return {"success": False, "error": "Quantity must be at least 1.",
                "code": "bad_quantity"}

    list_total = product["price"] * quantity
    total_amount = list_total

    # A merchant-signed offer may lower the price — never raise it.
    offer_id = ""
    if offer:
        pubkey = db.get_merchant_pubkey(product["merchant_id"])
        decision = offers.verify_offer(offer, pubkey)
        if not decision.allowed:
            db.audit("offer_rejected", agent_id, code=decision.code,
                     product_id=product_id)
            return {"success": False, "error": decision.reason, "code": decision.code}
        if offer.get("merchant_id") != product["merchant_id"]:
            return {"success": False, "error": "Offer is for a different merchant.",
                    "code": "offer_merchant_mismatch"}
        priced = offers.offer_amount_for(offer, product_id, quantity)
        if priced is None:
            return {"success": False, "error": "Offer does not cover this purchase.",
                    "code": "offer_not_applicable"}
        if priced > list_total:
            return {"success": False, "error": "Offer costs more than list price.",
                    "code": "offer_not_a_discount"}
        total_amount = priced
        offer_id = offer.get("offer_id", "")
        db.audit("offer_applied", agent_id, offer_id=offer_id,
                 list_total=list_total, offer_total=total_amount)

    # The price the agent decided on must be the price it pays. Without this an
    # agent can search at one price and settle at another, and the passport caps
    # would be measured against a number the agent never saw.
    if expected_amount is not None:
        if isinstance(expected_amount, float) or not isinstance(expected_amount, int):
            return {"success": False, "error": "expected_amount must be integer paise.",
                    "code": "float_amount"}
        if expected_amount != total_amount:
            db.audit("price_drift", agent_id, product_id=product_id,
                     expected=expected_amount, actual=total_amount)
            return {"success": False, "code": "price_changed",
                    "error": (f"Price changed since the agent chose it: expected "
                              f"₹{expected_amount / 100:,.0f}, now ₹{total_amount / 100:,.0f}.")}

    # 1. Passport gate — signature + RBI bounds, before anything is initiated.
    decision = _gate(agent, total_amount, product.get("category", ""))
    db.audit("passport_gate", agent_id, amount=total_amount, product_id=product_id,
             allowed=decision.allowed, code=decision.code, mode=mode)
    if not decision.allowed:
        return {"success": False, "error": decision.reason, "code": decision.code}

    # 2. Live spend guards the passport cannot carry (monthly aggregate, velocity).
    guards = check_spend_guards(agent_id, total_amount)
    db.audit("spend_guards", agent_id, amount=total_amount,
             allowed=guards.allowed, checks=guards.checks)
    if not guards.allowed:
        return {"success": False, "error": guards.reason, "code": "spend_guard"}

    order_id = f"ord_{uuid.uuid4().hex[:12]}"
    try:
        db.create_order(order_id=order_id, agent_id=agent_id, buyer_name=agent["buyer_name"],
                    merchant_id=product["merchant_id"], product_id=product_id,
                    product_name=product["name"], amount=total_amount,
                    quantity=quantity, payment_model="reserve",
                    wallet_id=agent["wallet_id"],
                        idempotency_key=idempotency_key, offer_id=offer_id,
                        list_amount=list_total)
    except sqlite3.IntegrityError:
        # Two identical requests raced past the lookup above. The unique index
        # is what actually enforces once-only; this turns the loser of the race
        # into the same replay answer instead of an error.
        prior = db.find_order_by_idempotency_key(idempotency_key)
        if not prior:
            raise
        return {"success": True, "order_id": prior["order_id"],
                "status": prior["status"], "product": prior["product_name"],
                "amount": prior["amount"] / 100,
                "amount_display": f"₹{prior['amount'] / 100:,.0f}",
                "idempotent_replay": True}

    # A real Razorpay Order for the purchase, in both modes. Failure to reach
    # Razorpay must never block the purchase — the gates have already run and the
    # ledger is our source of truth; we just lose the real-rail id.
    rzp = razorpay_client.create_purchase_order(
        total_amount, order_id, product["name"],
        product.get("merchant_name", ""), agent_id)
    if rzp.get("ok"):
        db.update_order(order_id, razorpay_order_id=rzp["razorpay_order_id"])

    if mode == "autonomous":
        result = _settle(order_id, agent, product, total_amount)
        result["razorpay_order_id"] = rzp.get("razorpay_order_id", "")
        result["razorpay_live"] = bool(rzp.get("ok"))
        return result

    # Default: push a UPI collect request to the buyer's app; settle on approval.
    # S2S UPI collect is not enabled on the test key, so the real object standing
    # behind this is a Payment Link the buyer can genuinely open and pay.
    reserve = db.get_wallet(agent["wallet_id"]) or {}
    # Payment links are created lazily. Razorpay test mode allows only 30 in
    # total, and minting one per purchase burns that quota on orders nobody ever
    # opens — then the link fails exactly when you are demoing. The Order above
    # is always real; the link is created on demand by ensure_payment_link().
    link = {}
    if os.getenv("RZP_AUTO_LINK", "").strip().lower() in ("1", "true", "yes"):
        link = _create_link(order_id, order_amount=total_amount, product=product,
                            agent=agent, reserve=reserve)

    db.update_order(order_id, status="awaiting_upi_approval")
    db.audit("upi_request_sent", agent_id, order_id=order_id,
             amount=total_amount, merchant=product["merchant_name"],
             razorpay_order_id=rzp.get("razorpay_order_id", ""),
             payment_link=link.get("short_url", ""))
    return {
        "success": True,
        "order_id": order_id,
        "status": "awaiting_upi_approval",
        "requires": "upi_approval",
        "product": product["name"],
        "merchant": product["merchant_name"],
        "amount": total_amount / 100,
        "amount_display": f"₹{total_amount / 100:,.0f}",
        "razorpay_order_id": rzp.get("razorpay_order_id", ""),
        "payment_link": link.get("short_url", ""),
        "payment_link_id": link.get("payment_link_id", ""),
        "notified": link.get("notified", {}),
        "razorpay_live": bool(rzp.get("ok")),
        "message": "Collect request sent to your UPI app — approve it to complete.",
    }


def complete_upi_request(order_id: str) -> dict:
    """The buyer approved the collect request in their UPI app — settle it now.
    The passport gate is re-run so an authority that lapsed between request and
    approval (expired, revoked, day cap now breached) still stops the payment."""
    order = db.get_order(order_id)
    if not order:
        return {"success": False, "error": "Order not found."}
    if order["status"] != "awaiting_upi_approval":
        return {"success": False, "error": f"Order is '{order['status']}', not awaiting approval."}

    agent = db.get_agent(order["agent_id"])
    product = db.get_product(order["product_id"])
    if not agent or not product:
        return {"success": False, "error": "Order references a missing agent or product."}

    decision = _gate(agent, order["amount"], product.get("category", ""))
    if not decision.allowed:
        db.update_order(order_id, status="failed", failure_reason=decision.reason)
        db.audit("passport_gate", order["agent_id"], order_id=order_id,
                 allowed=False, code=decision.code, stage="upi_approval")
        return {"success": False, "error": decision.reason, "code": decision.code}

    return _settle(order_id, agent, product, order["amount"])


def _create_link(order_id: str, order_amount: int, product: dict,
                 agent: dict, reserve: dict) -> dict:
    """Mint the Razorpay Payment Link for an order and store it.

    Notification goes only to the reserve's own owner, and only if they supplied
    a phone — being texted is theirs to consent to, and shopping does not imply it.
    """
    phone = (reserve.get("owner_phone") or "").strip()
    email = (reserve.get("owner_email") or agent.get("buyer_email") or "").strip()
    link = razorpay_client.create_collect_link(
        order_amount, order_id, product["name"], product.get("merchant_name", ""),
        agent.get("buyer_name", "Buyer"), email, reserve.get("upi_vpa", ""),
        buyer_phone=phone, notify=bool(phone or email))
    if link.get("ok"):
        db.update_order(order_id,
                        razorpay_payment_link_id=link["payment_link_id"],
                        razorpay_short_url=link["short_url"])
        db.audit("payment_link_created", agent.get("agent_id"), order_id=order_id,
                 link_id=link["payment_link_id"], notified=link.get("notified", {}))
    else:
        db.audit("payment_link_failed", agent.get("agent_id"), order_id=order_id,
                 reason=link.get("reason", "")[:200])
    return link


def ensure_payment_link(order_id: str) -> dict:
    """Return this order's payment link, creating it on first ask."""
    order = db.get_order(order_id)
    if not order:
        return {"ok": False, "error": "Order not found."}
    if order.get("razorpay_short_url"):
        return {"ok": True, "short_url": order["razorpay_short_url"],
                "payment_link_id": order.get("razorpay_payment_link_id", ""),
                "existing": True}
    agent = db.get_agent(order["agent_id"]) or {}
    product = db.get_product(order["product_id"]) or {}
    reserve = db.get_wallet(order.get("wallet_id") or agent.get("wallet_id", "")) or {}
    if not agent or not product:
        return {"ok": False, "error": "Order references a missing agent or product."}
    link = _create_link(order_id, order["amount"], product, agent, reserve)
    if not link.get("ok"):
        return {"ok": False, "error": link.get("reason", "Razorpay refused the link.")}
    return {"ok": True, "short_url": link["short_url"],
            "payment_link_id": link["payment_link_id"],
            "notified": link.get("notified", {}), "existing": False}


def settle_from_checkout(order_id: str, payment_id: str, signature: str) -> dict:
    """Complete an order the buyer really paid through Razorpay Checkout.

    Two independent proofs are required before a rupee moves, because the browser
    is not trusted: the HMAC signature (which only Razorpay can produce, since the
    key secret never leaves the server), and a server-side read of the payment
    confirming it is captured and for the right amount. A client POSTing
    "I paid" satisfies neither.
    """
    order = db.get_order(order_id)
    if not order:
        return {"success": False, "error": "Order not found."}
    if order["status"] == "paid":
        return {"success": True, "status": "paid", "order_id": order_id, "already": True}

    rzp_order_id = order.get("razorpay_order_id") or ""
    if not razorpay_client.verify_payment_signature(rzp_order_id, payment_id, signature):
        db.audit("checkout_bad_signature", order.get("agent_id"), order_id=order_id,
                 payment_id=payment_id)
        return {"success": False, "code": "bad_signature",
                "error": "That payment signature does not verify."}

    live = razorpay_client.fetch_payment(payment_id)
    if not live.get("ok") or not live.get("paid"):
        return {"success": False, "code": "not_captured",
                "error": f"Razorpay does not report that payment as captured "
                         f"({live.get('status') or live.get('reason')})."}
    if live.get("order_id") and live["order_id"] != rzp_order_id:
        return {"success": False, "code": "order_mismatch",
                "error": "That payment belongs to a different order."}
    if int(live.get("amount") or 0) != int(order["amount"]):
        return {"success": False, "code": "amount_mismatch",
                "error": "The amount paid does not match the order."}

    agent = db.get_agent(order["agent_id"])
    product = db.get_product(order["product_id"])
    if not agent or not product:
        return {"success": False, "error": "Order references a missing agent or product."}

    decision = _gate(agent, order["amount"], product.get("category", ""))
    if not decision.allowed:
        # They have already paid Razorpay. Refusing silently would strand real
        # money, so park it loudly — in production this is a refund.
        db.update_order(order_id, status="paid_unreconciled",
                        razorpay_payment_id=payment_id,
                        failure_reason=f"paid via checkout but refused: {decision.code}")
        db.audit("checkout_refused_after_payment", order["agent_id"], order_id=order_id,
                 code=decision.code, payment_id=payment_id)
        return {"success": False, "code": decision.code, "status": "paid_unreconciled",
                "error": f"Payment captured but the passport now refuses it "
                         f"({decision.code}). This needs a refund."}

    db.update_order(order_id, razorpay_payment_id=payment_id)
    result = _settle(order_id, agent, product, order["amount"])
    db.audit("settled_from_checkout", order["agent_id"], order_id=order_id,
             payment_id=payment_id, method=live.get("method", ""), vpa=live.get("vpa", ""))
    result.update({"payment_id": payment_id, "method": live.get("method", ""),
                   "payer_vpa": live.get("vpa", ""), "via": "razorpay_checkout"})
    return result


def razorpay_status(order_id: str) -> dict:
    """What Razorpay says about this order's payment link, read live.

    Proof-of-existence: the UI can show the payment's state as reported by
    Razorpay rather than only our own database, which is the difference between
    "we say it was sent" and "here it is on their servers".
    """
    order = db.get_order(order_id)
    if not order:
        return {"ok": False, "error": "Order not found."}
    link_id = order.get("razorpay_payment_link_id") or ""
    if not link_id:
        return {"ok": False, "error": "No Razorpay payment link on this order.",
                "local_status": order["status"]}
    live = razorpay_client.fetch_payment_link(link_id)
    return {**live, "order_id": order_id, "local_status": order["status"],
            "payment_link_id": link_id,
            "short_url": order.get("razorpay_short_url", "") or live.get("short_url", "")}


def reconcile_order(order_id: str) -> dict:
    """Settle an order that was actually paid on Razorpay.

    Until now only the simulated PIN pad could complete a collect request, so a
    buyer who really opened the rzp.io link and paid it left our ledger untouched.
    This closes that: if Razorpay reports the link paid, the money moved for real,
    and our side must follow.

    The passport gate still runs — but note the asymmetry. If it now refuses
    (revoked in the meantime, day cap breached), the buyer has *already paid*
    Razorpay. Refusing silently would strand their money, so the order is parked
    at `paid_unreconciled` and audited loudly: in production that is a refund,
    not a shrug.
    """
    order = db.get_order(order_id)
    if not order:
        return {"success": False, "error": "Order not found."}
    if order["status"] == "paid":
        return {"success": True, "status": "paid", "order_id": order_id,
                "already": True}

    live = razorpay_status(order_id)
    if not live.get("ok"):
        return {"success": False, "error": live.get("error", "Could not reach Razorpay.")}
    if not live.get("paid"):
        return {"success": False, "status": order["status"],
                "razorpay_status": live.get("status"),
                "error": "Razorpay has not recorded a payment for this link yet."}

    agent = db.get_agent(order["agent_id"])
    product = db.get_product(order["product_id"])
    if not agent or not product:
        return {"success": False, "error": "Order references a missing agent or product."}

    decision = _gate(agent, order["amount"], product.get("category", ""))
    if not decision.allowed:
        db.update_order(order_id, status="paid_unreconciled",
                        failure_reason=f"paid on Razorpay but refused locally: {decision.code}")
        db.audit("reconcile_refused_after_payment", order["agent_id"], order_id=order_id,
                 code=decision.code, payment_id=live.get("payment_id", ""),
                 amount=order["amount"])
        return {"success": False, "code": decision.code, "status": "paid_unreconciled",
                "error": (f"Razorpay took the payment but the passport now refuses it "
                          f"({decision.code}). This needs a refund.")}

    if live.get("payment_id"):
        db.update_order(order_id, razorpay_payment_id=live["payment_id"])
    result = _settle(order_id, agent, product, order["amount"])
    db.audit("reconciled_from_razorpay", order["agent_id"], order_id=order_id,
             payment_id=live.get("payment_id", ""))
    result["reconciled"] = True
    result["payment_id"] = live.get("payment_id", "")
    return result


def decline_upi_request(order_id: str, reason: str = "") -> dict:
    """The buyer declined the collect request in their UPI app."""
    order = db.get_order(order_id)
    if not order:
        return {"success": False, "error": "Order not found."}
    if order["status"] != "awaiting_upi_approval":
        return {"success": False, "error": f"Order is '{order['status']}', not awaiting approval."}
    # Declining in the UPI app should not leave a payable link alive on Razorpay.
    razorpay_client.cancel_collect_link(order.get("razorpay_payment_link_id", ""))
    db.update_order(order_id, status="declined", failure_reason=reason or "Declined in UPI app")
    db.audit("upi_request_declined", order["agent_id"], order_id=order_id, reason=reason)
    return {"success": True, "order_id": order_id, "status": "declined",
            "product": order["product_name"]}


def check_order_status(order_id: str) -> dict | None:
    """Read-only status of an order."""
    order = db.get_order(order_id)
    if not order:
        return None
    return {
        "order_id": order["order_id"],
        "status": order["status"],
        "product": order["product_name"],
        "amount": order["amount"] / 100,
        "amount_display": f"₹{order['amount'] / 100:,.0f}",
        "txn_id": order.get("wallet_txn_id", ""),
    }
