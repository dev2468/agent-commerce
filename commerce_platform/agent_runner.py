"""A real agent loop the browser can drive.

The MCP server (`mcp_server.py`) exposes these same capabilities over stdio for
Google ADK. That path is the product story, but it cannot be watched from a web
page: ADK spawns a subprocess and talks a protocol the browser does not speak.

So this module runs the *same* underlying functions — `db.search_products`,
`payments.pay_from_reserve`, `policy.get_budget` — through Gemini function
calling, in-process, and returns a structured transcript. The point is that a
purchase made here is not a demo shortcut: it goes through
`pay_from_reserve`, which means the passport gate and the spend guards run
exactly as they do for the ADK agent, and a refusal comes back with the same
stable code.

Without GOOGLE_API_KEY the loop still works: a deterministic planner picks the
best-value match and buys it. The transcript says which brain was used, because
a demo that silently degrades from "an LLM chose this" to "a for-loop chose
this" is lying about the interesting part.
"""

import json
import os
import re
import time

from commerce_platform import db, url_intel
from commerce_platform.payments import pay_from_reserve, check_order_status
from commerce_platform.policy import get_budget

# Gemini model availability shifts under you — 2.5-flash is already closed to new
# keys, and the API itself points at 3.6-flash. Override with GEMINI_MODEL; the
# loop falls through the rest on a 404 and remembers what worked.
# OpenRouter is OpenAI-compatible, so it needs a different call shape than the
# Google SDK — same tools, different wire format. Preferred when its key is set,
# because it has been the more reliable route from this machine.
OR_URL = "https://openrouter.ai/api/v1/chat/completions"
OR_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemini-3.7-flash")

MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
_MODEL_CHAIN = [MODEL, "gemini-3.6-flash", "gemini-flash-latest", "gemini-3-flash-preview"]
_working_model: str | None = None
MAX_STEPS = 12

SYSTEM_PROMPT = """You are a shopping agent acting for a human buyer on an agent-commerce platform.

You hold a signed Agent Passport that states exactly what you may spend autonomously (up to ₹10,000 per transaction).
Every autonomous purchase you attempt is checked against it — per-transaction cap, daily cap, allowed categories,
expiry, revocation — plus live monthly and velocity guards. You cannot talk your way past those checks,
and you should not try: if an autonomous purchase is refused, report the refusal and its code plainly and
suggest what would work instead (a cheaper item, a different category).

Important Rule for External Product Links (Amazon, Flipkart, etc.):
- If the user provides an external product link or URL, call `inspect_product_link` first to extract the item's specifications,
  brand, and benchmark price.
- You cannot buy directly on external third-party sites like Amazon; instead, use the extracted title/keywords to search
  your platform's onboarded merchants (e.g. Nykaa, Croma, Decathlon) with `search_products`.
- Match the best equivalent item within the buyer's budget and proceed with `purchase_product`.

Important Rule for High-Value Items (> ₹10,000) vs Autonomous Reserve Spend (<= ₹10,000):
- Items <= ₹10,000: Directly cut the amount from the reserve (autonomous=true). The buyer gave consent at mandate setup;
  do NOT send a UPI collect request or ask for manual approval for purchases <= ₹10,000! Settle it directly.
- Items > ₹10,000: Exceed the UPI reserve block ceiling. When you call purchase_product, the platform automatically routes
  it as a direct 2FA UPI collect request to the buyer's phone for UPI PIN approval. Tell the buyer to approve on their phone.

How to work — be decisive, you have a limited number of steps:
- If a link was provided, call inspect_product_link first.
- Call check_budget once, first, when the request involves spending.
- Call search_products once. Use short keywords ("saffron oil", "wireless headphones", not the whole title),
  plus price_max when they named a budget. If it returns nothing, retry once with a broader
  keyword. Never search more than twice — choose the best of what you already have.
- Never invent a product or a price. Compare on price, merchant rating and fit.
- Then call purchase_product straight away. For items <= ₹10,000, autonomous=true directly cuts the amount from the reserve.
- Be brief and concrete. State what you found, what you bought, and the settled amount."""

