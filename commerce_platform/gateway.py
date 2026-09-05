"""Agent Gateway -- FastAPI REST API for agents and the buyer approval dashboard.

Agent endpoints require a JWT. Approval endpoints are buyer-facing (session-based
in the web UI, no JWT needed for the hackathon demo).
"""

import json
import os
import secrets
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

import requests

from commerce_platform import (
    db, registry, razorpay_client, agent_runner, url_intel, offers,
)
from commerce_platform.auth import (
    validate_token, verify_secret, issue_token, hash_secret,
    issue_merchant_token, validate_merchant_token, issue_wallet_token,
)
from commerce_platform.policy import get_budget
from commerce_platform.payments import (
    check_order_status, pay_from_reserve, complete_upi_request, decline_upi_request,
    razorpay_status, reconcile_order, ensure_payment_link, settle_from_checkout,
)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="Agent Commerce Gateway", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def no_cache(request, call_next):
    """Demo server: never let the browser cache a page, stylesheet or script, so an
    edit is always what the next reload shows (no stale HTML calling dead endpoints)."""
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


# ── Request models ──

class AuthRequest(BaseModel):
    agent_id: str
    agent_secret: str

class SearchRequest(BaseModel):
    query: str = ""
    category: str = ""
    price_min: int = 0
    price_max: int = 0
    merchant_id: str = ""
    limit: int = 10

class OrderRequest(BaseModel):
    product_id: str
    quantity: int = 1
    mode: str = "collect"          # collect (UPI-app approval, default) | autonomous

class DeclineRequest(BaseModel):
    reason: str = ""


# ── Auth helpers ──

def _get_agent_id(authorization: str) -> str:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    payload = validate_token(authorization[7:])
    # A merchant token is signed with the same key, so it validates — it just has
    # no agent_id. Reject it here rather than letting a KeyError become a 500.
    if not payload or not payload.get("agent_id"):
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    agent = db.get_agent(payload["agent_id"])
    if not agent or agent["status"] != "active":
        raise HTTPException(status_code=403, detail="Agent is not active")
    return payload["agent_id"]


