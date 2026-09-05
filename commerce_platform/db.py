"""Database layer — catalog storage, agent registry, audit ledger.

All tables live in a single SQLite file at data/agent_commerce.db.
The audit_log table is append-only by convention (no UPDATE/DELETE in code).
"""

import json
import sqlite3
import time
import uuid
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "agent_commerce.db"


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# Columns added after the original schema shipped. Applied on every init_db()
# so an existing data/agent_commerce.db picks them up without being rebuilt.
_MIGRATIONS = [
    ("agents", "preferences", "TEXT DEFAULT '{}'"),
    ("merchants", "merchant_secret_hash", "TEXT DEFAULT ''"),
    ("merchants", "email", "TEXT DEFAULT ''"),
    ("merchants", "contact_name", "TEXT DEFAULT ''"),
    ("merchants", "brand_color", "TEXT DEFAULT '#2D81F7'"),
    ("merchants", "settlement", "TEXT DEFAULT '{}'"),
    ("merchants", "status", "TEXT DEFAULT 'active'"),
    # ── Wallet track (prefunded PPI model) ──
    ("agents", "payment_model", "TEXT DEFAULT 'reserve'"),
    ("agents", "wallet_id", "TEXT DEFAULT ''"),              # the reserve an agent draws from
    ("merchants", "wallet_id", "TEXT DEFAULT ''"),           # the merchant's payout account
    ("orders", "payment_model", "TEXT DEFAULT 'reserve'"),
    ("orders", "wallet_txn_id", "TEXT DEFAULT ''"),
    ("agents", "passport", "TEXT DEFAULT ''"),  # signed Agent Passport (JSON)
    # ── Real Razorpay objects ──
    # The Order behind the reserve block, and the Payment Link behind a collect
    # request, are created by Razorpay and their ids stored here. See
    # razorpay_client for exactly which parts are real vs modelled.
    ("wallets", "razorpay_order_id", "TEXT DEFAULT ''"),
    ("orders", "razorpay_payment_link_id", "TEXT DEFAULT ''"),
    ("orders", "razorpay_short_url", "TEXT DEFAULT ''"),
    ("orders", "wallet_id", "TEXT DEFAULT ''"),  # whose reserve funds it — scopes the UPI inbox
    # A retried purchase must not become a second debit.
    ("orders", "idempotency_key", "TEXT DEFAULT ''"),
    ("orders", "offer_id", "TEXT DEFAULT ''"),
    ("orders", "list_amount", "INTEGER DEFAULT 0"),   # pre-discount, for the receipt
    # Merchants sign their own offers; the private key stands in for the
    # merchant's own backend holding it.
    ("merchants", "offer_privkey", "TEXT DEFAULT ''"),
    ("merchants", "offer_pubkey", "TEXT DEFAULT ''"),
    # Needed to actually notify the payer when a collect request is raised.
    ("wallets", "owner_phone", "TEXT DEFAULT ''"),
]

# Reserve ceiling, in paise. Mirrors Razorpay UPI Reserve Pay's real cap: a single
# block tops out at ₹10,000. Enforced when the reserve is authorized / topped up.
RESERVE_CAP = 10_000_00


def _apply_migrations(conn: sqlite3.Connection) -> None:
    for table, column, decl in _MIGRATIONS:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        except sqlite3.OperationalError:
            pass  # column already there, or the table is about to be created with it


