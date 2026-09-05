"""Ownership scoping — one reserve must never see or touch another's.

The UPI inbox used to be global and unauthenticated: every pending collect
request on the platform, with an approve button, in front of anyone who loaded
the page. These checks exist so that cannot come back.
"""
import sys
sys.path.insert(0, r"C:\Users\HP\Desktop\agent-commerce")
from commerce_platform import db, registry, payments, auth, agent_runner

# Tests must never touch data/agent_commerce.db.
import tempfile, pathlib as _pl
db.DB_PATH = _pl.Path(tempfile.gettempdir()) / "agent_commerce_scope_test.db"
db.DB_PATH.unlink(missing_ok=True)
db.init_db()

ok = 0; fail = 0
def check(l, c):
    global ok, fail; print(f"  [{'PASS' if c else 'FAIL'}] {l}"); ok += c; fail += (not c)


def make_buyer(name, vpa, email):
    res = db.create_wallet("user", name, email, secret_hash=auth.hash_secret("x"),
                           upi_vpa=vpa, opening_balance=8_000_00)
    aid = db.register_agent(name, auth.hash_secret("x"), per_txn_limit=5_000_00,
                            daily_limit=10_000_00, monthly_limit=60_000_00,
                            payment_model="reserve", wallet_id=res)
    db.set_agent_passport(aid, registry.issue_passport(
        agent_id=aid, principal_name=name, principal_vpa=vpa,
        per_txn_cap=5_000_00, daily_cap=10_000_00,
        categories=["electronics"], afa_verified=True))
    return res, aid


mid = db.create_merchant("Scope Store", category="electronics", email="scope@store.test")
db.set_merchant_wallet(mid, db.create_wallet("merchant", "Scope Store"))
cheap = db.add_product(mid, "Scope Widget", "test item", "electronics", 500_00)
dear = db.add_product(mid, "Scope Flagship", "over cap", "electronics", 9_000_00)

ada_res, ada = make_buyer("Ada", "ada@okaxis", "ada_scope@test.com")
bob_res, bob = make_buyer("Bob", "bob@okaxis", "bob_scope@test.com")

print("=" * 66)
print("1. The UPI inbox is per-reserve")
print("=" * 66)
a_order = payments.pay_from_reserve(ada, cheap, 1)["order_id"]
b_order = payments.pay_from_reserve(bob, cheap, 1)["order_id"]

ada_inbox = [r["order_id"] for r in db.get_upi_requests(wallet_id=ada_res)]
bob_inbox = [r["order_id"] for r in db.get_upi_requests(wallet_id=bob_res)]
check("Ada sees her own request", a_order in ada_inbox)
check("Ada does NOT see Bob's request", b_order not in ada_inbox)
check("Bob sees his own request", b_order in bob_inbox)
check("Bob does NOT see Ada's request", a_order not in bob_inbox)

everything = [r["order_id"] for r in db.get_upi_requests()]
check("unscoped call still exists for the audit view", {a_order, b_order} <= set(everything))

print("\n" + "=" * 66)
print("2. Orders resolve to the reserve that funds them")
print("=" * 66)
# This is what the gateway uses to refuse a cross-reserve approve/decline.
check("Ada's order carries Ada's reserve", db.get_order(a_order)["wallet_id"] == ada_res)
check("Bob's order carries Bob's reserve", db.get_order(b_order)["wallet_id"] == bob_res)
check("the two do not collide", ada_res != bob_res)

print("\n" + "=" * 66)
print("3. Revoking is scoped, and bites a request already in the inbox")
print("=" * 66)
# Collect re-runs the passport gate at approval, so authority that lapsed after
# the request was raised still stops the money.
db.revoke_agent(bob)
declined = payments.complete_upi_request(b_order)
check("revoked agent cannot settle a pending request", not declined.get("success"))
check("refusal names revocation", declined.get("code") == "revoked")
check("Bob's reserve was not debited", db.get_wallet(bob_res)["balance"] == 8_000_00)
check("Ada is unaffected by Bob's revocation",
      payments.complete_upi_request(a_order).get("success") is True)
check("Ada's reserve debited exactly once", db.get_wallet(ada_res)["balance"] == 7_500_00)

print("\n" + "=" * 66)
print("4. The browser console runs the same gates as the ADK agent")
print("=" * 66)
# agent_runner is what /home drives. It must not be a softer path to money.
over = agent_runner._t_purchase(ada, {"product_id": dear})
check("over-cap purchase refused through the console", over.get("refused") is True)
check("refusal carries the stable code", over.get("code") == "per_txn_exceeded")

db.revoke_agent(ada)
after = agent_runner._t_purchase(ada, {"product_id": cheap})
check("revoked agent refused through the console", after.get("refused") is True)
check("console refusal names revocation", after.get("code") == "revoked")

print("\n" + "=" * 66)
print(f"  RESULT: {ok} passed, {fail} failed")
print("=" * 66)
sys.exit(1 if fail else 0)
