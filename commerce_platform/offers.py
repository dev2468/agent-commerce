"""Signed merchant offers — the seller's half of the credential.

The passport commits what a *buyer's agent* may spend. An offer commits what a
*merchant* will actually charge: a price, a validity window, an optional bundle.
Both are Ed25519-signed and both are re-verified in the money path, so the two
sides of a transaction are symmetric.

This buys three things at once:

  * **Discounts and bundles** a merchant controls, without a mutable `discount`
    column anyone with a token could edit.
  * **Price-drift protection.** An agent that searched at ₹1,995 and settles at
    ₹4,995 is the obvious attack. With an offer the price is cryptographically
    committed by the seller, so drift is detectable rather than trusted.
  * **Expiry.** A flash price cannot be replayed next week.

Each merchant gets its own keypair at onboarding. The private key never leaves
the server (this stands in for the merchant signing in their own backend); the
public key is served so anyone can verify an offer without trusting us.
"""

import time
import uuid

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from commerce_platform.registry import Decision, RegistryError, canonical_json

OFFER_SPEC = "aco-1"                # agent-commerce offer, v1
MAX_OFFER_MINUTES = 60 * 24         # a signed price may not outlive a day
MAX_DISCOUNT_BPS = 9_000            # refuse to sign away more than 90%
_OFFER_AMOUNTS = ("list_total", "offer_total")  # must be present integer paise


def new_keypair() -> tuple[str, str]:
    """(private_hex, public_hex) for a merchant."""
    k = Ed25519PrivateKey.generate()
    from cryptography.hazmat.primitives.serialization import (
        Encoding as E, NoEncryption, PrivateFormat,
    )
    priv = k.private_bytes(E.Raw, PrivateFormat.Raw, NoEncryption()).hex()
    pub = k.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    return priv, pub


def sign_offer(private_key_hex: str, merchant_id: str, merchant_name: str,
               items: list[dict], list_total: int, offer_total: int,
               valid_minutes: int = 30, note: str = "") -> dict:
    """Sign a price a merchant is committing to.

    `items` is [{product_id, name, qty, unit_price}] — a single product for a
    plain discount, several for a bundle. Amounts are integer paise; floats are
    refused before signing, same rule as the passport.
    """
    if not private_key_hex:
        raise RegistryError("no_signing_key", "This merchant has no signing key.")
    if not items:
        raise RegistryError("empty_offer", "An offer must contain at least one item.")
    for v in (list_total, offer_total):
        if isinstance(v, float) or not isinstance(v, int):
            raise RegistryError("float_amount", "Amounts must be integer paise.")
    if offer_total <= 0 or list_total <= 0:
        raise RegistryError("bad_amount", "Totals must be positive.")
    if offer_total > list_total:
        raise RegistryError("not_a_discount", "Offer total exceeds the list total.")
    if valid_minutes <= 0 or valid_minutes > MAX_OFFER_MINUTES:
        raise RegistryError("bad_validity", f"Validity must be 1..{MAX_OFFER_MINUTES} minutes.")

    discount_bps = round((list_total - offer_total) * 10_000 / list_total)
    if discount_bps > MAX_DISCOUNT_BPS:
        raise RegistryError("discount_too_large", "Discount exceeds 90%.")

    now = int(time.time() * 1000)
    payload = {
        "spec": OFFER_SPEC,
        "offer_id": f"ofr_{uuid.uuid4().hex[:16]}",
        "merchant_id": merchant_id,
        "merchant_name": merchant_name,
        "items": [{
            "product_id": str(i["product_id"]),
            "name": str(i.get("name", "")),
            "qty": int(i.get("qty", 1)),
            "unit_price": int(i["unit_price"]),
        } for i in items],
        "list_total": int(list_total),
        "offer_total": int(offer_total),
        "discount_bps": int(discount_bps),
        "currency": "INR",
        "settlement": "upi",
        "note": str(note or "")[:120],
        "issued_at_ms": now,
        "expires_at_ms": now + valid_minutes * 60_000,
    }
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_key_hex))
    signature = key.sign(canonical_json(payload, _OFFER_AMOUNTS)).hex()
    pub = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    return {**payload, "signature": signature, "pubkey": pub}


def verify_offer(offer: dict, merchant_pubkey: str, now_ms: int | None = None) -> Decision:
    """Signature, spec, settlement and expiry. Same shape as verify_passport."""
    if not isinstance(offer, dict):
        return Decision(False, "malformed", "Offer is not an object.")
    sig = offer.get("signature")
    if not sig:
        return Decision(False, "no_signature", "Offer carries no signature.")
    if offer.get("spec") != OFFER_SPEC:
        return Decision(False, "bad_spec", "Unknown offer spec.")
    if offer.get("settlement") != "upi":
        return Decision(False, "not_upi", "Offer does not settle over UPI.")
    if not merchant_pubkey:
        return Decision(False, "unknown_merchant", "No public key for that merchant.")
    # Trusting the key inside the offer would let anyone mint their own — check
    # against the key the registry holds for that merchant.
    if offer.get("pubkey") and offer["pubkey"] != merchant_pubkey:
        return Decision(False, "wrong_key", "Offer was not signed by that merchant.")

    payload = {k: v for k, v in offer.items() if k not in ("signature", "pubkey")}
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(merchant_pubkey)).verify(
            bytes.fromhex(sig), canonical_json(payload, _OFFER_AMOUNTS))
    except (InvalidSignature, ValueError, TypeError):
        return Decision(False, "bad_signature", "Offer signature does not verify.")

    now = now_ms if now_ms is not None else int(time.time() * 1000)
    if now > int(offer.get("expires_at_ms") or 0):
        return Decision(False, "expired", "This offer has expired.")
    return Decision(True, "ok", "Offer verified.")


def offer_amount_for(offer: dict, product_id: str, quantity: int) -> int | None:
    """What this offer charges for that purchase, or None if it does not cover it.

    A single-item offer must match the product and quantity. A bundle is
    all-or-nothing: it prices the whole basket, so buying one line of it does
    not get the bundle price.
    """
    items = offer.get("items") or []
    if len(items) == 1:
        it = items[0]
        if it["product_id"] == product_id and int(it.get("qty", 1)) == int(quantity):
            return int(offer["offer_total"])
        return None
    return None  # bundles settle through the bundle path, not a single line