def init_db() -> None:
    conn = _connect()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS merchants (
        merchant_id          TEXT PRIMARY KEY,
        name                 TEXT NOT NULL,
        category             TEXT,
        description          TEXT DEFAULT '',
        logo_url             TEXT DEFAULT '',
        website              TEXT DEFAULT '',
        razorpay_account     TEXT DEFAULT '',
        rating               REAL DEFAULT 0,
        product_count        INTEGER DEFAULT 0,
        active               INTEGER DEFAULT 1,
        created_at           REAL NOT NULL,
        merchant_secret_hash TEXT DEFAULT '',
        email                TEXT DEFAULT '',
        contact_name         TEXT DEFAULT '',
        brand_color          TEXT DEFAULT '#2D81F7',
        settlement           TEXT DEFAULT '{}',  -- JSON: upi_vpa, cycle, methods
        status               TEXT DEFAULT 'active'
    );

    CREATE TABLE IF NOT EXISTS products (
        product_id   TEXT PRIMARY KEY,
        merchant_id  TEXT NOT NULL REFERENCES merchants(merchant_id),
        name         TEXT NOT NULL,
        description  TEXT,
        category     TEXT NOT NULL,
        price        INTEGER NOT NULL,  -- paise (INR * 100)
        currency     TEXT DEFAULT 'INR',
        availability TEXT DEFAULT 'in_stock',
        attributes   TEXT DEFAULT '{}',  -- JSON
        created_at   REAL NOT NULL,
        updated_at   REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_products_category ON products(category);
    CREATE INDEX IF NOT EXISTS idx_products_price ON products(price);
    CREATE INDEX IF NOT EXISTS idx_products_merchant ON products(merchant_id);

    CREATE TABLE IF NOT EXISTS agents (
        agent_id          TEXT PRIMARY KEY,
        agent_secret_hash TEXT NOT NULL,
        buyer_name        TEXT NOT NULL,
        buyer_email       TEXT,
        platform          TEXT DEFAULT 'custom',
        per_txn_limit     INTEGER NOT NULL,  -- paise
        daily_limit       INTEGER NOT NULL,  -- paise
        monthly_limit     INTEGER NOT NULL,  -- paise
        autonomy_mode     TEXT DEFAULT 'confirm',  -- auto | confirm | manual
        category_allow    TEXT DEFAULT '[]',  -- JSON list, empty = all
        preferences       TEXT DEFAULT '{}',  -- JSON: brands, payment methods, EMI, price mode
        status            TEXT DEFAULT 'active',  -- active | suspended | revoked
        created_at        REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS agent_spend (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id  TEXT NOT NULL REFERENCES agents(agent_id),
        amount    INTEGER NOT NULL,  -- paise
        order_id  TEXT,
        spent_at  REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS orders (
        order_id          TEXT PRIMARY KEY,
        agent_id          TEXT NOT NULL,
        buyer_name        TEXT NOT NULL,
        merchant_id       TEXT NOT NULL,
        product_id        TEXT NOT NULL,
        product_name      TEXT NOT NULL,
        quantity          INTEGER DEFAULT 1,
        amount            INTEGER NOT NULL,  -- paise
        status            TEXT DEFAULT 'created',
        razorpay_order_id TEXT,
        razorpay_payment_id TEXT,
        failure_reason    TEXT,
        created_at        REAL NOT NULL,
        updated_at        REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS audit_log (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp  REAL NOT NULL,
        event_type TEXT NOT NULL,
        agent_id   TEXT,
        details    TEXT NOT NULL  -- JSON
    );
    CREATE INDEX IF NOT EXISTS idx_audit_agent ON audit_log(agent_id);
    CREATE INDEX IF NOT EXISTS idx_audit_type ON audit_log(event_type);

    -- ── Wallet track ──
    -- One wallet per user or per merchant. Balance is the single source of truth,
    -- in paise; the ledger below is the append-only history that must always sum
    -- back to it.
    CREATE TABLE IF NOT EXISTS wallets (
        wallet_id    TEXT PRIMARY KEY,
        owner_type   TEXT NOT NULL,          -- user | merchant
        owner_name   TEXT NOT NULL,
        owner_email  TEXT DEFAULT '',
        secret_hash  TEXT DEFAULT '',        -- user wallets sign in with this
        balance      INTEGER NOT NULL DEFAULT 0,  -- paise
        kyc_tier     TEXT DEFAULT 'min',     -- min (₹10k) | full (₹2L)
        upi_vpa      TEXT DEFAULT '',
        status       TEXT DEFAULT 'active',
        created_at   REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_wallets_email ON wallets(owner_email);

    -- Append-only. Every credit/debit writes one row carrying the balance it
    -- produced, so the money math is auditable after the fact.
    CREATE TABLE IF NOT EXISTS wallet_ledger (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        txn_id        TEXT NOT NULL,
        wallet_id     TEXT NOT NULL REFERENCES wallets(wallet_id),
        direction     TEXT NOT NULL,         -- credit | debit
        amount        INTEGER NOT NULL,      -- paise, always positive
        balance_after INTEGER NOT NULL,      -- paise
        kind          TEXT NOT NULL,         -- topup | payment | payout
        counterparty  TEXT DEFAULT '',       -- other wallet id or label
        order_id      TEXT DEFAULT '',
        note          TEXT DEFAULT '',
        created_at    REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_ledger_wallet ON wallet_ledger(wallet_id);
    CREATE INDEX IF NOT EXISTS idx_ledger_txn ON wallet_ledger(txn_id);
    """)
    # Must run AFTER the tables exist. Running it first meant every ALTER hit a
    # missing table and was swallowed, so a brand-new database came up without
    # any migration column (merchants.wallet_id, agents.passport, ...) — broken
    # for anyone starting from an empty data/ directory.
    _apply_migrations(conn)
    # Must come after the migration that adds the column. Partial, so the many
    # orders with no key do not collide with each other — the uniqueness is what
    # makes a replayed purchase impossible rather than merely unlikely.
    try:
        conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_idem
                        ON orders(idempotency_key) WHERE idempotency_key != ''""")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()


# ── Audit ──

def audit(event_type: str, agent_id: str | None, **details) -> None:
    conn = _connect()
    conn.execute(
        "INSERT INTO audit_log (timestamp, event_type, agent_id, details) VALUES (?, ?, ?, ?)",
        (time.time(), event_type, agent_id, json.dumps(details)),
    )
    conn.commit()
    conn.close()


def get_audit_log(agent_id: str | None = None, limit: int = 50) -> list[dict]:
    conn = _connect()
    if agent_id:
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE agent_id = ? ORDER BY timestamp DESC LIMIT ?",
            (agent_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Merchants ──

def _merchant_public(row: sqlite3.Row) -> dict:
    """Row -> dict with the secret hash stripped and settlement parsed.

    Everything leaving this module for an HTTP response goes through here, so the
    hash cannot ride along in a GET /api/merchants/{id} body.
    """
    d = dict(row)
    d.pop("merchant_secret_hash", None)
    d["settlement"] = json.loads(d.get("settlement") or "{}")
    return d


def create_merchant(name: str, category: str = "", rating: float = 0,
                    description: str = "", website: str = "",
                    razorpay_account: str = "", merchant_secret_hash: str = "",
                    email: str = "", contact_name: str = "",
                    brand_color: str = "#2D81F7",
                    settlement: dict | None = None) -> str:
    mid = f"merch_{uuid.uuid4().hex[:12]}"
    conn = _connect()
    conn.execute(
        """INSERT INTO merchants
           (merchant_id, name, category, description, website, razorpay_account, rating,
            created_at, merchant_secret_hash, email, contact_name, brand_color, settlement)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (mid, name, category, description, website, razorpay_account, rating,
         time.time(), merchant_secret_hash, email, contact_name, brand_color,
         json.dumps(settlement or {})),
    )
    conn.commit()
    conn.close()
    return mid


def get_merchant(merchant_id: str) -> dict | None:
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM merchants WHERE merchant_id = ? AND active = 1", (merchant_id,)
    ).fetchone()
    conn.close()
    return _merchant_public(row) if row else None


def get_merchant_auth(merchant_id: str) -> dict | None:
    """Full row including the secret hash. Login path only."""
    conn = _connect()
    row = conn.execute("SELECT * FROM merchants WHERE merchant_id = ?", (merchant_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def find_merchant_by_email(email: str) -> dict | None:
    """Full row including the secret hash. Login path only."""
    if not email:
        return None
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM merchants WHERE lower(email) = lower(?) AND active = 1", (email,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def list_merchants(active_only: bool = True) -> list[dict]:
    conn = _connect()
    if active_only:
        rows = conn.execute(
            "SELECT * FROM merchants WHERE active = 1 ORDER BY rating DESC"
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM merchants ORDER BY rating DESC").fetchall()
    conn.close()
    return [_merchant_public(r) for r in rows]


def merchant_stats(merchant_id: str) -> dict:
    """Headline numbers for the merchant dashboard."""
    conn = _connect()
    products = conn.execute(
        "SELECT COUNT(*) AS c FROM products WHERE merchant_id = ?", (merchant_id,)
    ).fetchone()["c"]
    row = conn.execute(
        """SELECT COUNT(*) AS orders,
                  COALESCE(SUM(CASE WHEN status = 'paid' THEN amount ELSE 0 END), 0) AS revenue,
                  COALESCE(SUM(CASE WHEN status = 'pending_approval' THEN 1 ELSE 0 END), 0) AS pending
           FROM orders WHERE merchant_id = ?""",
        (merchant_id,),
    ).fetchone()
    conn.close()
    return {
        "products": products,
        "orders": row["orders"],
        "revenue": row["revenue"],  # paise
        "pending": row["pending"],
    }


def update_merchant(merchant_id: str, **fields) -> bool:
    conn = _connect()
    if not fields:
        return False
    sets = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [merchant_id]
    cursor = conn.execute(f"UPDATE merchants SET {sets} WHERE merchant_id = ?", vals)
    conn.commit()
    conn.close()
    return cursor.rowcount > 0


def get_merchant_products(merchant_id: str) -> list[dict]:
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM products WHERE merchant_id = ? ORDER BY created_at DESC",
        (merchant_id,),
    ).fetchall()
    conn.close()
    results = []
    for r in rows:
        d = dict(r)
        d["attributes"] = json.loads(d["attributes"])
        d["price_display"] = f"Rs.{d['price'] / 100:,.2f}"
        results.append(d)
    return results


def get_merchant_orders(merchant_id: str, limit: int = 50) -> list[dict]:
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM orders WHERE merchant_id = ? ORDER BY created_at DESC LIMIT ?",
        (merchant_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_product(product_id: str, **fields) -> bool:
    conn = _connect()
    if not fields:
        return False
    fields["updated_at"] = time.time()
    sets = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [product_id]
    cursor = conn.execute(f"UPDATE products SET {sets} WHERE product_id = ?", vals)
    conn.commit()
    conn.close()
    return cursor.rowcount > 0


def delete_product(product_id: str) -> bool:
    conn = _connect()
    cursor = conn.execute("DELETE FROM products WHERE product_id = ?", (product_id,))
    conn.commit()
    conn.close()
    return cursor.rowcount > 0


def refresh_merchant_product_count(merchant_id: str) -> None:
    conn = _connect()
    count = conn.execute(
        "SELECT COUNT(*) as cnt FROM products WHERE merchant_id = ?",
        (merchant_id,),
    ).fetchone()["cnt"]
    conn.execute(
        "UPDATE merchants SET product_count = ? WHERE merchant_id = ?",
        (count, merchant_id),
    )
    conn.commit()
    conn.close()


# ── Products ──

def add_product(merchant_id: str, name: str, description: str, category: str,
                price: int, attributes: dict | None = None, availability: str = "in_stock") -> str:
    pid = f"prod_{uuid.uuid4().hex[:12]}"
    now = time.time()
    conn = _connect()
    conn.execute(
        """INSERT INTO products
           (product_id, merchant_id, name, description, category, price, availability, attributes, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (pid, merchant_id, name, description, category, price, availability,
         json.dumps(attributes or {}), now, now),
    )
    conn.commit()
    conn.close()
    return pid


def search_products(query: str = "", category: str = "", price_max: int = 0,
                    price_min: int = 0, merchant_id: str = "", limit: int = 20) -> list[dict]:
    conn = _connect()
    sql = """
        SELECT p.*, m.name as merchant_name, m.rating as merchant_rating
        FROM products p JOIN merchants m ON p.merchant_id = m.merchant_id
        WHERE m.active = 1 AND p.availability = 'in_stock'
    """
    params: list = []

    if query:
        q = f"%{query.strip()}%"
        words = [w.strip() for w in query.split() if len(w.strip()) >= 2]
        if len(words) <= 1:
            sql += " AND (p.name LIKE ? OR p.description LIKE ? OR p.category LIKE ? OR p.attributes LIKE ?)"
            params.extend([q, q, q, q])
        else:
            clause = "(p.name LIKE ? OR p.description LIKE ? OR p.category LIKE ? OR p.attributes LIKE ?"
            params.extend([q, q, q, q])
            word_clauses = []
            for w in words:
                qw = f"%{w}%"
                word_clauses.append("(p.name LIKE ? OR p.description LIKE ? OR p.category LIKE ? OR p.attributes LIKE ?)")
                params.extend([qw, qw, qw, qw])
            clause += " OR (" + " AND ".join(word_clauses) + "))"
            sql += f" AND {clause}"
    if category:
        sql += " AND p.category LIKE ?"
        params.append(f"%{category}%")
    if price_max > 0:
        sql += " AND p.price <= ?"
        params.append(price_max)
    if price_min > 0:
        sql += " AND p.price >= ?"
        params.append(price_min)
    if merchant_id:
        sql += " AND p.merchant_id = ?"
        params.append(merchant_id)

    sql += " ORDER BY p.price ASC LIMIT ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    conn.close()

    results = []
    for r in rows:
        d = dict(r)
        d["attributes"] = json.loads(d["attributes"])
        d["price_display"] = f"₹{d['price'] / 100:,.2f}"
        results.append(d)
    return results


def get_product(product_id: str) -> dict | None:
    conn = _connect()
    row = conn.execute(
        """SELECT p.*, m.name as merchant_name, m.rating as merchant_rating
           FROM products p JOIN merchants m ON p.merchant_id = m.merchant_id
           WHERE p.product_id = ?""",
        (product_id,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["attributes"] = json.loads(d["attributes"])
    d["price_display"] = f"₹{d['price'] / 100:,.2f}"
    return d


# ── Agents ──

def register_agent(buyer_name: str, agent_secret_hash: str, per_txn_limit: int,
                    daily_limit: int, monthly_limit: int, buyer_email: str = "",
                    platform: str = "custom", autonomy_mode: str = "confirm",
                    category_allow: list | None = None,
                    payment_model: str = "reserve", wallet_id: str = "") -> str:
    aid = f"agent_{uuid.uuid4().hex[:12]}"
    conn = _connect()
    conn.execute(
        """INSERT INTO agents
           (agent_id, agent_secret_hash, buyer_name, buyer_email, platform,
            per_txn_limit, daily_limit, monthly_limit, autonomy_mode, category_allow,
            payment_model, wallet_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (aid, agent_secret_hash, buyer_name, buyer_email, platform,
         per_txn_limit, daily_limit, monthly_limit, autonomy_mode,
         json.dumps(category_allow or []), payment_model, wallet_id, time.time()),
    )
    conn.commit()
    conn.close()
    return aid


def get_agent(agent_id: str) -> dict | None:
    conn = _connect()
    row = conn.execute("SELECT * FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["category_allow"] = json.loads(d["category_allow"])
    d["preferences"] = json.loads(d.get("preferences") or "{}")
    d["passport"] = json.loads(d["passport"]) if d.get("passport") else None
    return d


def set_agent_passport(agent_id: str, passport: dict) -> None:
    conn = _connect()
    conn.execute("UPDATE agents SET passport = ? WHERE agent_id = ?",
                 (json.dumps(passport), agent_id))
    conn.commit()
    conn.close()


def revoke_agent(agent_id: str) -> bool:
    """Emergency stop: the passport stays signed but the agent is marked revoked,
    so the purchase gate refuses it with `revoked` on the very next attempt."""
    conn = _connect()
    cur = conn.execute("UPDATE agents SET status = 'revoked' WHERE agent_id = ?", (agent_id,))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def update_agent_preferences(agent_id: str, preferences: dict) -> bool:
    conn = _connect()
    cursor = conn.execute(
        "UPDATE agents SET preferences = ? WHERE agent_id = ?",
        (json.dumps(preferences), agent_id),
    )
    conn.commit()
    conn.close()
    return cursor.rowcount > 0


# ── Spend tracking ──

def record_spend(agent_id: str, amount: int, order_id: str) -> None:
    conn = _connect()
    conn.execute(
        "INSERT INTO agent_spend (agent_id, amount, order_id, spent_at) VALUES (?, ?, ?, ?)",
        (agent_id, amount, order_id, time.time()),
    )
    conn.commit()
    conn.close()


def get_daily_spend(agent_id: str) -> int:
    conn = _connect()
    day_start = time.time() - (time.time() % 86400)
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) as total FROM agent_spend WHERE agent_id = ? AND spent_at >= ?",
        (agent_id, day_start),
    ).fetchone()
    conn.close()
    return row["total"]


def get_monthly_spend(agent_id: str) -> int:
    conn = _connect()
    month_start = time.time() - (30 * 86400)
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) as total FROM agent_spend WHERE agent_id = ? AND spent_at >= ?",
        (agent_id, month_start),
    ).fetchone()
    conn.close()
    return row["total"]


# ── Orders ──

def create_order(order_id: str, agent_id: str, buyer_name: str, merchant_id: str,
                 product_id: str, product_name: str, amount: int, quantity: int = 1,
                 payment_model: str = "reserve", wallet_id: str = "",
                 idempotency_key: str = "", offer_id: str = "",
                 list_amount: int = 0) -> None:
    now = time.time()
    conn = _connect()
    conn.execute(
        """INSERT INTO orders
           (order_id, agent_id, buyer_name, merchant_id, product_id, product_name,
            quantity, amount, status, payment_model, wallet_id,
            idempotency_key, offer_id, list_amount, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'created', ?, ?, ?, ?, ?, ?, ?)""",
        (order_id, agent_id, buyer_name, merchant_id, product_id, product_name,
         quantity, amount, payment_model, wallet_id,
         idempotency_key, offer_id, list_amount or amount, now, now),
    )
    conn.commit()
    conn.close()


def update_order(order_id: str, **fields) -> None:
    conn = _connect()
    fields["updated_at"] = time.time()
    sets = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [order_id]
    conn.execute(f"UPDATE orders SET {sets} WHERE order_id = ?", vals)
    conn.commit()
    conn.close()


def get_order(order_id: str) -> dict | None:
    conn = _connect()
    row = conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_upi_requests(wallet_id: str | None = None, limit: int = 20) -> list[dict]:
    """Collect requests waiting on the buyer's UPI-app approval.

    Scoped to one reserve when `wallet_id` is given — a UPI inbox must only ever
    show its own owner's requests. Callers that hold a reserve token always pass
    it; the unscoped form exists only for the operator-facing audit view.
    """
    conn = _connect()
    sql = """SELECT o.*, p.description, m.name AS merchant_name, m.brand_color,
                    w.upi_vpa AS payer_vpa, w.owner_name AS payer_name
             FROM orders o
             LEFT JOIN products p ON o.product_id = p.product_id
             LEFT JOIN merchants m ON o.merchant_id = m.merchant_id
             LEFT JOIN wallets w ON o.wallet_id = w.wallet_id
             WHERE o.status = 'awaiting_upi_approval'"""
    params: list = []
    if wallet_id:
        sql += " AND o.wallet_id = ?"
        params.append(wallet_id)
    sql += " ORDER BY o.created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_agents_for_wallet(wallet_id: str) -> list[dict]:
    """Every agent drawing on one reserve, with its passport parsed."""
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM agents WHERE wallet_id = ? ORDER BY created_at DESC",
        (wallet_id,),
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d.pop("agent_secret_hash", None)
        d["category_allow"] = json.loads(d.get("category_allow") or "[]")
        d["preferences"] = json.loads(d.get("preferences") or "{}")
        try:
            d["passport"] = json.loads(d["passport"]) if d.get("passport") else None
        except (ValueError, TypeError):
            d["passport"] = None
        out.append(d)
    return out


def get_orders_for_wallet(wallet_id: str, limit: int = 40) -> list[dict]:
    """Order history for one reserve, newest first."""
    conn = _connect()
    rows = conn.execute(
        """SELECT o.*, m.name AS merchant_name, m.brand_color
           FROM orders o
           LEFT JOIN merchants m ON o.merchant_id = m.merchant_id
           WHERE o.wallet_id = ?
           ORDER BY o.created_at DESC LIMIT ?""",
        (wallet_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def find_order_by_idempotency_key(key: str) -> dict | None:
    """The order a previous identical request already created, if any."""
    if not key:
        return None
    conn = _connect()
    row = conn.execute("SELECT * FROM orders WHERE idempotency_key = ?", (key,)).fetchone()
    conn.close()
    return dict(row) if row else None


def ensure_merchant_signing_key(merchant_id: str) -> tuple[str, str]:
    """(private, public) for a merchant, minting one on first use so merchants
    created before offers existed still work."""
    from commerce_platform import offers as _offers
    conn = _connect()
    row = conn.execute(
        "SELECT offer_privkey, offer_pubkey FROM merchants WHERE merchant_id = ?",
        (merchant_id,)).fetchone()
    if row and row["offer_privkey"] and row["offer_pubkey"]:
        conn.close()
        return row["offer_privkey"], row["offer_pubkey"]
    priv, pub = _offers.new_keypair()
    conn.execute("UPDATE merchants SET offer_privkey = ?, offer_pubkey = ? WHERE merchant_id = ?",
                 (priv, pub, merchant_id))
    conn.commit()
    conn.close()
    return priv, pub


def get_merchant_pubkey(merchant_id: str) -> str:
    conn = _connect()
    row = conn.execute("SELECT offer_pubkey FROM merchants WHERE merchant_id = ?",
                       (merchant_id,)).fetchone()
    conn.close()
    return (row["offer_pubkey"] if row else "") or ""


def set_wallet_razorpay_order(wallet_id: str, razorpay_order_id: str) -> None:
    conn = _connect()
    conn.execute("UPDATE wallets SET razorpay_order_id = ? WHERE wallet_id = ?",
                 (razorpay_order_id, wallet_id))
    conn.commit()
    conn.close()


# ── Reserves (Razorpay UPI Reserve Pay model) ──
#
# A reserve is a single block of funds the buyer authorizes once (UPI PIN in
# production). The agent then debits it as it buys — Single Block Multi Debit.
# Rules encoded here mirror the real product:
#   • one ceiling, RESERVE_CAP (₹10,000) — enforced when the reserve is loaded
#   • authorizing/loading assumes AFA collected by the caller (the PIN step)
#   • a payment is an atomic reserve->merchant transfer (fanned out by Route in prod)
# The tables are still named `wallets` / `wallet_ledger` for storage; the concept
# they hold is the reserve.


def _wallet_public(row: sqlite3.Row) -> dict:
    d = dict(row)
    d.pop("secret_hash", None)
    d["balance_display"] = f"₹{d['balance'] / 100:,.0f}"
    d["reserve_cap"] = RESERVE_CAP
    return d


def create_wallet(owner_type: str, owner_name: str, owner_email: str = "",
                  secret_hash: str = "", upi_vpa: str = "",
                  opening_balance: int = 0, owner_phone: str = "") -> str:
    wid = f"wal_{uuid.uuid4().hex[:12]}"
    now = time.time()
    conn = _connect()
    conn.execute(
        """INSERT INTO wallets
           (wallet_id, owner_type, owner_name, owner_email, secret_hash,
            balance, kyc_tier, upi_vpa, owner_phone, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (wid, owner_type, owner_name, owner_email, secret_hash,
         0, "", upi_vpa, owner_phone, now),
    )
    conn.commit()
    conn.close()
    if opening_balance > 0:
        wallet_topup(wid, opening_balance, note="Reserve authorized")
    return wid


def get_wallet(wallet_id: str) -> dict | None:
    conn = _connect()
    row = conn.execute("SELECT * FROM wallets WHERE wallet_id = ?", (wallet_id,)).fetchone()
    conn.close()
    return _wallet_public(row) if row else None


def get_wallet_auth(wallet_id: str) -> dict | None:
    """Full row including secret hash. Sign-in path only."""
    conn = _connect()
    row = conn.execute("SELECT * FROM wallets WHERE wallet_id = ?", (wallet_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def find_wallet_by_email(owner_email: str, owner_type: str = "user") -> dict | None:
    """Full row including secret hash. Sign-in path only."""
    if not owner_email:
        return None
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM wallets WHERE lower(owner_email) = lower(?) AND owner_type = ?",
        (owner_email, owner_type),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def _ledger(conn, wallet_id, direction, amount, balance_after, kind,
            txn_id, counterparty="", order_id="", note=""):
    conn.execute(
        """INSERT INTO wallet_ledger
           (txn_id, wallet_id, direction, amount, balance_after, kind,
            counterparty, order_id, note, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (txn_id, wallet_id, direction, amount, balance_after, kind,
         counterparty, order_id, note, time.time()),
    )


def wallet_topup(wallet_id: str, amount: int, note: str = "Reserve authorized") -> dict:
    """Load a reserve. AFA is assumed collected by the caller (the UPI PIN step).

    Rejects anything that would push the reserve past the ₹10,000 Reserve Pay
    ceiling — the real product's cap on a single block.
    """
    if amount <= 0:
        return {"success": False, "error": "Amount must be positive."}
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM wallets WHERE wallet_id = ?", (wallet_id,)).fetchone()
        if not row:
            conn.rollback()
            return {"success": False, "error": "Reserve not found."}
        new_balance = row["balance"] + amount
        if new_balance > RESERVE_CAP:
            conn.rollback()
            room = max(0, RESERVE_CAP - row["balance"])
            return {
                "success": False,
                "error": f"That exceeds the ₹10,000 Reserve Pay ceiling. You can reserve up to ₹{room / 100:,.0f} more.",
                "headroom": room,
            }
        txn_id = f"txn_{uuid.uuid4().hex[:12]}"
        conn.execute("UPDATE wallets SET balance = ? WHERE wallet_id = ?", (new_balance, wallet_id))
        _ledger(conn, wallet_id, "credit", amount, new_balance, "topup", txn_id, note=note)
        conn.commit()
        return {"success": True, "txn_id": txn_id, "balance": new_balance,
                "balance_display": f"₹{new_balance / 100:,.0f}"}
    finally:
        conn.close()


def wallet_transfer(from_wallet_id: str, to_wallet_id: str, amount: int,
                    order_id: str = "", note: str = "") -> dict:
    """Atomic user->merchant payment. Both legs and both ledger rows commit together
    or not at all, and the debit is refused if the balance cannot cover it.

    This is the money-math core: it can never leave the two wallets out of balance,
    and it can never produce a negative balance.
    """
    if amount <= 0:
        return {"success": False, "error": "Amount must be positive."}
    if from_wallet_id == to_wallet_id:
        return {"success": False, "error": "Cannot transfer to the same wallet."}
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        src = conn.execute("SELECT * FROM wallets WHERE wallet_id = ?", (from_wallet_id,)).fetchone()
        dst = conn.execute("SELECT * FROM wallets WHERE wallet_id = ?", (to_wallet_id,)).fetchone()
        if not src:
            conn.rollback()
            return {"success": False, "error": "Source wallet not found."}
        if not dst:
            conn.rollback()
            return {"success": False, "error": "Merchant wallet not found."}
        if src["balance"] < amount:
            conn.rollback()
            return {
                "success": False,
                "error": f"Insufficient wallet balance. Available ₹{src['balance'] / 100:,.0f}, needs ₹{amount / 100:,.0f}.",
                "balance": src["balance"],
                "code": "insufficient_funds",
            }
        txn_id = f"txn_{uuid.uuid4().hex[:12]}"
        src_after = src["balance"] - amount
        dst_after = dst["balance"] + amount
        conn.execute("UPDATE wallets SET balance = ? WHERE wallet_id = ?", (src_after, from_wallet_id))
        conn.execute("UPDATE wallets SET balance = ? WHERE wallet_id = ?", (dst_after, to_wallet_id))
        _ledger(conn, from_wallet_id, "debit", amount, src_after, "payment", txn_id,
                counterparty=dst["owner_name"], order_id=order_id, note=note)
        _ledger(conn, to_wallet_id, "credit", amount, dst_after, "payout", txn_id,
                counterparty=src["owner_name"], order_id=order_id, note=note)
        conn.commit()
        return {
            "success": True,
            "txn_id": txn_id,
            "from_balance": src_after,
            "from_balance_display": f"₹{src_after / 100:,.0f}",
            "to_balance": dst_after,
        }
    finally:
        conn.close()


def wallet_credit_merchant(wallet_id: str, amount: int, order_id: str = "",
                           counterparty: str = "", note: str = "") -> dict:
    """Credit merchant wallet directly on direct UPI payments (bypass reserve debit)."""
    if amount <= 0:
        return {"success": False, "error": "Amount must be positive."}
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        dst = conn.execute("SELECT * FROM wallets WHERE wallet_id = ?", (wallet_id,)).fetchone()
        if not dst:
            conn.rollback()
            return {"success": False, "error": "Merchant wallet not found."}
        txn_id = f"txn_{uuid.uuid4().hex[:12]}"
        dst_after = dst["balance"] + amount
        conn.execute("UPDATE wallets SET balance = ? WHERE wallet_id = ?", (dst_after, wallet_id))
        _ledger(conn, wallet_id, "credit", amount, dst_after, "payout", txn_id,
                counterparty=counterparty, order_id=order_id, note=note)
        conn.commit()
        return {"success": True, "txn_id": txn_id, "to_balance": dst_after}
    finally:
        conn.close()


def get_wallet_ledger(wallet_id: str, limit: int = 25) -> list[dict]:
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM wallet_ledger WHERE wallet_id = ? ORDER BY id DESC LIMIT ?",
        (wallet_id, limit),
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["amount_display"] = f"₹{d['amount'] / 100:,.0f}"
        d["balance_after_display"] = f"₹{d['balance_after'] / 100:,.0f}"
        out.append(d)
    return out


def set_merchant_wallet(merchant_id: str, wallet_id: str) -> None:
    conn = _connect()
    conn.execute("UPDATE merchants SET wallet_id = ? WHERE merchant_id = ?", (wallet_id, merchant_id))
    conn.commit()
    conn.close()
