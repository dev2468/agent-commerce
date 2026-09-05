"""Agent authentication — secret hashing, JWT issuance and validation."""

import hashlib
import os
import time

import jwt
from dotenv import load_dotenv

load_dotenv()

JWT_SECRET = os.environ.get("JWT_SECRET", "dev-secret-change-me")
JWT_ALGORITHM = "HS256"
TOKEN_EXPIRY_SECONDS = 1800  # 30 minutes
MERCHANT_TOKEN_EXPIRY_SECONDS = 43200  # 12 hours — a dashboard session, not a bot call


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def verify_secret(secret: str, hashed: str) -> bool:
    return hash_secret(secret) == hashed


def issue_token(agent_id: str, buyer_name: str) -> str:
    payload = {
        "agent_id": agent_id,
        "buyer_name": buyer_name,
        "iat": int(time.time()),
        "exp": int(time.time()) + TOKEN_EXPIRY_SECONDS,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def issue_merchant_token(merchant_id: str, name: str) -> str:
    """Same signing key as agent tokens, but a distinct `scope` claim.

    The scope is what keeps the two apart: an agent JWT can never satisfy a
    merchant endpoint and vice versa, even though both validate.
    """
    payload = {
        "merchant_id": merchant_id,
        "name": name,
        "scope": "merchant",
        "iat": int(time.time()),
        "exp": int(time.time()) + MERCHANT_TOKEN_EXPIRY_SECONDS,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def issue_wallet_token(wallet_id: str, name: str) -> str:
    """Wallet-scoped session token. Same key, distinct `scope`, so it cannot be
    used on agent or merchant endpoints and vice versa."""
    payload = {
        "wallet_id": wallet_id,
        "name": name,
        "scope": "wallet",
        "iat": int(time.time()),
        "exp": int(time.time()) + MERCHANT_TOKEN_EXPIRY_SECONDS,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def validate_token(token: str) -> dict | None:
    """Returns decoded payload or None if invalid/expired."""
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None


def validate_merchant_token(token: str) -> dict | None:
    """Like validate_token, but rejects anything that is not merchant-scoped."""
    payload = validate_token(token)
    if not payload or payload.get("scope") != "merchant":
        return None
    return payload
