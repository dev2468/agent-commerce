"""MCP Server -- exposes the Agent Commerce platform as tool calls.

This is a stdio-based MCP server that an AI agent (via Google ADK McpToolset)
connects to. The agent_id is passed via the AGENT_ID environment variable,
set when the ADK McpToolset spawns this as a subprocess.
"""

import json
import os
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from commerce_platform import db
from commerce_platform.policy import get_budget
from commerce_platform.payments import pay_from_reserve, check_order_status

AGENT_ID = os.environ.get("AGENT_ID", "")

server = Server("agent-commerce")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="search_products",
            description=(
                "Search for products across all merchants on the platform. "
                "Returns product name, price, merchant, availability, and attributes. "
                "Use filters to narrow results by category, price range, or keyword."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search keyword (e.g., '65 inch TV', 'laptop', 'headphones')"},
                    "category": {"type": "string", "description": "Product category filter (e.g., 'electronics.televisions')"},
                    "price_max": {"type": "integer", "description": "Maximum price in rupees (e.g., 40000)"},
                    "price_min": {"type": "integer", "description": "Minimum price in rupees"},
                    "limit": {"type": "integer", "description": "Max results to return (default 10)", "default": 10},
                },
            },
        ),
        Tool(
            name="get_product_details",
            description="Get full details of a specific product by its product_id.",
            inputSchema={
                "type": "object",
                "properties": {
                    "product_id": {"type": "string", "description": "The product ID (e.g., 'prod_abc123')"},
                },
                "required": ["product_id"],
            },
        ),
        Tool(
            name="purchase_product",
            description=(
                "Buy a product. The purchase is checked against this agent's signed Agent Passport "
                "(per-transaction cap, daily cap, allowed categories, expiry) before anything happens. "
                "By default a UPI collect request is pushed to the buyer's UPI app and they approve it "
                "there -- money only moves after they tap approve. "
                "IMPORTANT: Always confirm with the user before calling this tool."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "product_id": {"type": "string", "description": "The product ID to purchase"},
                    "quantity": {"type": "integer", "description": "Quantity to buy (default 1)", "default": 1},
                    "autonomous": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Leave false (default) to send a UPI approval request the buyer taps. "
                            "Set true only when the user has explicitly asked the agent to pay without "
                            "prompting -- it settles straight from the pre-authorized reserve."
                        ),
                    },
                },
                "required": ["product_id"],
            },
        ),
        Tool(
            name="check_order_status",
            description="Check the status of an existing order.",
            inputSchema={
                "type": "object",
                "properties": {
                    "order_id": {"type": "string", "description": "The order ID to check"},
                },
                "required": ["order_id"],
            },
        ),
        Tool(
            name="check_budget",
            description=(
                "Check the current spending budget -- per-transaction limit, daily/monthly spend and remaining amounts. "
                "Call this before making a purchase to know how much the user can spend."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="view_audit_log",
            description="View recent actions taken by this agent -- searches, orders, policy checks.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Number of entries to show (default 10)", "default": 10},
                },
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "search_products":
            query = arguments.get("query", "")
            category = arguments.get("category", "")
            price_max = arguments.get("price_max", 0)
            price_min = arguments.get("price_min", 0)
            limit = arguments.get("limit", 10)

            results = db.search_products(
                query=query,
                category=category,
                price_max=price_max * 100 if price_max else 0,
                price_min=price_min * 100 if price_min else 0,
                limit=limit,
            )
            db.audit("product_search", AGENT_ID,
                     query=query, category=category, price_max=price_max,
                     result_count=len(results))

            if not results:
                return [TextContent(type="text", text="No products found matching your search criteria.")]

            lines = [f"Found {len(results)} product(s):\n"]
            for i, p in enumerate(results, 1):
                lines.append(
                    f"{i}. **{p['name']}**\n"
                    f"   Product ID: `{p['product_id']}`\n"
                    f"   Price: {p['price_display']}\n"
                    f"   Merchant: {p['merchant_name']} (Rating: {p.get('merchant_rating', 'N/A')})\n"
                    f"   Category: {p['category']}\n"
                    f"   Availability: {p['availability']}\n"
                )
                if p.get("attributes"):
                    attrs = ", ".join(f"{k}: {v}" for k, v in p["attributes"].items())
                    lines.append(f"   Attributes: {attrs}\n")
            return [TextContent(type="text", text="\n".join(lines))]

        elif name == "get_product_details":
            product = db.get_product(arguments["product_id"])
            if not product:
                return [TextContent(type="text", text="Product not found.")]
            db.audit("product_viewed", AGENT_ID, product_id=arguments["product_id"])
            return [TextContent(type="text", text=json.dumps(product, indent=2, default=str))]

        elif name == "purchase_product":
            if not AGENT_ID:
                return [TextContent(type="text", text="Error: Agent ID not configured.")]
            mode = "autonomous" if arguments.get("autonomous") else "collect"
            result = pay_from_reserve(
                AGENT_ID,
                arguments["product_id"],
                arguments.get("quantity", 1),
                mode=mode,
            )
            if not result["success"]:
                # The passport gate refuses with a stable code -- surface it so the
                # model can explain *why* rather than guessing or retrying blindly.
                code = result.get("code")
                suffix = f" (code: {code})" if code else ""
                return [TextContent(type="text", text=f"Purchase refused: {result['error']}{suffix}")]

            if result.get("status") == "awaiting_upi_approval":
                return [TextContent(type="text", text=(
                    f"UPI approval request sent to the buyer.\n\n"
                    f"**Order ID:** {result['order_id']}\n"
                    f"**Product:** {result['product']}\n"
                    f"**Merchant:** {result['merchant']}\n"
                    f"**Amount:** {result['amount_display']}\n\n"
                    f"It is waiting in their UPI app. Nothing has been debited yet -- "
                    f"the money moves only once they tap approve. "
                    f"Use check_order_status to see when it is paid."
                ))]

            return [TextContent(type="text", text=(
                f"Paid from the reserve.\n\n"
                f"**Order ID:** {result['order_id']}\n"
                f"**Product:** {result['product']}\n"
                f"**Merchant:** {result['merchant']}\n"
                f"**Amount:** {result['amount_display']}\n"
                f"**Reserve left:** {result.get('reserve_balance_display', 'n/a')}"
            ))]

        elif name == "check_order_status":
            result = check_order_status(arguments["order_id"])
            if not result:
                return [TextContent(type="text", text="Order not found.")]
            return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

        elif name == "check_budget":
            if not AGENT_ID:
                return [TextContent(type="text", text="Error: Agent ID not configured.")]
            budget = get_budget(AGENT_ID)
            if not budget:
                return [TextContent(type="text", text="Agent not found.")]
            lines = [
                "**Current Budget Status:**\n",
                f"Per-transaction limit: {budget['per_transaction_limit']}",
                f"Daily limit: {budget['daily_limit']}",
                f"Daily spent: {budget['daily_spent']}",
                f"Daily remaining: {budget['daily_remaining']}",
                f"Monthly limit: {budget['monthly_limit']}",
                f"Monthly spent: {budget['monthly_spent']}",
                f"Monthly remaining: {budget['monthly_remaining']}",
                f"Autonomy mode: {budget['autonomy_mode']}",
            ]
            return [TextContent(type="text", text="\n".join(lines))]

        elif name == "view_audit_log":
            limit = arguments.get("limit", 10)
            entries = db.get_audit_log(agent_id=AGENT_ID, limit=limit)
            if not entries:
                return [TextContent(type="text", text="No audit entries found.")]
            lines = [f"Last {len(entries)} audit entries:\n"]
            for e in entries:
                details = json.loads(e["details"]) if isinstance(e["details"], str) else e["details"]
                summary = ", ".join(f"{k}={v}" for k, v in list(details.items())[:4])
                lines.append(f"- [{e['event_type']}] {summary}")
            return [TextContent(type="text", text="\n".join(lines))]

        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

    except Exception as e:
        db.audit("tool_error", AGENT_ID, tool=name, error=str(e))
        return [TextContent(type="text", text=f"Error executing {name}: {e}")]


async def main():
    db.init_db()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
