"""End-to-end: the passport is ACTIVE in the money path. Exercises the real
db + registry + payments functions the gateway calls."""
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

# reserve with a UPI id + ₹8,000
res = db.create_wallet("user", "Ada", "ada_live@test.com", secret_hash=auth.hash_secret("x"),
                       upi_vpa="ada@okaxis", opening_balance=8_000_00)

# merchant with an electronics product (₹100) and a fashion one (₹300)
mid = db.create_merchant("Live Store", category="electronics", email="live@store.test")
db.set_merchant_wallet(mid, db.create_wallet("merchant", "Live Store"))
p_elec = db.add_product(mid, "USB Cable", "type-c", "electronics", 100_00)
p_fash = db.add_product(mid, "T-Shirt", "cotton", "fashion", 300_00)
p_big  = db.add_product(mid, "Laptop", "16GB", "electronics", 8_000_00)  # over ₹5k per-txn

# agent scoped to electronics, ₹5,000 per-txn
aid = db.register_agent("Ada", auth.hash_secret("x"), per_txn_limit=5_000_00,
                        daily_limit=20_000_00, monthly_limit=1_00_000_00,
                        payment_model="reserve", wallet_id=res)
passport = registry.issue_passport(agent_id=aid, principal_name="Ada",
                                   principal_vpa="ada@okaxis", per_txn_cap=5_000_00,
                                   daily_cap=20_000_00, categories=["electronics"],
                                   afa_verified=True)
db.set_agent_passport(aid, passport)

print("=" * 62)
print("Passport is active in pay_from_reserve")
print("=" * 62)
# autonomous mode so this asserts the full settle path; the collect default
# (UPI-app approval) is covered end-to-end in test_collect.py
r = payments.pay_from_reserve(aid, p_elec, 1, mode="autonomous")
check("in-scope electronics under cap -> paid", r["success"] and r["status"] == "paid")

r = payments.pay_from_reserve(aid, p_fash, 1)
check("out-of-scope fashion -> refused out_of_scope", (not r["success"]) and r.get("code") == "out_of_scope")

r = payments.pay_from_reserve(aid, p_big, 1)
check("over ₹5,000 per-txn -> refused per_txn_exceeded", (not r["success"]) and r.get("code") == "per_txn_exceeded")

# tamper the stored passport: raise the cap, save, try again
tampered = dict(passport); tampered["per_txn_cap"] = 50_000_00
db.set_agent_passport(aid, tampered)
r = payments.pay_from_reserve(aid, p_big, 1)
check("tampered stored passport -> refused bad_signature", (not r["success"]) and r.get("code") == "bad_signature")
db.set_agent_passport(aid, passport)  # restore

# revoke the agent
db.revoke_agent(aid)
r = payments.pay_from_reserve(aid, p_elec, 1)
check("revoked agent -> refused revoked", (not r["success"]) and r.get("code") == "revoked")

print("=" * 62)
print(f"  RESULT: {ok} passed, {fail} failed")
print("=" * 62)
sys.exit(1 if fail else 0)
