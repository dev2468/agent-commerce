"""Seed script — loads test merchants, products, and a demo agent into the DB.

    venv\\Scripts\\python.exe -m commerce_platform.seed            # add to what's there
    venv\\Scripts\\python.exe -m commerce_platform.seed --reset    # wipe first

Use `--reset` before a demo. Clicking through the flows leaves the catalog full of
"USB Cable" x13 and half-finished "Test Store" merchants, and an agent searching
that looks broken even though it is working exactly as designed.

Seeded merchants get a real `merchant_secret_hash`, so they can sign into the
merchant dashboard. Without it the only merchants you can actually demo are ones
you onboard by hand in the browser, whose catalogs are empty.
"""

import json
import secrets
import sys
from pathlib import Path

from commerce_platform import db
from commerce_platform.auth import hash_secret

CATALOG_FILE = Path(__file__).parent / "catalog_data" / "merchants.json"


def purge() -> None:
    """Drop every row. Demo data only — there is nothing here worth keeping."""
    conn = db._connect()
    for table in ("wallet_ledger", "wallets", "audit_log", "agent_spend",
                  "orders", "products", "merchants", "agents"):
        try:
            conn.execute(f"DELETE FROM {table}")
        except Exception:                                          # noqa: BLE001
            pass  # table may not exist on an older file
    conn.commit()
    conn.close()
    print("  Purged merchants, products, agents, orders, reserves and ledger.")


def seed_catalog() -> list[tuple[str, str, str]]:
    with open(CATALOG_FILE) as f:
        merchants = json.load(f)

    creds = []
    for m in merchants:
        merchant_secret = secrets.token_urlsafe(18)
        mid = db.create_merchant(
            name=m["name"],
            category=m.get("category", ""),
            rating=m.get("rating", 0),
            description=m.get("description", ""),
            website=m.get("website", ""),
            razorpay_account=m.get("razorpay_account", ""),
            merchant_secret_hash=hash_secret(merchant_secret),
            email=m.get("email", f"{m['name'].lower().replace(' ', '')}@example.com"),
            contact_name=m.get("contact_name", "Store Manager"),
        )
        # Every merchant needs a payout account for settlement to land somewhere.
        wid = db.create_wallet(owner_type="merchant", owner_name=m["name"])
        db.set_merchant_wallet(mid, wid)
        creds.append((m["name"], mid, merchant_secret))
        print(f"  Merchant: {m['name']} -> {mid}")
        for p in m.get("products", []):
            pid = db.add_product(
                merchant_id=mid,
                name=p["name"],
                description=p.get("description", ""),
                category=p["category"],
                price=p["price"],
                attributes=p.get("attributes"),
                availability=p.get("availability", "in_stock"),
            )
            print(f"    Product: {p['name']} (Rs.{p['price']/100:,.2f}) -> {pid}")
        db.refresh_merchant_product_count(mid)
    return creds


def seed_demo_agent() -> str:
    agent_secret = secrets.token_urlsafe(24)
    agent_id = db.register_agent(
        buyer_name="Demo Buyer",
        agent_secret_hash=hash_secret(agent_secret),
        per_txn_limit=5_000_000,    # Rs.50,000 per transaction
        daily_limit=10_000_000,     # Rs.1,00,000 per day
        monthly_limit=50_000_000,   # Rs.5,00,000 per month
        buyer_email="demo@example.com",
        platform="gemini-adk",
        autonomy_mode="confirm",
    )
    print(f"\n  Demo Agent registered:")
    print(f"    agent_id:     {agent_id}")
    print(f"    agent_secret: {agent_secret}")
    print(f"    buyer:        Demo Buyer")
    print(f"    per-txn:      Rs.50,000")
    print(f"    daily:        Rs.1,00,000")
    print(f"    monthly:      Rs.5,00,000")
    print(f"    mode:         confirm")
    return agent_id, agent_secret


def main():
    reset = "--reset" in sys.argv
    print("Initializing database...")
    db.init_db()

    if reset:
        print("\nResetting...")
        purge()

    print("\nSeeding merchants and products...")
    creds = seed_catalog()

    print("\nRegistering demo agent...")
    agent_id, agent_secret = seed_demo_agent()

    print("\n" + "=" * 68)
    print("MERCHANT LOGINS — sign in at /merchant with either ID or email")
    print("=" * 68)
    for name, mid, secret in creds:
        print(f"  {name:<20} {mid}  {secret}")

    print("\n" + "=" * 68)
    print("STANDALONE AGENT (for agent/run.py — the browser console does not need it)")
    print("=" * 68)
    print(f"  AGENT_ID={agent_id}")
    print(f"  AGENT_SECRET={agent_secret}")
    print("\nNote: this agent has no reserve and no passport, so it cannot buy.")
    print("Create one that can at /user — that is the flow worth demoing.")
    if not reset:
        print("\nTip: re-running without --reset duplicates the catalog.")


if __name__ == "__main__":
    main()