TOOLS = [{
    "name": "inspect_product_link",
    "description": ("Extract structured product details (title, brand, reference price, keywords) "
                    "from an external product link (e.g. Amazon, Flipkart, H&M) to use as a specification."),
    "parameters": {"type": "object", "properties": {
        "url": {"type": "string", "description": "The product URL to inspect"},
    }, "required": ["url"]},
}, {
    "name": "search_products",
    "description": "Search products across every merchant on the platform. Returns real catalog items with ids and prices.",
    "parameters": {"type": "object", "properties": {
        "query": {"type": "string", "description": "Keywords, e.g. 'wireless headphones'"},
        "category": {"type": "string", "description": "Optional category filter"},
        "price_max": {"type": "integer", "description": "Max price in rupees"},
        "price_min": {"type": "integer", "description": "Min price in rupees"},
        "limit": {"type": "integer", "description": "Max results, default 8"},
    }},
}, {
    "name": "get_product_details",
    "description": "Full details for one product id.",
    "parameters": {"type": "object",
                   "properties": {"product_id": {"type": "string"}},
                   "required": ["product_id"]},
}, {
    "name": "check_budget",
    "description": "The caps and remaining spend on this agent's passport. Call before spending.",
    "parameters": {"type": "object", "properties": {}},
}, {
    "name": "purchase_product",
    "description": ("Buy a product. Runs the passport gate and spend guards. For items <= ₹10,000, "
                    "directly settles and cuts from the pre-authorized reserve (autonomous=true by default). "
                    "For items > ₹10,000, automatically routes as a direct UPI collect request to the buyer's phone for PIN approval."),
    "parameters": {"type": "object", "properties": {
        "product_id": {"type": "string"},
        "quantity": {"type": "integer", "description": "Default 1"},
        "autonomous": {"type": "boolean",
                       "description": "Default true for items <= ₹10,000: directly cuts from reserve with no prompt. Set false only if user requested a manual UPI collect request."},
    }, "required": ["product_id"]},
}, {
    "name": "check_order_status",
    "description": "Status of an order by id.",
    "parameters": {"type": "object",
                   "properties": {"order_id": {"type": "string"}},
                   "required": ["order_id"]},
}]


# ── Tool implementations — the same functions the MCP server calls ──

def _t_search(agent_id: str, a: dict) -> dict:
    rows = db.search_products(
        query=a.get("query", ""), category=a.get("category", ""),
        price_max=int(a.get("price_max") or 0) * 100,
        price_min=int(a.get("price_min") or 0) * 100,
        limit=int(a.get("limit") or 8))
    db.audit("product_search", agent_id, query=a.get("query", ""),
             result_count=len(rows), via="web_console")
    return {"count": len(rows), "products": [{
        "product_id": p["product_id"], "name": p["name"],
        "price_rupees": p["price"] / 100, "price_display": p["price_display"],
        "merchant": p["merchant_name"], "rating": p.get("merchant_rating"),
        "category": p["category"], "availability": p["availability"],
    } for p in rows]}


def _t_details(agent_id: str, a: dict) -> dict:
    p = db.get_product(a.get("product_id", ""))
    if not p:
        return {"error": "Product not found."}
    db.audit("product_viewed", agent_id, product_id=a.get("product_id"), via="web_console")
    return {"product_id": p["product_id"], "name": p["name"],
            "price_display": p.get("price_display", f"₹{p['price']/100:,.0f}"),
            "description": p.get("description", ""), "category": p["category"],
            "merchant": p.get("merchant_name", ""), "availability": p["availability"],
            "attributes": p.get("attributes", {})}


def _t_budget(agent_id: str, a: dict) -> dict:
    return get_budget(agent_id) or {"error": "Agent not found."}


def _t_purchase(agent_id: str, a: dict) -> dict:
    """The real thing: gates, order, autonomous reserve cut or direct collect request."""
    autonomous = a.get("autonomous", True)
    result = pay_from_reserve(
        agent_id, a.get("product_id", ""), int(a.get("quantity") or 1),
        mode="autonomous" if autonomous else "collect")
    if not result.get("success"):
        return {"refused": True, "error": result.get("error"),
                "code": result.get("code"),
                "hint": "This was blocked by the passport gate or a spend guard. "
                        "Do not retry the same purchase; report the reason."}
    return result


def _t_order_status(agent_id: str, a: dict) -> dict:
    return check_order_status(a.get("order_id", "")) or {"error": "Order not found."}


