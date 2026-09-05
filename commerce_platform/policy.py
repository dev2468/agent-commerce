"""Spend guards that the Agent Passport cannot express.

Division of responsibility — each rule lives in exactly one place:

  registry.authorize_purchase  ->  identity + signature, expiry, revocation,
                                   UPI-only settlement, per-transaction cap,
                                   daily cap, category scope
  policy.check_spend_guards    ->  monthly aggregate, velocity

The passport is a signed, offline-verifiable credential, so it can only carry
bounds that are true for its whole lifetime. Anything that depends on *live*
history — how much has been spent this month, how many orders in the last ten
minutes — has to be evaluated server-side at purchase time. That is this module.
"""

import time
from dataclasses import dataclass

from commerce_platform import db

VELOCITY_WINDOW_SECONDS = 600   # 10 minutes
VELOCITY_MAX_ORDERS = 5


@dataclass
class PolicyResult:
    allowed: bool
    reason: str
    checks: dict


def check_spend_guards(agent_id: str, amount: int) -> PolicyResult:
    """Monthly aggregate + velocity. Amount is in paise.

    Deliberately does NOT re-check per-transaction / daily / category — the
    passport gate owns those, and duplicating them here would let the two drift.
    """
    agent = db.get_agent(agent_id)
    if not agent:
        return PolicyResult(False, "Agent not found.", {"agent_exists": False})

    checks: dict = {}

    monthly_spent = db.get_monthly_spend(agent_id)
    checks["monthly_spent"] = monthly_spent / 100
    checks["monthly_ok"] = (monthly_spent + amount) <= agent["monthly_limit"]
    if not checks["monthly_ok"]:
        return PolicyResult(
            False,
            f"This would exceed the monthly limit of ₹{agent['monthly_limit']/100:,.0f} "
            f"(₹{monthly_spent/100:,.0f} spent this month).",
            checks,
        )

    conn = db._connect()
    try:
        recent = conn.execute(
            "SELECT COUNT(*) AS cnt FROM agent_spend WHERE agent_id = ? AND spent_at >= ?",
            (agent_id, time.time() - VELOCITY_WINDOW_SECONDS),
        ).fetchone()["cnt"]
    finally:
        conn.close()
    checks["recent_orders"] = recent
    checks["velocity_ok"] = recent < VELOCITY_MAX_ORDERS
    if not checks["velocity_ok"]:
        return PolicyResult(
            False,
            f"Too many orders in a short window ({recent} in the last 10 minutes). "
            "Wait a moment before ordering again.",
            checks,
        )

    checks["all_passed"] = True
    return PolicyResult(True, "Spend guards passed.", checks)


def get_budget(agent_id: str) -> dict | None:
    """The agent's current budget, for the check_budget tool."""
    agent = db.get_agent(agent_id)
    if not agent:
        return None
    daily_spent = db.get_daily_spend(agent_id)
    monthly_spent = db.get_monthly_spend(agent_id)
    rupees = lambda p: f"₹{p/100:,.0f}"
    return {
        "per_transaction_limit": rupees(agent["per_txn_limit"]),
        "daily_limit": rupees(agent["daily_limit"]),
        "daily_spent": rupees(daily_spent),
        "daily_remaining": rupees(max(0, agent["daily_limit"] - daily_spent)),
        "monthly_limit": rupees(agent["monthly_limit"]),
        "monthly_spent": rupees(monthly_spent),
        "monthly_remaining": rupees(max(0, agent["monthly_limit"] - monthly_spent)),
        "autonomy_mode": agent["autonomy_mode"],
        "status": agent["status"],
    }