def _get_merchant_id(authorization: str) -> str:
    """Merchant-scoped equivalent of _get_agent_id.

    An agent JWT fails here because validate_merchant_token checks the scope
    claim, not just the signature.
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    payload = validate_merchant_token(authorization[7:])
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired merchant token")
    merchant = db.get_merchant(payload["merchant_id"])
    if not merchant:
        raise HTTPException(status_code=403, detail="Merchant is not active")
    return payload["merchant_id"]


# ── Agent endpoints (JWT required) ──

@app.post("/agent/v1/auth/token")
def authenticate(req: AuthRequest):
    agent = db.get_agent(req.agent_id)
    if not agent:
        raise HTTPException(status_code=401, detail="Agent not found")
    if not verify_secret(req.agent_secret, agent["agent_secret_hash"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if agent["status"] != "active":
        raise HTTPException(status_code=403, detail=f"Agent is {agent['status']}")
    token = issue_token(req.agent_id, agent["buyer_name"])
    db.audit("agent_authenticated", req.agent_id)
    return {"token": token, "expires_in": 1800, "agent_id": req.agent_id}


@app.post("/agent/v1/search")
def search_products(req: SearchRequest, authorization: str = Header("")):
    agent_id = _get_agent_id(authorization)
    results = db.search_products(
        query=req.query, category=req.category,
        price_max=req.price_max, price_min=req.price_min,
        merchant_id=req.merchant_id, limit=req.limit,
    )
    db.audit("product_search", agent_id,
             query=req.query, category=req.category,
             price_max=req.price_max, result_count=len(results))
    return {"results": results, "count": len(results)}


@app.get("/agent/v1/products/{product_id}")
def get_product(product_id: str, authorization: str = Header("")):
    agent_id = _get_agent_id(authorization)
    product = db.get_product(product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    db.audit("product_viewed", agent_id, product_id=product_id)
    return product


@app.post("/agent/v1/orders")
def create_order(req: OrderRequest, authorization: str = Header("")):
    agent_id = _get_agent_id(authorization)
    # Default: a UPI collect request the buyer approves in their app. `mode=autonomous`
    # settles straight from the reserve (Single Block Multi Debit).
    mode = req.mode if req.mode in ("collect", "autonomous") else "collect"
    return pay_from_reserve(agent_id, req.product_id, req.quantity, mode=mode)


@app.get("/agent/v1/orders/{order_id}")
def get_order_status(order_id: str, authorization: str = Header("")):
    agent_id = _get_agent_id(authorization)
    result = check_order_status(order_id)
    if not result:
        raise HTTPException(status_code=404, detail="Order not found")
    db.audit("order_status_checked", agent_id, order_id=order_id)
    return result


@app.get("/agent/v1/budget")
def check_budget_endpoint(authorization: str = Header("")):
    agent_id = _get_agent_id(authorization)
    budget = get_budget(agent_id)
    if not budget:
        raise HTTPException(status_code=404, detail="Agent not found")
    db.audit("budget_checked", agent_id)
    return budget


@app.get("/agent/v1/audit")
def view_audit(limit: int = 20, authorization: str = Header("")):
    agent_id = _get_agent_id(authorization)
    entries = db.get_audit_log(agent_id=agent_id, limit=limit)
    return {"entries": entries, "count": len(entries)}


# ── Merchant endpoints (merchant-facing) ──

class ProductCreate(BaseModel):
    name: str
    description: str = ""
    category: str
    price: float
    attributes: dict = {}
    availability: str = "in_stock"

class ProductUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    category: str | None = None
    price: float | None = None
    attributes: dict | None = None
    availability: str | None = None


class MerchantOnboard(BaseModel):
    name: str
    contact_name: str = ""
    email: str = ""
    category: str = ""
    description: str = ""
    website: str = ""
    brand_color: str = "#2D81F7"
    razorpay_account: str = ""
    settlement: dict = {}
    products: list[ProductCreate] = []
    enable_wallet: bool = False   # wallet track: provision a payout wallet too


class MerchantLogin(BaseModel):
    merchant_id: str = ""
    email: str = ""
    merchant_secret: str


@app.post("/api/merchants/onboard")
def onboard_merchant(req: MerchantOnboard):
    """Full merchant onboarding: business profile, settlement, and opening catalog.

    Returns a one-time merchant_secret. Only the hash is stored, exactly like the
    agent registration path.
    """
    if not req.name or len(req.name.strip()) < 2:
        raise HTTPException(status_code=400, detail="Business name is required (at least 2 characters).")
    if not req.email or "@" not in req.email:
        raise HTTPException(status_code=400, detail="A valid email is required.")
    if db.find_merchant_by_email(req.email.strip()):
        raise HTTPException(status_code=409, detail="A merchant with that email already exists.")

    merchant_secret = secrets.token_urlsafe(24)
    merchant_id = db.create_merchant(
        name=req.name.strip(),
        category=req.category,
        description=req.description,
        website=req.website,
        razorpay_account=req.razorpay_account,
        merchant_secret_hash=hash_secret(merchant_secret),
        email=req.email.strip(),
        contact_name=req.contact_name.strip(),
        brand_color=req.brand_color or "#2D81F7",
        settlement=req.settlement,
    )

    created = []
    for p in req.products:
        pid = db.add_product(
            merchant_id=merchant_id,
            name=p.name, description=p.description,
            category=p.category or req.category,
            price=int(p.price * 100),
            attributes=p.attributes, availability=p.availability,
        )
        created.append(pid)
    db.refresh_merchant_product_count(merchant_id)

    wallet_id = ""
    if req.enable_wallet:
        wallet_id = db.create_wallet(
            owner_type="merchant", owner_name=req.name.strip(),
            owner_email=req.email.strip())
        db.set_merchant_wallet(merchant_id, wallet_id)

    db.audit("merchant_onboarded", None,
             merchant_id=merchant_id, name=req.name, products=len(created),
             wallet=bool(wallet_id))

    return {
        "merchant_id": merchant_id,
        "merchant_secret": merchant_secret,
        "name": req.name.strip(),
        "products_created": len(created),
        "wallet_id": wallet_id,
        "api_base": "/api/merchants",
    }


@app.post("/api/merchants/login")
def merchant_login(req: MerchantLogin):
    record = (db.get_merchant_auth(req.merchant_id) if req.merchant_id
              else db.find_merchant_by_email(req.email))
    if not record or not record.get("merchant_secret_hash"):
        raise HTTPException(status_code=401, detail="Merchant not found")
    if not verify_secret(req.merchant_secret, record["merchant_secret_hash"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not record.get("active", 1):
        raise HTTPException(status_code=403, detail="Merchant is not active")
    token = issue_merchant_token(record["merchant_id"], record["name"])
    db.audit("merchant_authenticated", None, merchant_id=record["merchant_id"])
    return {
        "token": token,
        "expires_in": 43200,
        "merchant_id": record["merchant_id"],
        "name": record["name"],
    }


@app.get("/api/merchant/me")
def merchant_me(authorization: str = Header("")):
    """Everything the dashboard needs in one call."""
    merchant_id = _get_merchant_id(authorization)
    return {
        **db.get_merchant(merchant_id),
        "stats": db.merchant_stats(merchant_id),
        "products": db.get_merchant_products(merchant_id),
        "orders": db.get_merchant_orders(merchant_id, limit=25),
    }


@app.post("/api/merchant/products")
def merchant_add_product(req: ProductCreate, authorization: str = Header("")):
    merchant_id = _get_merchant_id(authorization)
    pid = db.add_product(
        merchant_id=merchant_id,
        name=req.name, description=req.description, category=req.category,
        price=int(req.price * 100), attributes=req.attributes,
        availability=req.availability,
    )
    db.refresh_merchant_product_count(merchant_id)
    db.audit("product_added", None, merchant_id=merchant_id, product_id=pid, name=req.name)
    return {"product_id": pid, "name": req.name, "price": req.price}


@app.put("/api/merchant/products/{product_id}")
def merchant_update_product(product_id: str, req: ProductUpdate, authorization: str = Header("")):
    merchant_id = _get_merchant_id(authorization)
    product = db.get_product(product_id)
    if not product or product["merchant_id"] != merchant_id:
        raise HTTPException(status_code=404, detail="Product not found for this merchant")
    fields = {}
    for key in ("name", "description", "category", "availability"):
        v = getattr(req, key)
        if v is not None:
            fields[key] = v
    if req.price is not None:
        fields["price"] = int(req.price * 100)
    if req.attributes is not None:
        fields["attributes"] = json.dumps(req.attributes)
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")
    db.update_product(product_id, **fields)
    db.audit("product_updated", None, merchant_id=merchant_id,
             product_id=product_id, fields=list(fields.keys()))
    return {"product_id": product_id, "updated": list(fields.keys())}


@app.delete("/api/merchant/products/{product_id}")
def merchant_delete_product(product_id: str, authorization: str = Header("")):
    merchant_id = _get_merchant_id(authorization)
    product = db.get_product(product_id)
    if not product or product["merchant_id"] != merchant_id:
        raise HTTPException(status_code=404, detail="Product not found for this merchant")
    db.delete_product(product_id)
    db.refresh_merchant_product_count(merchant_id)
    db.audit("product_removed", None, merchant_id=merchant_id,
             product_id=product_id, name=product["name"])
    return {"deleted": product_id, "name": product["name"]}


@app.get("/api/merchants")
def list_merchants():
    merchants = db.list_merchants()
    return {"merchants": merchants, "count": len(merchants)}


@app.get("/api/merchants/{merchant_id}")
def get_merchant_detail(merchant_id: str):
    """Public storefront: profile plus catalog. Writes go through the
    token-authenticated /api/merchant/* routes, never through this path."""
    merchant = db.get_merchant(merchant_id)
    if not merchant:
        raise HTTPException(status_code=404, detail="Merchant not found")
    products = db.get_merchant_products(merchant_id)
    return {**merchant, "products": products}


# ── Catalog endpoints (public, for the demo UI) ──

@app.get("/api/products")
def list_products(query: str = "", category: str = "", limit: int = 20):
    results = db.search_products(query=query, category=category, limit=limit)
    return {"products": results, "count": len(results)}


@app.get("/api/products/{product_id}")
def get_product_public(product_id: str):
    product = db.get_product(product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return product


@app.get("/api/orders/{order_id}")
def get_order_public(order_id: str):
    result = check_order_status(order_id)
    if not result:
        raise HTTPException(status_code=404, detail="Order not found")
    return result


# ── Agent registration (web UI) ──

class AgentRegister(BaseModel):
    buyer_name: str
    buyer_email: str = ""
    platform: str = "custom"
    per_txn_limit: float = 10000
    daily_limit: float = 50000
    monthly_limit: float = 200000
    autonomy_mode: str = "confirm"
    category_interests: list[str] = []
    preferences: dict = {}
    wallet_id: str = ""               # the reserve this agent draws from (required)


@app.post("/api/agents/register")
def register_agent_endpoint(req: AgentRegister):
    if not req.buyer_name or len(req.buyer_name.strip()) < 2:
        raise HTTPException(status_code=400, detail="Name is required (at least 2 characters).")

    reserve = db.get_wallet(req.wallet_id) if req.wallet_id else None
    if not reserve or reserve["owner_type"] != "user":
        raise HTTPException(status_code=400, detail="A valid reserve is required — authorize one first.")
    if not reserve.get("upi_vpa"):
        raise HTTPException(status_code=400, detail="The reserve has no UPI id on file; a passport needs one.")

    per_txn = int(req.per_txn_limit * 100)
    daily = int(req.daily_limit * 100)

    # Issue the signed Agent Passport up front. If the requested authority is out
    # of RBI bounds the registry refuses here — the agent is never created.
    try:
        passport = registry.issue_passport(
            agent_id="pending", principal_name=req.buyer_name.strip(),
            principal_vpa=reserve["upi_vpa"], per_txn_cap=per_txn, daily_cap=daily,
            categories=req.category_interests, afa_verified=True)
    except registry.RegistryError as e:
        raise HTTPException(status_code=400, detail=e.message)

    agent_secret = secrets.token_urlsafe(24)
    agent_id = db.register_agent(
        buyer_name=req.buyer_name.strip(),
        agent_secret_hash=hash_secret(agent_secret),
        per_txn_limit=per_txn, daily_limit=daily,
        monthly_limit=int(req.monthly_limit * 100),
        buyer_email=req.buyer_email.strip(),
        platform=req.platform,
        autonomy_mode=req.autonomy_mode,
        category_allow=req.category_interests,
        payment_model="reserve",
        wallet_id=req.wallet_id,
    )
    # Re-issue with the real agent_id now that it exists, and store it.
    passport = registry.issue_passport(
        agent_id=agent_id, principal_name=req.buyer_name.strip(),
        principal_vpa=reserve["upi_vpa"], per_txn_cap=per_txn, daily_cap=daily,
        categories=req.category_interests, afa_verified=True)
    db.set_agent_passport(agent_id, passport)
    if req.preferences:
        db.update_agent_preferences(agent_id, req.preferences)
    db.audit("agent_registered_via_web", agent_id,
             buyer_name=req.buyer_name, platform=req.platform,
             passport_id=passport["passport_id"])
    return {
        "agent_id": agent_id,
        "agent_secret": agent_secret,
        "buyer_name": req.buyer_name.strip(),
        "wallet_id": req.wallet_id,
        "passport": passport,
        "api_base": "/agent/v1",
    }


# ── Agent Registry (verifiable passports) ──

class PassportVerify(BaseModel):
    passport: dict


@app.get("/api/registry/pubkey")
def registry_pubkey():
    """The registry's Ed25519 public key. A merchant verifies passports against
    this — no call back to us required."""
    return {"algorithm": "ed25519", "public_key": registry.public_key_hex(),
            "spec": registry.SPEC, "issuer": registry.ISSUER}


@app.get("/api/agents/{agent_id}/passport")
def agent_passport(agent_id: str):
    """An agent's passport, publicly readable — which is the point of it.

    A merchant has to be able to fetch and check a passport without holding any
    credential of ours. It carries caps, scope and the payer VPA, never a secret,
    and its signature is what makes it trustworthy rather than this endpoint.
    """
    agent = db.get_agent(agent_id)
    if not agent or not agent.get("passport"):
        raise HTTPException(status_code=404, detail="No passport issued for that agent.")
    return {"passport": agent["passport"], "status": agent.get("status", "active"),
            "revoked": agent.get("status") != "active"}


@app.get("/api/registry/sample")
def registry_sample():
    """A freshly-signed demo passport, for trying the verifier. Not persisted."""
    return registry.issue_passport(
        agent_id="agent_demo01", principal_name="Demo Buyer",
        principal_vpa="demo@okaxis", per_txn_cap=5_000_00, daily_cap=10_000_00,
        categories=["electronics", "books"], afa_verified=True)


@app.post("/api/registry/verify")
def registry_verify(req: PassportVerify):
    """Verify a passport's signature, settlement, expiry and revocation."""
    agent = db.get_agent((req.passport or {}).get("agent_id", "")) if req.passport else None
    revoked = bool(agent and agent.get("status") != "active")
    d = registry.verify_passport(req.passport, revoked=revoked)
    return {"valid": d.allowed, "code": d.code, "reason": d.reason}


# Revoking is owner-scoped — see /api/reserve/agents/{agent_id}/revoke. The
# unauthenticated version this replaced let anyone kill anyone's agent.


# ── UPI approval (collect requests the buyer approves in their UPI app) ──

def _order_reserve_id(order: dict) -> str:
    """Which reserve funds this order. Falls back through the agent so rows
    written before orders carried a wallet_id still resolve."""
    if order.get("wallet_id"):
        return order["wallet_id"]
    agent = db.get_agent(order.get("agent_id", "")) or {}
    return agent.get("wallet_id", "")


def _own_order_or_404(order_id: str, reserve_id: str) -> dict:
    order = db.get_order(order_id)
    if not order or _order_reserve_id(order) != reserve_id:
        # Not "forbidden" — a reserve should not be able to probe for the
        # existence of another reserve's orders.
        raise HTTPException(status_code=404, detail="Request not found.")
    return order


@app.get("/api/upi/requests")
def upi_requests(authorization: str = Header("")):
    """The signed-in reserve's own pending collect requests.

    A UPI inbox is per-person. This used to return every pending request on the
    platform to anyone who asked, which put one buyer's purchases — and an
    approve button for them — in front of another.
    """
    reserve_id = _get_reserve_id(authorization)
    reqs = db.get_upi_requests(wallet_id=reserve_id)
    return {"requests": reqs, "count": len(reqs),
            "reserve_id": reserve_id,
            "razorpay": razorpay_client.mode()}


@app.post("/api/upi/requests/{order_id}/approve")
def upi_approve(order_id: str, authorization: str = Header("")):
    reserve_id = _get_reserve_id(authorization)
    _own_order_or_404(order_id, reserve_id)
    result = complete_upi_request(order_id)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


class CheckoutVerify(BaseModel):
    razorpay_payment_id: str
    razorpay_order_id: str = ""
    razorpay_signature: str


@app.get("/api/orders/{order_id}/checkout")
def order_checkout(order_id: str):
    """Config for Razorpay Checkout on this order.

    Public by order id so the pay page works on a phone that holds no session —
    it exposes only the public key id and the amount, and paying still requires
    the buyer's own UPI PIN in their own app.
    """
    order = db.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found.")
    if not order.get("razorpay_order_id"):
        raise HTTPException(status_code=400, detail="No Razorpay order on this purchase.")
    if order["status"] == "paid":
        return {"already_paid": True, "order_id": order_id,
                "amount_display": f"₹{order['amount'] / 100:,.0f}",
                "product": order["product_name"]}
    merchant = db.get_merchant(order["merchant_id"]) or {}
    reserve = db.get_wallet(order.get("wallet_id", "")) or {}
    cfg = razorpay_client.checkout_config(
        order["razorpay_order_id"], order["amount"], order["product_name"],
        merchant.get("name", ""), reserve.get("owner_name", ""),
        reserve.get("owner_email", ""), reserve.get("owner_phone", ""))
    return {**cfg, "local_order_id": order_id, "product": order["product_name"],
            "merchant_name": merchant.get("name", ""),
            "amount_display": f"₹{order['amount'] / 100:,.0f}", "already_paid": False}


@app.post("/api/orders/{order_id}/verify-payment")
def order_verify_payment(order_id: str, req: CheckoutVerify):
    """Checkout succeeded in the browser — verify and settle.

    Unauthenticated by design: the proof is the HMAC signature, which only
    Razorpay can produce. A forged call fails on the signature, not on a session.
    """
    result = settle_from_checkout(order_id, req.razorpay_payment_id.strip(),
                                  req.razorpay_signature.strip())
    if not result.get("success"):
        raise HTTPException(status_code=400,
                            detail=f"{result.get('error')} ({result.get('code', 'refused')})")
    return result


@app.post("/api/upi/requests/{order_id}/link")
def upi_payment_link(order_id: str, authorization: str = Header("")):
    """Mint (or return) the real Razorpay link for this request, and notify the
    reserve owner if they gave a contact. Created on demand — Razorpay test mode
    allows only 30 payment links in total."""
    reserve_id = _get_reserve_id(authorization)
    _own_order_or_404(order_id, reserve_id)
    result = ensure_payment_link(order_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Could not create link."))
    return result


@app.get("/api/upi/requests/{order_id}/razorpay")
def upi_razorpay_status(order_id: str, authorization: str = Header("")):
    """Live proof from Razorpay for one collect request."""
    reserve_id = _get_reserve_id(authorization)
    _own_order_or_404(order_id, reserve_id)
    return razorpay_status(order_id)


@app.post("/api/upi/requests/{order_id}/reconcile")
def upi_reconcile(order_id: str, authorization: str = Header("")):
    """Settle an order the buyer actually paid on Razorpay.

    The simulated PIN pad is not the only way a collect request can complete —
    the payment link is real and payable from any UPI app.
    """
    reserve_id = _get_reserve_id(authorization)
    _own_order_or_404(order_id, reserve_id)
    result = reconcile_order(order_id)
    if not result.get("success"):
        raise HTTPException(status_code=409 if result.get("code") else 400,
                            detail=result.get("error", "Could not reconcile."))
    return result


@app.post("/api/upi/requests/{order_id}/decline")
def upi_decline(order_id: str, req: DeclineRequest = DeclineRequest(),
                authorization: str = Header("")):
    reserve_id = _get_reserve_id(authorization)
    _own_order_or_404(order_id, reserve_id)
    result = decline_upi_request(order_id, req.reason)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


# ── Reserve (Razorpay UPI Reserve Pay model) ──

class ReserveCreate(BaseModel):
    owner_name: str
    owner_email: str = ""
    reserve_secret: str = ""      # optional; generated if absent
    upi_vpa: str = ""
    owner_phone: str = ""      # notified when a collect request is raised
    opening_balance: float = 0    # rupees to reserve at creation (UPI PIN assumed done)


class ReserveLogin(BaseModel):
    reserve_id: str = ""
    email: str = ""
    reserve_secret: str


class ReserveTopup(BaseModel):
    amount: float                 # rupees


def _get_reserve_id(authorization: str) -> str:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    payload = validate_token(authorization[7:])
    if not payload or payload.get("scope") != "wallet" or not payload.get("wallet_id"):
        raise HTTPException(status_code=401, detail="Invalid or expired reserve token")
    if not db.get_wallet(payload["wallet_id"]):
        raise HTTPException(status_code=403, detail="Reserve not found")
    return payload["wallet_id"]


@app.post("/api/reserve/create")
def reserve_create(req: ReserveCreate):
    if not req.owner_name or len(req.owner_name.strip()) < 2:
        raise HTTPException(status_code=400, detail="Name is required (at least 2 characters).")
    if req.owner_email and db.find_wallet_by_email(req.owner_email.strip(), "user"):
        raise HTTPException(status_code=409, detail="A reserve with that email already exists.")

    reserve_secret = req.reserve_secret.strip() or secrets.token_urlsafe(18)
    opening = max(0, int(req.opening_balance * 100))
    if opening > db.RESERVE_CAP:
        raise HTTPException(status_code=400,
            detail="A single reserve tops out at ₹10,000 (Razorpay Reserve Pay ceiling).")

    reserve_id = db.create_wallet(
        owner_type="user", owner_name=req.owner_name.strip(),
        owner_email=req.owner_email.strip(), secret_hash=hash_secret(reserve_secret),
        upi_vpa=req.upi_vpa.strip(), opening_balance=opening,
        owner_phone=req.owner_phone.strip())
    db.audit("reserve_created", None, wallet_id=reserve_id, opening=opening)

    # A real Razorpay Order stands behind the block. The hold-and-draw-down
    # itself is modelled in our ledger — Single Block Multi Debit needs account
    # enablement — so this id is the honest part to show, not the whole story.
    block = razorpay_client.create_reserve_block(
        opening, req.owner_name.strip(), req.upi_vpa.strip(), reserve_id) if opening else {}
    if block.get("ok"):
        db.set_wallet_razorpay_order(reserve_id, block["razorpay_order_id"])
        db.audit("reserve_block_created", None, wallet_id=reserve_id,
                 razorpay_order_id=block["razorpay_order_id"])

    w = db.get_wallet(reserve_id)
    token = issue_wallet_token(reserve_id, req.owner_name.strip())
    return {
        "reserve_id": reserve_id,
        "reserve_secret": reserve_secret,
        "token": token,
        "reserve_cap": w["reserve_cap"],
        "balance": w["balance"],
        "balance_display": w["balance_display"],
        "razorpay_order_id": block.get("razorpay_order_id", ""),
        "razorpay_live": bool(block.get("ok")),
    }


@app.post("/api/reserve/login")
def reserve_login(req: ReserveLogin):
    record = (db.get_wallet_auth(req.reserve_id) if req.reserve_id
              else db.find_wallet_by_email(req.email, "user"))
    if not record or not record.get("secret_hash"):
        raise HTTPException(status_code=401, detail="Reserve not found")
    if not verify_secret(req.reserve_secret, record["secret_hash"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = issue_wallet_token(record["wallet_id"], record["owner_name"])
    return {"token": token, "reserve_id": record["wallet_id"], "name": record["owner_name"]}


@app.get("/api/reserve/me")
def reserve_me(authorization: str = Header("")):
    reserve_id = _get_reserve_id(authorization)
    w = db.get_wallet(reserve_id)
    return {**w, "ledger": db.get_wallet_ledger(reserve_id, limit=25)}


@app.post("/api/reserve/topup")
def reserve_topup_endpoint(req: ReserveTopup, authorization: str = Header("")):
    reserve_id = _get_reserve_id(authorization)
    amount = int(req.amount * 100)
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive.")
    result = db.wallet_topup(reserve_id, amount)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])
    db.audit("reserve_topup", None, wallet_id=reserve_id, amount=amount)
    return result


@app.get("/api/platform/mode")
def platform_mode():
    """What is really wired, so the UI can label itself honestly rather than
    implying a rail it does not have."""
    return {"razorpay": razorpay_client.probe(),
            "agent_brain": ("openrouter" if os.getenv("OPENROUTER_API_KEY", "").strip()
                            else "gemini" if os.getenv("GOOGLE_API_KEY", "").strip()
                            else "deterministic")}


@app.get("/api/reserve/home")
def reserve_home(authorization: str = Header("")):
    """Everything the signed-in user's home needs: the reserve, the agents drawing
    on it with their passports and live spend, order history, and the ledger."""
    reserve_id = _get_reserve_id(authorization)
    reserve = db.get_wallet(reserve_id)
    if not reserve:
        raise HTTPException(status_code=404, detail="Reserve not found.")

    agents = []
    for a in db.get_agents_for_wallet(reserve_id):
        budget = get_budget(a["agent_id"]) or {}
        passport = a.get("passport")
        agents.append({
            "agent_id": a["agent_id"], "label": a["buyer_name"],
            "platform": a.get("platform", ""), "status": a.get("status", "active"),
            "created_at": a.get("created_at"),
            "categories": a.get("category_allow", []),
            "passport": passport,
            "passport_id": (passport or {}).get("passport_id", ""),
            "budget": budget,
        })

    orders = db.get_orders_for_wallet(reserve_id)
    pending = [o for o in orders if o["status"] == "awaiting_upi_approval"]
    paid = [o for o in orders if o["status"] == "paid"]
    return {
        "reserve": {
            "reserve_id": reserve_id, "owner_name": reserve["owner_name"],
            "owner_email": reserve.get("owner_email", ""),
            "upi_vpa": reserve.get("upi_vpa", ""),
            "balance": reserve["balance"],
            "balance_display": f"₹{reserve['balance'] / 100:,.0f}",
            "razorpay_order_id": reserve.get("razorpay_order_id", ""),
            "cap": db.RESERVE_CAP, "cap_display": f"₹{db.RESERVE_CAP / 100:,.0f}",
        },
        "agents": agents,
        "orders": [{
            "order_id": o["order_id"], "product_name": o["product_name"],
            "merchant_name": o.get("merchant_name", ""),
            "amount": o["amount"], "amount_display": f"₹{o['amount'] / 100:,.0f}",
            "status": o["status"], "quantity": o.get("quantity", 1),
            "created_at": o.get("created_at"),
            "razorpay_order_id": o.get("razorpay_order_id", "") or "",
            "short_url": o.get("razorpay_short_url", "") or "",
            "agent_id": o.get("agent_id", ""),
        } for o in orders],
        "stats": {
            "pending": len(pending), "paid": len(paid), "orders": len(orders),
            "spent": sum(o["amount"] for o in paid),
            "spent_display": f"₹{sum(o['amount'] for o in paid) / 100:,.0f}",
        },
        "ledger": db.get_wallet_ledger(reserve_id, limit=25),
        "razorpay": razorpay_client.mode(),
    }


class AgentRun(BaseModel):
    agent_id: str
    message: str


@app.post("/api/reserve/agent/run")
def reserve_agent_run(req: AgentRun, authorization: str = Header("")):
    """Drive one of this reserve's agents from the browser.

    The same tool functions the ADK agent reaches over MCP, so a purchase made
    here runs the real passport gate and spend guards — a refusal comes back
    with the same stable code the attack-matrix tests assert on.
    """
    reserve_id = _get_reserve_id(authorization)
    agent = db.get_agent(req.agent_id)
    if not agent or agent.get("wallet_id") != reserve_id:
        raise HTTPException(status_code=404, detail="Agent not found on this reserve.")
    if not (req.message or "").strip():
        raise HTTPException(status_code=400, detail="Say what the agent should do.")
    result = agent_runner.run_agent(req.agent_id, req.message.strip())
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result


class CompareRequest(BaseModel):
    agent_id: str
    url: str


@app.post("/api/reserve/compare")
def reserve_compare(req: CompareRequest, authorization: str = Header("")):
    """Paste a product link; get the catalog's best answers to it.

    The link is a spec and a price benchmark, not a place we buy from. See
    url_intel for why fetching it is done defensively — the page is
    attacker-controlled, and this agent holds a payment mandate.
    """
    reserve_id = _get_reserve_id(authorization)
    agent = db.get_agent(req.agent_id)
    if not agent or agent.get("wallet_id") != reserve_id:
        raise HTTPException(status_code=404, detail="Agent not found on this reserve.")
    try:
        return url_intel.compare(req.url.strip(), agent_id=req.agent_id)
    except url_intel.UrlRefused as e:
        raise HTTPException(status_code=400, detail=e.reason)
    except requests.RequestException:
        raise HTTPException(status_code=400, detail="That page could not be fetched.")


class ChoiceBuy(BaseModel):
    agent_id: str
    product_id: str
    quantity: int = 1
    expected_amount: int | None = None     # integer paise the buyer was shown
    idempotency_key: str = ""
    autonomous: bool = True


@app.post("/api/reserve/buy")
def reserve_buy(req: ChoiceBuy, authorization: str = Header("")):
    """Buy the option the human picked from a comparison.

    `expected_amount` is what the buyer actually saw on the card. If the catalog
    moved since, this refuses rather than charging a price nobody agreed to.
    """
    reserve_id = _get_reserve_id(authorization)
    agent = db.get_agent(req.agent_id)
    if not agent or agent.get("wallet_id") != reserve_id:
        raise HTTPException(status_code=404, detail="Agent not found on this reserve.")
    result = pay_from_reserve(
        req.agent_id, req.product_id, max(1, req.quantity),
        mode="autonomous" if req.autonomous else "collect",
        idempotency_key=req.idempotency_key.strip(),
        expected_amount=req.expected_amount)
    if not result.get("success"):
        raise HTTPException(status_code=400,
                            detail=f"{result.get('error')} ({result.get('code', 'refused')})")
    return result


class OfferCreate(BaseModel):
    product_id: str
    offer_price: float                     # rupees the merchant will honour
    quantity: int = 1
    valid_minutes: int = 30
    note: str = ""


@app.post("/api/merchant/offers")
def merchant_create_offer(req: OfferCreate, authorization: str = Header("")):
    """A merchant signs a price it will honour — a discount an agent can verify.

    The signature is what makes it trustworthy: the price cannot be edited after
    the fact, by us or by the agent, and it expires on its own.
    """
    merchant_id = _get_merchant_id(authorization)
    product = db.get_product(req.product_id)
    if not product or product["merchant_id"] != merchant_id:
        raise HTTPException(status_code=404, detail="Product not found in your catalog.")
    qty = max(1, req.quantity)
    offer_total = int(round(req.offer_price * 100))
    list_total = product["price"] * qty
    priv, _pub = db.ensure_merchant_signing_key(merchant_id)
    merchant = db.get_merchant(merchant_id)
    try:
        offer = offers.sign_offer(
            priv, merchant_id, merchant["name"],
            items=[{"product_id": product["product_id"], "name": product["name"],
                    "qty": qty, "unit_price": product["price"]}],
            list_total=list_total, offer_total=offer_total,
            valid_minutes=req.valid_minutes, note=req.note)
    except registry.RegistryError as e:
        raise HTTPException(status_code=400, detail=f"{e.args[0]}: {e}")
    db.audit("offer_signed", None, merchant_id=merchant_id,
             offer_id=offer["offer_id"], offer_total=offer_total)
    return {"offer": offer}


@app.get("/api/merchants/{merchant_id}/offer-key")
def merchant_offer_key(merchant_id: str):
    """A merchant's offer-signing public key, so anyone can verify its offers
    without holding a credential of ours."""
    if not db.get_merchant(merchant_id):
        raise HTTPException(status_code=404, detail="Merchant not found.")
    return {"merchant_id": merchant_id, "offer_pubkey": db.get_merchant_pubkey(merchant_id)}


@app.post("/api/reserve/agents/{agent_id}/revoke")
def reserve_revoke_agent(agent_id: str, authorization: str = Header("")):
    """Emergency stop, scoped to the owner. The next purchase the passport gate
    sees is refused — including one already sitting in the UPI app, because
    collect re-runs the gate at approval."""
    reserve_id = _get_reserve_id(authorization)
    agent = db.get_agent(agent_id)
    if not agent or agent.get("wallet_id") != reserve_id:
        raise HTTPException(status_code=404, detail="Agent not found on this reserve.")
    if not db.revoke_agent(agent_id):
        raise HTTPException(status_code=404, detail="Agent not found.")
    db.audit("agent_revoked", agent_id, by="reserve_owner")
    return {"agent_id": agent_id, "status": "revoked"}


# ── Page routes ──

@app.get("/")
def serve_index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/user")
def serve_user():
    return FileResponse(str(STATIC_DIR / "user.html"))


@app.get("/pay/{order_id}")
def serve_pay(order_id: str):
    """Razorpay Checkout for one order — openable on a real phone."""
    return FileResponse(str(STATIC_DIR / "pay.html"))


@app.get("/home")
def serve_home():
    """Where a user lives after onboarding: their reserve, their agents and the
    console that actually drives one."""
    return FileResponse(str(STATIC_DIR / "home.html"))


@app.get("/merchant")
def serve_merchant():
    return FileResponse(str(STATIC_DIR / "merchant.html"))


@app.get("/agents")
def serve_agents():
    return FileResponse(str(STATIC_DIR / "agents.html"))


@app.get("/wallet")
def serve_wallet():
    return FileResponse(str(STATIC_DIR / "wallet.html"))


@app.get("/verify")
def serve_verify():
    return FileResponse(str(STATIC_DIR / "verify.html"))


@app.get("/upi")
def serve_upi():
    return FileResponse(str(STATIC_DIR / "upi.html"))


@app.get("/merchants")
def serve_merchants():
    return FileResponse(str(STATIC_DIR / "merchants.html"))


@app.get("/merchants/dashboard")
def serve_merchant_dashboard():
    return FileResponse(str(STATIC_DIR / "merchant-dashboard.html"))




# ── Static + health ──

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/health")
def health():
    return {"status": "ok", "service": "agent-commerce-gateway"}