def _t_inspect_link(agent_id: str, a: dict) -> dict:
    url = (a.get("url") or "").strip()
    if not url:
        return {"error": "url is required."}
    try:
        data = url_intel.extract(url)
        db.audit("link_inspected", agent_id, url=url, title=data.get("title", ""))
        return {
            "title": data.get("title", ""),
            "brand": data.get("brand", ""),
            "reference_price": data.get("price", 0) / 100 if data.get("price") else 0,
            "reference_price_display": data.get("price_display", ""),
            "suggested_query": data.get("query", ""),
            "host": data.get("host", ""),
        }
    except url_intel.UrlRefused as e:
        return {"error": f"URL refused: {e.reason}", "code": e.code}
    except Exception as e:
        return {"error": f"Could not inspect link: {str(e)}"}


IMPL = {"inspect_product_link": _t_inspect_link,
        "search_products": _t_search, "get_product_details": _t_details,
        "check_budget": _t_budget, "purchase_product": _t_purchase,
        "check_order_status": _t_order_status}


def _summarize(name: str, result: dict) -> str:
    """One line for the UI transcript."""
    if name == "inspect_product_link":
        if result.get("error"):
            return f"link error · {result.get('error')[:35]}"
        title = result.get("title") or "spec"
        return f"inspected link · {title[:35]}"
    if name == "search_products":
        return f"found {result.get('count', 0)} product(s)"
    if name == "purchase_product":
        if result.get("refused"):
            return f"REFUSED · {result.get('code') or 'blocked'}"
        if result.get("status") == "awaiting_upi_approval":
            if result.get("is_direct_upi"):
                return f"direct collect sent to phone · {result.get('amount_display', '')} · awaiting UPI PIN"
            return f"collect request sent · {result.get('amount_display', '')} · awaiting UPI approval"
        return f"paid · {result.get('amount_display', '')}"
    if name == "check_budget":
        return f"per-txn {result.get('per_transaction_limit', '?')} · daily left {result.get('daily_remaining', '?')}"
    if name == "get_product_details":
        return result.get("name", "product")
    return result.get("status", "ok")


# ── The loop ──

def run_agent(agent_id: str, message: str, max_steps: int = MAX_STEPS) -> dict:
    """Run one turn. Returns {reply, steps, brain}."""
    agent = db.get_agent(agent_id)
    if not agent:
        return {"error": "Agent not found.", "steps": [], "reply": ""}
    if agent.get("status") != "active":
        return {"error": f"This agent is {agent.get('status')}.", "steps": [], "reply": ""}

    or_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    api_key = os.getenv("GOOGLE_API_KEY", "").strip()
    if not or_key and not api_key:
        return _fallback(agent_id, message)
    try:
        # OpenRouter first when configured; fall through to the Google SDK so a
        # single bad response does not cost the turn its model.
        if or_key:
            try:
                return _openrouter(agent_id, message, or_key, max_steps)
            except Exception:                                     # noqa: BLE001
                if not api_key:
                    raise
        return _gemini(agent_id, message, api_key, max_steps)
    except Exception as e:                                        # noqa: BLE001
        # A model outage must not take the demo down — but say so rather than
        # passing off the deterministic planner as the model's work.
        out = _fallback(agent_id, message)
        # Carry the actual reason — "ClientError" alone tells nobody whether the
        # key is wrong, the model is gone, or the quota ran out.
        reason = str(e).replace("\n", " ")[:120] or type(e).__name__
        out["brain"] = f"deterministic (Gemini unavailable: {reason})"
        db.audit("agent_brain_fallback", agent_id, error=str(e)[:300])
        return out


def _openrouter(agent_id: str, message: str, api_key: str, max_steps: int) -> dict:
    """Same tools and same gates, over OpenRouter's OpenAI-compatible API."""
    import requests

    tools = [{"type": "function", "function": t} for t in TOOLS]
    msgs = [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": message}]
    steps: list[dict] = []

    for _ in range(max_steps):
        r = requests.post(
            OR_URL,
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json",
                     # OpenRouter attributes traffic with these; harmless if ignored.
                     "X-Title": "Agent Commerce"},
            json={"model": OR_MODEL, "messages": msgs, "tools": tools,
                  "temperature": 0.2},
            timeout=90)
        if r.status_code != 200:
            raise RuntimeError(f"OpenRouter {r.status_code}: {r.text[:200]}")
        body = r.json()
        if not body.get("choices"):
            raise RuntimeError(f"OpenRouter returned no choices: {str(body)[:200]}")
        m = body["choices"][0]["message"]
        msgs.append(m)

        calls = m.get("tool_calls") or []
        if not calls:
            return {"reply": (m.get("content") or "").strip(),
                    "steps": steps, "brain": f"OpenRouter · {OR_MODEL}"}

        for c in calls:
            name = c.get("function", {}).get("name", "")
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except (ValueError, TypeError):
                args = {}
            fn = IMPL.get(name)
            result = fn(agent_id, args) if fn else {"error": f"Unknown tool {name}"}
            steps.append({"tool": name, "args": args,
                          "summary": _summarize(name, result), "result": result})
            msgs.append({"role": "tool", "tool_call_id": c.get("id", ""),
                         "content": json.dumps(result, default=str)})

    return {"reply": "I ran out of steps before finishing that. Here is what I did.",
            "steps": steps, "brain": f"OpenRouter · {OR_MODEL}"}


