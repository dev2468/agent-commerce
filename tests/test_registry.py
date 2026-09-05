"""Attack matrix for the Agent Registry. Proves each RBI bound and each tamper
vector produces the exact refusal code — security by testing what FAILS."""
import copy
import sys
import time
sys.path.insert(0, r"C:\Users\HP\Desktop\agent-commerce")

from commerce_platform import registry as R

ok = 0; fail = 0
def check(label, cond):
    global ok, fail
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    ok += cond; fail += (not cond)

def issue(**kw):
    d = dict(agent_id="agent_x", principal_name="Ada", principal_vpa="ada@okaxis",
             per_txn_cap=5_000_00, daily_cap=20_000_00, categories=["electronics"],
             afa_verified=True)
    d.update(kw)
    return R.issue_passport(**d)

def expect_error(label, code, **kw):
    try:
        issue(**kw); check(f"{label} (expected {code})", False)
    except R.RegistryError as e:
        check(f"{label} -> {e.code}", e.code == code)

print("=" * 66)
print("1. Valid passport issues and verifies")
print("=" * 66)
p = issue()
check("issued with signature + pubkey", "signature" in p and "pubkey" in p)
check("verify_passport allows it", R.verify_passport(p).allowed)
check("reserve_cap pinned to ₹10,000", p["reserve_cap"] == 10_000_00)
check("settlement is upi", p["settlement"] == "upi")

print("\n" + "=" * 66)
print("2. Issuance guards (RBI bounds refused at the door)")
print("=" * 66)
expect_error("no AFA", "afa_required", afa_verified=False)
expect_error("per_txn over ₹15,000 e-mandate", "per_txn_over_emandate", per_txn_cap=16_000_00)
expect_error("daily under per_txn", "daily_under_txn", per_txn_cap=5_000_00, daily_cap=1_000_00)
expect_error("non-UPI principal id", "bad_vpa", principal_vpa="4111111111111111")
expect_error("float amount in cap", "amount_not_integer_paise", per_txn_cap=5000.5)
expect_error("validity over 90 days", "bad_validity", valid_days=120)

print("\n" + "=" * 66)
print("3. Tamper matrix (signature re-checked every time)")
print("=" * 66)
# raise the cap AFTER signing -> canonical bytes change -> signature fails
t = copy.deepcopy(p); t["per_txn_cap"] = 50_000_00
check("raised cap post-sign -> bad_signature", R.verify_passport(t).code == "bad_signature")
# flip a byte of the signature
t2 = copy.deepcopy(p); t2["signature"] = ("0" if t2["signature"][0] != "0" else "1") + t2["signature"][1:]
check("mangled signature -> bad_signature", R.verify_passport(t2).code == "bad_signature")
# strip signature entirely
t3 = copy.deepcopy(p); t3.pop("signature")
check("no signature -> no_signature", R.verify_passport(t3).code == "no_signature")
# smuggle a float amount into a signed-looking passport
t4 = copy.deepcopy(p); t4["per_txn_cap"] = 5000.5
check("float amount -> malformed", R.verify_passport(t4).code == "malformed")

print("\n" + "=" * 66)
print("4. Expiry + revocation")
print("=" * 66)
past = int(time.time() * 1000) + 5 * 86_400_000  # 'now' 5 days in the future
# a 3-day passport is expired 5 days out
short = issue(valid_days=3)
check("expired passport -> expired", R.verify_passport(short, now_ms=past).code == "expired")
check("revoked passport -> revoked", R.verify_passport(p, revoked=True).code == "revoked")

print("\n" + "=" * 66)
print("5. Purchase gate (scope, per-txn, daily)")
print("=" * 66)
check("in-scope, under caps -> ok",
      R.authorize_purchase(p, 3_000_00, "electronics", 0).allowed)
check("out-of-scope category -> out_of_scope",
      R.authorize_purchase(p, 3_000_00, "fashion", 0).code == "out_of_scope")
check("over per-txn cap -> per_txn_exceeded",
      R.authorize_purchase(p, 6_000_00, "electronics", 0).code == "per_txn_exceeded")
check("daily cap breach -> daily_exceeded",
      R.authorize_purchase(p, 5_000_00, "electronics", 18_000_00).code == "daily_exceeded")
check("float purchase amount -> amount_invalid",
      R.authorize_purchase(p, 3000.5, "electronics", 0).code == "amount_invalid")
check("negative amount -> amount_invalid",
      R.authorize_purchase(p, -100, "electronics", 0).code == "amount_invalid")

print("\n" + "=" * 66)
print("6. Defense-in-depth: a validly-signed non-UPI passport is still refused")
print("=" * 66)
# Sign a card-settlement payload with the REAL registry key (bypassing issue_passport)
import uuid
bad_payload = {k: v for k, v in p.items() if k not in ("signature", "pubkey")}
bad_payload["settlement"] = "card"
bad_payload["passport_id"] = f"agtp_{uuid.uuid4().hex[:16]}"
bad_sig = R._key().sign(R.canonical_json(bad_payload)).hex()
bad = {**bad_payload, "signature": bad_sig, "pubkey": R.public_key_hex()}
check("valid signature but settlement=card -> not_upi", R.verify_passport(bad).code == "not_upi")

print("\n" + "=" * 66)
print(f"  RESULT: {ok} passed, {fail} failed")
print("=" * 66)
sys.exit(1 if fail else 0)
