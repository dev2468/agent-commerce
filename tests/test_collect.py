"""UPI collect (default) vs autonomous, proven in the money path."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from commerce_platform import db, registry, payments, auth

# Tests must never touch data/agent_commerce.db — they create merchants and
# products, and running them repeatedly was what filled the demo catalog with
# "USB Cable" x13. Point the module at a scratch file first.
import tempfile, pathlib as _pl
db.DB_PATH = _pl.Path(tempfile.gettempdir()) / "agent_commerce_test.db"
db.DB_PATH.unlink(missing_ok=True)

db.init_db()
ok = 0; fail = 0
def check(l, c):
    global ok, fail; print(f"  [{'PASS' if c else 'FAIL'}] {l}"); ok += c; fail += (not c)

res = db.create_wallet("user", "Ada", "ada_col@test.com", secret_hash=auth.hash_secret("x"),
                       upi_vpa="ada@okaxis", opening_balance=8_000_00)
mid = db.create_merchant("Col Store", category="electronics", email="col@store.test")
db.set_merchant_wallet(mid, db.create_wallet("merchant", "Col Store"))
prod = db.add_product(mid, "USB Cable", "type-c", "electronics", 500_00)
aid = db.register_agent("Ada", auth.hash_secret("x"), per_txn_limit=5_000_00,
                        daily_limit=10_000_00, monthly_limit=60_000_00,
                        payment_model="reserve", wallet_id=res)
db.set_agent_passport(aid, registry.issue_passport(agent_id=aid, principal_name="Ada",
    principal_vpa="ada@okaxis", per_txn_cap=5_000_00, daily_cap=10_000_00,
    categories=["electronics"], afa_verified=True))
bal0 = db.get_wallet(res)["balance"]

print("=" * 60)
print("Default mode = collect (UPI-app approval)")
print("=" * 60)
r = payments.pay_from_reserve(aid, prod, 1)   # default mode
check("default status is awaiting_upi_approval", r.get("status") == "awaiting_upi_approval")
check("requires upi_approval", r.get("requires") == "upi_approval")
check("reserve NOT debited yet", db.get_wallet(res)["balance"] == bal0)
reqs = db.get_upi_requests()
check("request visible in UPI app queue", any(x["order_id"] == r["order_id"] for x in reqs))

print("\n" + "=" * 60)
print("Buyer approves in the UPI app -> settles")
print("=" * 60)
done = payments.complete_upi_request(r["order_id"])
check("approval settles -> paid", done.get("success") and done.get("status") == "paid")
check("reserve debited by ₹500", bal0 - db.get_wallet(res)["balance"] == 500_00)
check("no longer in UPI queue", not any(x["order_id"] == r["order_id"] for x in db.get_upi_requests()))

print("\n" + "=" * 60)
print("Decline path")
print("=" * 60)
r2 = payments.pay_from_reserve(aid, prod, 1)
bal_before = db.get_wallet(res)["balance"]
dec = payments.decline_upi_request(r2["order_id"], "not now")
check("decline -> declined", dec.get("status") == "declined")
check("reserve unchanged after decline", db.get_wallet(res)["balance"] == bal_before)

print("\n" + "=" * 60)
print("Autonomous mode still works (opt-in, no prompt)")
print("=" * 60)
r3 = payments.pay_from_reserve(aid, prod, 1, mode="autonomous")
check("autonomous settles immediately -> paid", r3.get("success") and r3.get("status") == "paid")

print("\n" + "=" * 60)
print("Passport still gates collect mode (out-of-scope refused at initiation)")
print("=" * 60)
pf = db.add_product(mid, "Shirt", "cotton", "fashion", 200_00)
r4 = payments.pay_from_reserve(aid, pf, 1)
check("out-of-scope refused before any UPI request", (not r4.get("success")) and r4.get("code") == "out_of_scope")

print("\n" + "=" * 60)
print("Item > ₹10,000 routes as direct UPI collect (bypasses reserve debit)")
print("=" * 60)
prod_high = db.add_product(mid, "Flagship Phone", "pro max", "electronics", 25_000_00)
bal_before_high = db.get_wallet(res)["balance"]
m_wallet = db.get_merchant(mid)["wallet_id"]
m_bal_before = db.get_wallet(m_wallet)["balance"]

# High value item forces direct collect mode even if autonomous requested
r5 = payments.pay_from_reserve(aid, prod_high, 1, mode="autonomous")
check("high value routes to awaiting_upi_approval", r5.get("status") == "awaiting_upi_approval")
check("flagged as is_direct_upi", r5.get("is_direct_upi") is True)
check("payment_model is direct", r5.get("payment_model") == "direct")
check("reserve NOT debited at creation", db.get_wallet(res)["balance"] == bal_before_high)

# Buyer approves collect in UPI app
done5 = payments.complete_upi_request(r5["order_id"])
check("direct approval settles -> paid", done5.get("success") and done5.get("status") == "paid")
check("order via is direct_upi", done5.get("via") == "direct_upi")
check("reserve balance remains untouched", db.get_wallet(res)["balance"] == bal_before_high)
check("merchant wallet credited with full amount", db.get_wallet(m_wallet)["balance"] - m_bal_before == 25_000_00)

prod_high_fashion = db.add_product(mid, "Designer Bag", "leather", "fashion", 30_000_00)
r6 = payments.pay_from_reserve(aid, prod_high_fashion, 1)
check("high-value out-of-scope rejected", (not r6.get("success")) and r6.get("code") == "out_of_scope")

print("\n" + "=" * 60)
print(f"  RESULT: {ok} passed, {fail} failed")
print("=" * 60)
sys.exit(1 if fail else 0)