def _gemini(agent_id: str, message: str, api_key: str, max_steps: int) -> dict:
    from google import genai
    from google.genai import types

    global _working_model

    client = genai.Client(api_key=api_key,
                          http_options=types.HttpOptions(timeout=45_000))
    tools = [types.Tool(function_declarations=TOOLS)]
    cfg = types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT, tools=tools,
                                      temperature=0.2)
    contents = [types.Content(role="user", parts=[types.Part(text=message)])]
    steps: list[dict] = []

    def generate(payload):
        """Call the first reachable model, retrying the transient failures.

        504s and locally-aborted connections show up often enough here that one
        unlucky call must not drop the whole turn onto the fallback planner.
        A 404 means the model is gone for this key, so move down the chain
        instead of retrying it.
        """
        global _working_model
        candidates = ([_working_model] if _working_model
                      else list(dict.fromkeys(_MODEL_CHAIN)))
        last = None
        for name in candidates:
            for attempt in range(3):
                try:
                    out = client.models.generate_content(
                        model=name, contents=payload, config=cfg)
                    _working_model = name
                    return out
                except Exception as e:                            # noqa: BLE001
                    last, msg = e, str(e)
                    if "404" in msg or "NOT_FOUND" in msg:
                        _working_model = None
                        break                     # model gone — try the next one
                    if attempt == 2:
                        break                     # transient, but out of retries
                    time.sleep(1.5 * (attempt + 1))
        raise last

    for _ in range(max_steps):
        resp = generate(contents)
        cand = (resp.candidates or [None])[0]
        if not cand or not cand.content or not cand.content.parts:
            break
        contents.append(cand.content)

        calls = [p.function_call for p in cand.content.parts if getattr(p, "function_call", None)]
        if not calls:
            text = "".join(p.text or "" for p in cand.content.parts if getattr(p, "text", None))
            return {"reply": text.strip(), "steps": steps, "brain": f"Gemini · {_working_model or MODEL}"}

        replies = []
        for call in calls:
            name = call.name
            args = dict(call.args or {})
            fn = IMPL.get(name)
            result = fn(agent_id, args) if fn else {"error": f"Unknown tool {name}"}
            steps.append({"tool": name, "args": args,
                          "summary": _summarize(name, result), "result": result})
            replies.append(types.Part.from_function_response(name=name, response={"result": result}))
        contents.append(types.Content(role="user", parts=replies))

    return {"reply": "I ran out of steps before finishing that. Here is what I did.",
            "steps": steps, "brain": f"Gemini · {_working_model or MODEL}"}


# Words that carry no signal for a catalog search. Passing a whole sentence as
# the query matches nothing, which reads as "the catalog is empty" when it isn't.
_STOP = {
    "find", "get", "buy", "me", "a", "an", "the", "some", "for", "under", "below",
    "over", "above", "best", "good", "cheap", "cheapest", "value", "one", "and",
    "or", "with", "please", "want", "need", "looking", "look", "rupees", "rupee",
    "rs", "inr", "than", "less", "more", "up", "to", "my", "i", "it", "that",
    "this", "of", "in", "on", "at", "is", "are", "can", "you", "your", "purchase",
    "available", "any", "such", "item", "product", "link", "url", "there",
}


