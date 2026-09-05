"""Agent Registry — verifiable Agent Passports with RBI-bounded, UPI-only authority.

The problem this solves: in agentic commerce a merchant has no way to know *who*
an agent is or *what it is allowed to spend*. Merchants register with Razorpay and
get a merchant id; nobody registers the agents. This module is the missing half —
an agent registers once and receives an **Agent Passport**: a signed credential that
states its identity, the human it acts for, and its scoped spending authority. A
merchant (or the settlement rail) verifies the signature locally and enforces the
bounds, trusting the registry's public key rather than any server call.

Design stance — deliberately stubborn:
  * Every authority bound is a *real RBI number*, enforced as a hard gate with a
    named refusal code. Nothing here is advisory.
  * Settlement is UPI, full stop. There is one allowed value and everything else
    is refused at construction time — there is no card / netbanking / wallet path.
  * The signed payload is canonicalised with INTEGER PAISE ONLY. A float anywhere
    in an amount is refused before a signature is ever produced, so a rounding
    attack cannot even be encoded, let alone signed.
  * The signature is re-verified on every purchase. A passport is never trusted
    because we issued it once — it is trusted because it verifies right now.

RBI grounding (see the pitch notes for citations):
  * UPI Reserve Pay — a single block tops out at ₹10,000. -> RESERVE_BLOCK_CAP
  * Digital-payments e-mandate — recurring debits up to ₹15,000/txn clear without
    a fresh factor; above that needs step-up. -> EMANDATE_PER_TXN_CAP
  * Reserve Pay token validity is up to 90 days. -> MAX_PASSPORT_DAYS
  * The authorization itself is the one additional-factor moment (UPI PIN). ->
    a passport cannot be issued without afa_verified.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding, PrivateFormat, PublicFormat, NoEncryption,
)

# ── RBI-bounded constants (paise) ──
RESERVE_BLOCK_CAP = 10_000_00      # UPI Reserve Pay: max single block ₹10,000
EMANDATE_PER_TXN_CAP = 15_000_00   # e-mandate AFA-exempt per-transaction ceiling
MAX_PASSPORT_DAYS = 90             # Reserve Pay token validity
SETTLEMENT = "upi"                 # the only settlement rail this registry issues
SPEC = "acp-1"                     # Agent Commerce Passport, v1
ISSUER = "razorpay-agent-registry"

_KEY_PATH = Path(__file__).resolve().parent.parent / "data" / "registry_ed25519.key"


# ── Errors ──

class RegistryError(ValueError):
    """Raised when a passport cannot be issued because the request is out of bounds.

    `code` is a stable machine-readable reason, safe to surface to the caller and
    to assert on in tests.
    """
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Decision:
    """The result of the enforcement gate. `allowed` plus a stable `code`."""
    allowed: bool
    code: str            # "ok" on allow; a refusal code otherwise
    reason: str


# ── Canonical serialization (integer-paise-only) ──

_AMOUNT_FIELDS = {"per_txn_cap", "daily_cap", "reserve_cap"}


def _no_floats_anywhere(node, path: str = "") -> None:
    """No float may appear anywhere in a signed payload, at any depth.

    The named-field check below only guards the fields it knows about. Nested
    money — an offer's items[].unit_price, say — would otherwise slip past.
    """
    if isinstance(node, float):
        raise RegistryError("amount_not_integer_paise",
                            f"{path or 'value'} is a float ({node!r}); use integer paise.")
    if isinstance(node, dict):
        for k, v in node.items():
            _no_floats_anywhere(v, f"{path}.{k}" if path else str(k))
    elif isinstance(node, (list, tuple)):
        for i, v in enumerate(node):
            _no_floats_anywhere(v, f"{path}[{i}]")


def _reject_floats(payload: dict, required: tuple[str, ...] = ()) -> None:
    """Refuse a payload where any amount is not a clean non-negative integer.

    Floats are the classic agentic-payment attack (₹99.999 -> rounds to ₹100 on
    one side, ₹99 on another). We refuse them at the door: an amount that is a
    bool, a float, negative, or non-integer never reaches the signer.
    """
    for f in required:
        v = payload.get(f)
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise RegistryError("amount_not_integer_paise",
                                f"{f} must be a non-negative integer in paise, got {v!r}.")
    _no_floats_anywhere(payload)


def canonical_json(payload: dict, required: tuple[str, ...] | None = None) -> bytes:
    """Deterministic bytes for signing: sorted keys, no whitespace, UTF-8.

    Runs the float guard first, so a non-integer amount can never be canonicalised.
    `required` names the fields that must be present integers — it defaults to the
    passport's money fields; other signed documents (merchant offers) pass their own.
    """
    _reject_floats(payload, _AMOUNT_FIELDS if required is None else required)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


# ── Registry signing key ──

def _load_or_create_key() -> Ed25519PrivateKey:
    _KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    if _KEY_PATH.exists():
        raw = bytes.fromhex(_KEY_PATH.read_text().strip())
        return Ed25519PrivateKey.from_private_bytes(raw)
    key = Ed25519PrivateKey.generate()
    raw = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    _KEY_PATH.write_text(raw.hex())
    return key


_signing_key: Ed25519PrivateKey | None = None


def _key() -> Ed25519PrivateKey:
    global _signing_key
    if _signing_key is None:
        _signing_key = _load_or_create_key()
    return _signing_key


def public_key_hex() -> str:
    """The registry's Ed25519 public key. A merchant verifies passports against
    this and nothing else — no call back to our server is required."""
    raw = _key().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return raw.hex()


# ── Passport issuance ──

def _clean_categories(categories: list[str] | None) -> list[str]:
    if not categories:
        return []
    out = sorted({str(c).strip().lower() for c in categories if str(c).strip()})
    return out


def issue_passport(agent_id: str, principal_name: str, principal_vpa: str,
                   per_txn_cap: int, daily_cap: int,
                   categories: list[str] | None = None,
                   afa_verified: bool = False,
                   valid_days: int = MAX_PASSPORT_DAYS) -> dict:
    """Mint a signed Agent Passport, or raise RegistryError with a stable code.

    Bounds enforced here (issuance is the first stubborn gate):
      * afa_verified must be True — no passport without the one-time UPI PIN factor
      * principal_vpa must look like a UPI handle (settlement is UPI-only)
      * per_txn_cap in (0, EMANDATE_PER_TXN_CAP]  — the e-mandate lane
      * daily_cap in [per_txn_cap, ...]           — a day cannot be under one txn
      * valid_days in (0, MAX_PASSPORT_DAYS]       — Reserve Pay token life
    """
    if not afa_verified:
        raise RegistryError("afa_required",
                            "Passport needs the one-time additional factor (UPI PIN) first.")
    if not agent_id:
        raise RegistryError("agent_required", "agent_id is required.")
    if not principal_name or len(principal_name.strip()) < 2:
        raise RegistryError("principal_required", "principal_name must be at least 2 characters.")

    vpa = (principal_vpa or "").strip().lower()
    # A UPI VPA is `handle@psp`, no spaces. This is the only settlement identifier
    # the registry accepts — there is deliberately no card / account field.
    if "@" not in vpa or " " in vpa or vpa.startswith("@") or vpa.endswith("@"):
        raise RegistryError("bad_vpa", "principal_vpa must be a UPI id like name@bank.")

    for name, v in (("per_txn_cap", per_txn_cap), ("daily_cap", daily_cap)):
        if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
            raise RegistryError("amount_not_integer_paise",
                                f"{name} must be a positive integer in paise.")
    if per_txn_cap > EMANDATE_PER_TXN_CAP:
        raise RegistryError("per_txn_over_emandate",
                            f"per_txn_cap ₹{per_txn_cap/100:,.0f} exceeds the ₹15,000 e-mandate ceiling.")
    if daily_cap < per_txn_cap:
        raise RegistryError("daily_under_txn",
                            "daily_cap cannot be below per_txn_cap.")
    if not isinstance(valid_days, int) or not (0 < valid_days <= MAX_PASSPORT_DAYS):
        raise RegistryError("bad_validity",
                            f"valid_days must be 1..{MAX_PASSPORT_DAYS} (Reserve Pay token life).")

    now_ms = int(time.time() * 1000)
    payload = {
        "spec": SPEC,
        "issuer": ISSUER,
        "passport_id": f"agtp_{uuid.uuid4().hex[:16]}",
        "agent_id": agent_id,
        "principal_name": principal_name.strip(),
        "principal_vpa": vpa,
        "settlement": SETTLEMENT,
        "per_txn_cap": int(per_txn_cap),
        "daily_cap": int(daily_cap),
        "reserve_cap": RESERVE_BLOCK_CAP,
        "categories": _clean_categories(categories),
        "kyc_tier": "min",
        "afa_verified": True,
        "issued_at_ms": now_ms,
        "expires_at_ms": now_ms + valid_days * 86_400_000,
    }
    signature = _key().sign(canonical_json(payload)).hex()
    return {**payload, "signature": signature, "pubkey": public_key_hex()}


# ── Verification + enforcement ──

def verify_passport(passport: dict, now_ms: int | None = None,
                    revoked: bool = False) -> Decision:
    """Verify a passport standalone: signature, settlement, expiry, revocation.

    Called on every purchase. A passport is only ever trusted because it verifies
    at this instant — never because it was once issued.
    """
    if not isinstance(passport, dict):
        return Decision(False, "malformed", "Passport is not an object.")
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)

    sig = passport.get("signature")
    if not isinstance(sig, str):
        return Decision(False, "no_signature", "Passport carries no signature.")

    # Re-derive the exact signed bytes: strip the two envelope fields and canonicalise.
    payload = {k: v for k, v in passport.items() if k not in ("signature", "pubkey")}
    try:
        message = canonical_json(payload)
    except RegistryError as e:
        # A float smuggled into an amount cannot even be canonicalised — refuse.
        return Decision(False, "malformed", e.message)

    try:
        _key().public_key().verify(bytes.fromhex(sig), message)
    except (InvalidSignature, ValueError):
        return Decision(False, "bad_signature", "Passport signature does not verify — tampered or forged.")

    if passport.get("settlement") != SETTLEMENT:
        return Decision(False, "not_upi", "Passport settlement is not UPI.")
    if revoked:
        return Decision(False, "revoked", "Passport has been revoked.")
    exp = passport.get("expires_at_ms", 0)
    if not isinstance(exp, int) or now_ms >= exp:
        return Decision(False, "expired", "Passport has expired.")

    return Decision(True, "ok", "Passport verified.")


def authorize_purchase(passport: dict, amount_paise: int, merchant_category: str,
                       daily_spent_paise: int, now_ms: int | None = None,
                       revoked: bool = False) -> Decision:
    """The full stubborn gate for one purchase. Order matters: identity/integrity
    first, then scope, then amount. Every branch returns a stable refusal code.

    Reserve *balance* is NOT checked here — that is the money layer's job (the
    atomic reserve debit). This gate decides whether the passport *authorizes*
    the attempt at all.
    """
    base = verify_passport(passport, now_ms=now_ms, revoked=revoked)
    if not base.allowed:
        return base

    # Amount must be a clean positive integer in paise — no float, no bool.
    if isinstance(amount_paise, bool) or not isinstance(amount_paise, int) or amount_paise <= 0:
        return Decision(False, "amount_invalid", "Amount must be a positive integer in paise.")

    cats = passport.get("categories") or []
    if cats and (merchant_category or "").strip().lower() not in cats:
        return Decision(False, "out_of_scope",
                        f"Merchant category '{merchant_category}' is outside this passport's scope.")

    if amount_paise > passport.get("per_txn_cap", 0):
        return Decision(False, "per_txn_exceeded",
                        f"₹{amount_paise/100:,.0f} exceeds the per-transaction cap "
                        f"₹{passport.get('per_txn_cap',0)/100:,.0f}.")

    if not isinstance(daily_spent_paise, int) or daily_spent_paise < 0:
        daily_spent_paise = 0
    if daily_spent_paise + amount_paise > passport.get("daily_cap", 0):
        remaining = max(0, passport.get("daily_cap", 0) - daily_spent_paise)
        return Decision(False, "daily_exceeded",
                        f"₹{amount_paise/100:,.0f} would breach today's cap "
                        f"(₹{remaining/100:,.0f} left).")

    return Decision(True, "ok", "Authorized.")
