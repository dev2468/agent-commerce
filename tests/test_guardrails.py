"""Idempotency, price drift, signed merchant offers, and SSRF.

Each of these is a way money moves that nobody authorized: paying twice for one
click, settling at a price the buyer never saw, honouring a discount nobody
signed, or making the server fetch its own internals.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from commerce_platform import db, registry, payments, auth, offers, url_intel

import tempfile, pathlib as _pl
db.DB_PATH = _pl.Path(tempfile.gettempdir()) / "agent_commerce_guard_test.db"
db.DB_PATH.unlink(missing_ok=True)
db.init_db()

ok = 0; fail = 0
def check(l, c):
    global ok, fail; print(f"  [{'PASS' if c else 'FAIL'}] {l}"); ok += c; fail += (not c)

res = db.create_wallet("user", "Ada", "ada_g@test.com", secret_hash=auth.hash_secret("x"),
                       upi_vpa="ada@okaxis", opening_balance=10_000_00)
mid = db.create_merchant("Guard Store", category="electronics", email="g@store.test")
db.set_merchant_wallet(mid, db.create_wallet("merchant", "Guard Store"))
prod = db.add_product(mid, "Guard Widget", "a thing", "electronics", 1_000_00)
aid = db.register_agent("Ada", auth.hash_secret("x"), per_txn_limit=5_000_00,
                        daily_limit=10_000_00, monthly_limit=60_000_00,
                        payment_model="reserve", wallet_id=res)
db.set_agent_passport(aid, registry.issue_passport(
    agent_id=aid, principal_name="Ada", principal_vpa="ada@okaxis",
    per_txn_cap=5_000_00, daily_cap=10_000_00,
    categories=["electronics"], afa_verified=True))

print("=" * 66)
print("1. Idempotency — a retry must not become a second debit")
print("=" * 66)
a = payments.pay_from_reserve(aid, prod, 1, mode="autonomous", idempotency_key="k-1")
b = payments.pay_from_reserve(aid, prod, 1, mode="autonomous", idempotency_key="k-1")
check("first call settles", a.get("status") == "paid")
check("retry returns the SAME order", a["order_id"] == b["order_id"])
check("retry is flagged as a replay", b.get("idempotent_replay") is True)
check("reserve debited exactly once", db.get_wallet(res)["balance"] == 9_000_00)
c = payments.pay_from_reserve(aid, prod, 1, mode="autonomous", idempotency_key="k-2")
check("a different key does create a new order", c["order_id"] != a["order_id"])
check("and debits again", db.get_wallet(res)["balance"] == 8_000_00)

print("\n" + "=" * 66)
print("2. Price drift — settle only at the price the agent decided on")
print("=" * 66)
good = payments.pay_from_reserve(aid, prod, 1, expected_amount=1_000_00)
check("matching expected_amount proceeds", good.get("success") is True)
stale = payments.pay_from_reserve(aid, prod, 1, expected_amount=200_00)
check("stale price refused", not stale.get("success"))
check("refusal code is price_changed", stale.get("code") == "price_changed")
check("a float amount is refused", payments.pay_from_reserve(
    aid, prod, 1, expected_amount=1000.0).get("code") == "float_amount")

print("\n" + "=" * 66)
print("3. Signed offers — a discount nobody can forge or edit")
print("=" * 66)
priv, pub = db.ensure_merchant_signing_key(mid)
offer = offers.sign_offer(priv, mid, "Guard Store",
                          items=[{"product_id": prod, "name": "Guard Widget",
                                  "qty": 1, "unit_price": 1_000_00}],
                          list_total=1_000_00, offer_total=600_00, valid_minutes=30)
check("offer verifies against the merchant key", offers.verify_offer(offer, pub).allowed)

tampered = {**offer, "offer_total": 10_00}
check("editing the price breaks the signature",
      offers.verify_offer(tampered, pub).code == "bad_signature")

other_priv, other_pub = offers.new_keypair()
forged = offers.sign_offer(other_priv, mid, "Guard Store",
                           items=[{"product_id": prod, "name": "Guard Widget",
                                   "qty": 1, "unit_price": 1_000_00}],
                           list_total=1_000_00, offer_total=500_00)
check("an offer signed by a stranger is refused",
      offers.verify_offer(forged, pub).code in ("wrong_key", "bad_signature"))

expired = offers.sign_offer(priv, mid, "Guard Store",
                            items=[{"product_id": prod, "name": "Guard Widget",
                                    "qty": 1, "unit_price": 1_000_00}],
                            list_total=1_000_00, offer_total=600_00, valid_minutes=1)
check("an expired offer is refused",
      offers.verify_offer(expired, pub, now_ms=expired["expires_at_ms"] + 1).code == "expired")

try:
    offers.sign_offer(priv, mid, "Guard Store",
                      items=[{"product_id": prod, "name": "W", "qty": 1, "unit_price": 1_000_00}],
                      list_total=1_000_00, offer_total=1_500_00)
    check("refuses to sign a price above list", False)
except registry.RegistryError as e:
    check("refuses to sign a price above list", e.code == "not_a_discount")

try:
    offers.sign_offer(priv, mid, "Guard Store",
                      items=[{"product_id": prod, "name": "W", "qty": 1, "unit_price": 1_000_00}],
                      list_total=1_000_00, offer_total=600.5)
    check("refuses a float total pre-sign", False)
except registry.RegistryError as e:
    check("refuses a float total pre-sign", e.code == "float_amount")

print("\n" + "=" * 66)
print("4. Offers in the money path")
print("=" * 66)
before = db.get_wallet(res)["balance"]
disc = payments.pay_from_reserve(aid, prod, 1, mode="autonomous", offer=offer)
check("a valid offer settles at the discounted price", disc.get("amount") == 600.0)
check("the reserve moved by the offer price, not list",
      before - db.get_wallet(res)["balance"] == 600_00)
check("forged offer refused in the money path",
      payments.pay_from_reserve(aid, prod, 1, offer=forged).get("code")
      in ("wrong_key", "bad_signature"))
check("tampered offer refused in the money path",
      payments.pay_from_reserve(aid, prod, 1, offer=tampered).get("code") == "bad_signature")
# The offer price is what the caps are measured against, so it must also be
# what expected_amount is compared to.
check("expected_amount is checked against the OFFER price",
      payments.pay_from_reserve(aid, prod, 1, mode="autonomous",
                                offer=offer, expected_amount=600_00).get("success") is True)

print("\n" + "=" * 66)
print("5. SSRF — a pasted link must not reach the inside")
print("=" * 66)
for bad, label in [("http://127.0.0.1:8000/admin", "loopback"),
                   ("http://169.254.169.254/latest/meta-data/", "cloud metadata"),
                   ("http://10.0.0.5/", "private range"),
                   ("http://[::1]/", "IPv6 loopback"),
                   ("file:///etc/passwd", "file scheme"),
                   ("gopher://evil/", "odd scheme")]:
    try:
        url_intel._check(bad)
        check(f"{label} blocked", False)
    except url_intel.UrlRefused:
        check(f"{label} blocked", True)
check("a public host is allowed", bool(url_intel._check("https://example.com/p/1")))
check("page titles become catalog keywords, not prose",
      url_intel.search_terms("Buy H&M Cotton Shirt Online in India | Best Price", "H&M")
      .startswith("h&m cotton"))

print("\n" + "=" * 66)
print(f"  RESULT: {ok} passed, {fail} failed")
print("=" * 66)
sys.exit(1 if fail else 0)