def _parse_request(message: str) -> tuple[str, int]:
    """Pull search keywords and a price ceiling out of plain English."""
    clean_msg = re.sub(r"https?://\S+", " ", message)
    low = clean_msg.lower()
    price_max = 0
    m = re.search(r"(?:under|below|less than|max|upto|up to|within)\s*(?:rs\.?|inr|₹)?\s*(\d[\d,]*)", low)
    if not m:
        m = re.search(r"(\d[\d,]*)\s*(?:rupees|rs\b|inr|₹)", low)
    if m:
        try:
            price_max = int(m.group(1).replace(",", ""))
        except ValueError:
            price_max = 0
    words = [w for w in re.findall(r"[a-z0-9]+", low)
             if w not in _STOP and not w.isdigit() and len(w) > 2]
    return " ".join(words[:4]), price_max


def _fallback(agent_id: str, message: str) -> dict:
    """No API key (or Gemini down): keyword-search, then buy the best in-cap match."""
    steps: list[dict] = []
    budget = _t_budget(agent_id, {})
    steps.append({"tool": "check_budget", "args": {},
                  "summary": _summarize("check_budget", budget), "result": budget})

    url_match = re.search(r"https?://[^\s]+", message)
    query, price_max = _parse_request(message)

    if url_match:
        url = url_match.group(0).strip()
        inspected = _t_inspect_link(agent_id, {"url": url})
        steps.append({"tool": "inspect_product_link", "args": {"url": url},
                      "summary": _summarize("inspect_product_link", inspected), "result": inspected})
        spec_query = inspected.get("suggested_query", "")
        if spec_query:
            spec_words = spec_query.split()
            tail_phrase = " ".join(spec_words[-2:]) if len(spec_words) >= 2 else spec_query
            matching_words = [w for w in spec_words if w in query] if query else []
            if matching_words:
                query = " ".join(matching_words + [w for w in spec_words[-2:] if w not in matching_words])
            else:
                query = tail_phrase or spec_query
        elif inspected.get("title") and not query:
            query = " ".join(inspected["title"].split()[:3])
        if not price_max and inspected.get("reference_price"):
            price_max = int(inspected["reference_price"])

    args = {"query": query, "limit": 8}
    if price_max:
        args["price_max"] = price_max
    found = _t_search(agent_id, args)
    # Narrowing to nothing is worse than a broad list — widen rather than give up.
    if not found.get("count") and price_max:
        args.pop("price_max")
        found = _t_search(agent_id, args)
    if not found.get("count") and query and len(query.split()) > 1:
        args["query"] = " ".join(query.split()[-2:])
        found = _t_search(agent_id, args)
    if not found.get("count") and query:
        args["query"] = query.split(" ")[-1]
        found = _t_search(agent_id, args)
    steps.append({"tool": "search_products", "args": args,
                  "summary": _summarize("search_products", found), "result": found})

    items = found.get("products", [])
    if not items:
        return {"reply": f"Nothing in the catalog matched “{query or message}”.",
                "steps": steps, "brain": "deterministic (no GOOGLE_API_KEY)"}

    affordable = [p for p in items if not price_max or p["price_rupees"] <= price_max]
    pick = max(affordable or items, key=lambda p: (p.get("rating") or 0, -p["price_rupees"]))
    bought = _t_purchase(agent_id, {"product_id": pick["product_id"]})
    steps.append({"tool": "purchase_product", "args": {"product_id": pick["product_id"]},
                  "summary": _summarize("purchase_product", bought), "result": bought})

    if bought.get("refused"):
        reply = (f"Found {pick['name']} at {pick.get('price_display')} from {pick['merchant']}, "
                 f"but the purchase was refused: {bought.get('error')} "
                 f"(code: {bought.get('code')}).")
    elif bought.get("status") == "awaiting_upi_approval":
        if bought.get("is_direct_upi"):
            reply = (f"Picked {pick['name']} — {pick.get('price_display')} from {pick['merchant']}. "
                     f"Because it exceeds the ₹10,000 reserve ceiling, a direct UPI collect request was "
                     f"sent directly to your phone. Authorize it in your UPI app with your PIN to complete.")
        else:
            reply = (f"Picked {pick['name']} — {pick.get('price_display')} from {pick['merchant']}, "
                     f"the cheapest match. A UPI collect request is waiting in your UPI app. "
                     f"Nothing has been debited yet.")
    else:
        reply = (f"Bought {pick['name']} for {bought.get('amount_display')} from "
                 f"{pick['merchant']}, settled from your reserve.")
    return {"reply": reply, "steps": steps, "brain": "deterministic (no GOOGLE_API_KEY)"}
