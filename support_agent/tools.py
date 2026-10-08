"""The agent's tools.

Design rules that make these safe to hand to an LLM:
  * `customer_email` is NEVER a tool argument. It comes from the authenticated session
    (ToolContext), so the model cannot read or modify someone else's order, even if
    it is tricked into trying. Missing and not-owned orders return the same error so
    order IDs cannot be enumerated.
  * Tools return structured dicts: {"ok": True, ...} or {"ok": False, "error", "message"}.
  * Business rules (what can be cancelled) are enforced here, not left to the model.
  * Tools that change state (SENSITIVE) support dry_run; the agent layer forces a
    dry run + human confirmation before the real call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from .rag import PolicyIndex
from .safety import redact
from .store import Store
from .text import tokenize

ORDER_ID_RE = re.compile(r"\bORD-\d{4,6}\b", re.I)

STATUS_LABELS = {
    "processing": "Processing",
    "shipped": "Shipped",
    "out_for_delivery": "Out for delivery",
    "delivered": "Delivered",
    "cancelled": "Cancelled",
}
CANCELLABLE = {"processing"}


@dataclass
class ToolContext:
    store: Store
    policies: PolicyIndex
    customer_email: str


def _ok(**data) -> dict:
    return {"ok": True, **data}


def _err(code: str, message: str, **extra) -> dict:
    return {"ok": False, "error": code, "message": message, **extra}


def normalize_order_id(raw: str) -> str | None:
    match = ORDER_ID_RE.search(str(raw or ""))
    return match.group(0).upper() if match else None


def _owned_order(ctx: ToolContext, order_id: str):
    oid = normalize_order_id(order_id)
    if not oid:
        return None, _err("invalid_order_id", "Order IDs look like ORD-1234.")
    order = ctx.store.get_order(oid)
    if not order or order["customer_email"].lower() != ctx.customer_email.lower():
        return None, _err("order_not_found", f"I couldn't find order {oid} on your account.")
    return order, None


# ------------------------------------------------------------------------ tools
def list_orders(ctx: ToolContext) -> dict:
    orders = sorted(ctx.store.orders_for(ctx.customer_email), key=lambda o: o["placed_at"], reverse=True)
    return _ok(
        orders=[
            {
                "order_id": o["order_id"],
                "placed_at": o["placed_at"],
                "status": o["status"],
                "status_label": STATUS_LABELS[o["status"]],
                "items": [i["name"] for i in o["items"]],
                "total": o["total"],
                "currency": o["currency"],
            }
            for o in orders
        ]
    )


def get_order_status(ctx: ToolContext, order_id: str) -> dict:
    order, err = _owned_order(ctx, order_id)
    if err:
        return err
    return _ok(
        order_id=order["order_id"],
        status=order["status"],
        status_label=STATUS_LABELS[order["status"]],
        items=[f'{i["qty"]} x {i["name"]}' for i in order["items"]],
        total=order["total"],
        currency=order["currency"],
        carrier=order.get("carrier"),
        tracking_number=order.get("tracking_number"),
        expected_delivery=order.get("expected_delivery"),
        delivered_at=order.get("delivered_at"),
        last_update=order.get("last_update"),
        can_cancel=order["status"] in CANCELLABLE,
    )


def cancel_order(ctx: ToolContext, order_id: str, dry_run: bool = False) -> dict:
    order, err = _owned_order(ctx, order_id)
    if err:
        return err
    status = order["status"]
    if status == "cancelled":
        return _err("already_cancelled", f"Order {order['order_id']} is already cancelled.")
    if status not in CANCELLABLE:
        hint = {
            "shipped": "It has already shipped, so it can't be cancelled. You can refuse delivery, "
                       "or return it within 30 days of delivery.",
            "out_for_delivery": "It is out for delivery, so it can't be cancelled. Once it arrives "
                                "you can return it within 30 days of delivery.",
            "delivered": "It has already been delivered. You can start a return within 30 days of delivery.",
        }.get(status, "It can't be cancelled at this stage.")
        return _err("not_cancellable", f"Order {order['order_id']} is {STATUS_LABELS[status].lower()}. {hint}",
                    status=status)

    preview = dict(order_id=order["order_id"], items=[i["name"] for i in order["items"]],
                   refund_amount=order["total"], currency=order["currency"])
    if dry_run:
        return _ok(dry_run=True, eligible=True, **preview)

    order["status"] = "cancelled"
    order["last_update"] = "Cancelled by customer via support assistant"
    order["expected_delivery"] = None
    order["refund"] = {
        "status": "initiated",
        "amount": order["total"],
        "method": "original payment method",
        "reason": "Order cancelled before shipping",
        "initiated_at": "today",
        "expected_by": "5-7 business days",
    }
    ctx.store.save()
    return _ok(cancelled=True, refund_expected="5-7 business days", **preview)


def check_refund_status(ctx: ToolContext, order_id: str) -> dict:
    order, err = _owned_order(ctx, order_id)
    if err:
        return err
    refund = order.get("refund")
    if not refund:
        return _ok(order_id=order["order_id"], has_refund=False, order_status=order["status"])
    return _ok(order_id=order["order_id"], has_refund=True, currency=order["currency"], **refund)


def search_products(ctx: ToolContext, query: str, max_price: float | None = None,
                    in_stock_only: bool = False, limit: int = 5) -> dict:
    q = set(tokenize(query))
    if not q:
        return _ok(results=[], note="empty_query")
    results = []
    for p in ctx.store.products:
        name_t, cat_t = set(tokenize(p["name"])), set(tokenize(p["category"]))
        tag_t = {t for tag in p["tags"] for t in tokenize(tag)}
        desc_t = set(tokenize(p["description"]))
        score = 3 * len(q & name_t) + 2 * len(q & tag_t) + 2 * len(q & cat_t) + len(q & desc_t)
        if score == 0:
            continue
        if max_price is not None and p["price"] > float(max_price):
            continue
        if in_stock_only and p["stock"] <= 0:
            continue
        coverage = len(q & (name_t | tag_t | cat_t | desc_t))
        results.append((coverage, score, p))
    # Prefer products matching the most query words (a wireless mouse is not an answer to
    # "wireless earbuds"), then the best score, then the cheapest.
    results.sort(key=lambda x: (-x[0], -x[1], x[2]["price"]))
    if results:
        results = [r for r in results if r[0] == results[0][0]]
    limit = max(1, min(int(limit), 10))
    return _ok(results=[
        {"product_id": p["product_id"], "name": p["name"], "category": p["category"],
         "price": p["price"], "currency": "USD", "in_stock": p["stock"] > 0,
         "summary": p["description"]}
        for _, _, p in results[:limit]
    ])


def search_policy(ctx: ToolContext, query: str, k: int = 3) -> dict:
    hits = ctx.policies.search(query, k=max(1, min(int(k), 5)))
    return _ok(results=hits, found=bool(hits))


def create_support_ticket(ctx: ToolContext, subject: str, description: str,
                          priority: str = "normal", order_id: str | None = None) -> dict:
    if priority not in {"low", "normal", "high", "urgent"}:
        priority = "normal"
    oid = None
    if order_id:
        order, err = _owned_order(ctx, order_id)
        if err:
            return err
        oid = order["order_id"]
    ticket = ctx.store.add_ticket({
        "customer_email": ctx.customer_email,
        "subject": redact(str(subject))[:120],
        "description": redact(str(description))[:2000],
        "priority": priority,
        "order_id": oid,
        "status": "open",
    })
    return _ok(ticket_id=ticket["ticket_id"], priority=priority, status="open",
               response_time="within 1 business day" if priority in {"normal", "low"} else "within 4 hours")


# ------------------------------------------------------------------------ registry
@dataclass
class Tool:
    fn: Callable
    description: str
    properties: dict
    required: list[str]
    sensitive: bool = False

    def spec(self, name: str) -> dict:   # Anthropic tool-use format
        return {
            "name": name,
            "description": self.description,
            "input_schema": {"type": "object", "properties": self.properties, "required": self.required},
        }


TOOLS: dict[str, Tool] = {
    "list_orders": Tool(
        list_orders,
        "List the signed-in customer's orders (newest first). Use when the customer doesn't say which order.",
        {}, []),
    "get_order_status": Tool(
        get_order_status,
        "Get status, carrier, tracking and expected delivery for one of the customer's orders.",
        {"order_id": {"type": "string", "description": "Order ID such as ORD-1001"}}, ["order_id"]),
    "cancel_order": Tool(
        cancel_order,
        "Request cancellation of an order. Only 'processing' orders are eligible. The customer is "
        "asked to confirm before anything is cancelled, so call this as soon as they ask to cancel.",
        {"order_id": {"type": "string", "description": "Order ID such as ORD-1002"}}, ["order_id"],
        sensitive=True),
    "check_refund_status": Tool(
        check_refund_status,
        "Check the refund status, amount and expected date for an order.",
        {"order_id": {"type": "string", "description": "Order ID such as ORD-1003"}}, ["order_id"]),
    "search_products": Tool(
        search_products,
        "Search the product catalogue by keywords. Returns price and stock.",
        {"query": {"type": "string"},
         "max_price": {"type": "number", "description": "Optional maximum price in USD"},
         "in_stock_only": {"type": "boolean"},
         "limit": {"type": "integer"}}, ["query"]),
    "search_policy": Tool(
        search_policy,
        "Search company policies (returns, refunds, cancellation, shipping, warranty). "
        "Always use this for policy questions and quote only what it returns.",
        {"query": {"type": "string"}}, ["query"]),
    "create_support_ticket": Tool(
        create_support_ticket,
        "Escalate to a human agent by creating a ticket. Use when you can't resolve the issue, "
        "the customer asks for a human, or something needs manual action (e.g. missing refund, damaged item).",
        {"subject": {"type": "string"},
         "description": {"type": "string", "description": "Summary of the issue and what was already tried"},
         "priority": {"type": "string", "enum": ["low", "normal", "high", "urgent"]},
         "order_id": {"type": "string", "description": "Related order, if any"}},
        ["subject", "description"]),
}

TOOL_SPECS = [t.spec(name) for name, t in TOOLS.items()]
