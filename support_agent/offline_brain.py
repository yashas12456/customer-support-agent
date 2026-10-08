"""Offline brain: deterministic intent routing, no API key needed.

Lets the whole project be demoed and tested without an LLM. It exercises exactly the same
tools and safety layer as the LLM brain: intent -> tool call -> structured data -> reply.
"""

from __future__ import annotations

import re
from datetime import date

from .tools import ORDER_ID_RE

_RULES = [  # order matters: first match wins
    ("cancel", r"\bcancel"),
    ("refund", r"\brefund|money back|reimburs"),
    ("ticket", r"\b(open|create|raise|file|make) (a |an )?(ticket|complaint|case)|\bcomplain"),
    ("order_status", r"\b(track|tracking|status|where('s| is| are)|delivery|deliver|arrive|arrival|"
                     r"shipped|shipping status|when (will|does|is)|package|parcel|my order|order status)\b"),
    ("policy", r"\b(polic(y|ies)|price match|price adjustment|discount|coupon|promo code|return|exchange|warranty|guarantee|shipping|international|"
               r"how long|how do i|can i)\b"),
    ("product", r"\b(do you (have|sell|stock)|looking for|recommend|search|show me|under|below|"
                r"cheapest|buy|price|in stock|available)\b"),
    ("greeting", r"^(hi|hello|hey|good (morning|afternoon|evening)|thanks|thank you)\b"),
]


def classify(text: str, has_order_id: bool) -> str:
    low = text.lower()
    for intent, pattern in _RULES:
        if re.search(pattern, low):
            # "how long do refunds take?" is a policy question; "where is my refund?" is a status check
            if intent == "refund" and not has_order_id and not re.search(
                    r"\b(my|the) refund|where('s| is)|status|refund for", low):
                return "policy"
            if intent == "order_status" and re.search(r"\b(polic|how long does shipping|international)", low):
                return "policy"
            return intent
    return "fallback"


def extract_order_id(text: str) -> str | None:
    m = ORDER_ID_RE.search(text)
    return m.group(0).upper() if m else None


def fmt_date(iso: str | None) -> str:
    if not iso:
        return "not available yet"
    try:
        return date.fromisoformat(iso[:10]).strftime("%a %d %b %Y")
    except ValueError:
        return iso


class OfflineBrain:
    name = "offline-rules"

    def respond(self, message: str, history: list[dict], execute, session: dict) -> str:
        oid = extract_order_id(message)
        if oid:
            session["last_order_id"] = oid
        intent = classify(message, bool(oid))
        if intent != "fallback":
            session["fallbacks"] = 0
        return getattr(self, f"_{intent}")(message, execute, session)

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _resolve_order(execute, session, verb: str):
        """Return (order_id, None) or (None, reply asking the customer which order)."""
        if session.get("last_order_id"):
            return session["last_order_id"], None
        orders = execute("list_orders", {}).get("orders", [])
        if not orders:
            return None, "I couldn't find any orders on your account."
        if len(orders) == 1:
            return orders[0]["order_id"], None
        lines = [f"- {o['order_id']}: {', '.join(o['items'])} ({o['status_label']})" for o in orders]
        return None, f"Which order would you like to {verb}?\n" + "\n".join(lines)

    # ---------------------------------------------------------------- intents
    def _order_status(self, message, execute, session) -> str:
        oid, ask = self._resolve_order(execute, session, "check")
        if ask:
            return ask
        res = execute("get_order_status", {"order_id": oid})
        if not res["ok"]:
            session.pop("last_order_id", None)
            return res["message"]
        items = ", ".join(res["items"])
        head = f"Order {res['order_id']} ({items}) is {res['status_label'].lower()}."
        status = res["status"]
        if status == "processing":
            return (f"{head} We're preparing it for dispatch. Estimated delivery: "
                    f"{fmt_date(res['expected_delivery'])}. You can still cancel it if you need to.")
        if status == "shipped":
            return (f"{head} It's in transit with {res['carrier']} (tracking {res['tracking_number']}). "
                    f"Latest update: {res['last_update']}. Expected delivery: {fmt_date(res['expected_delivery'])}.")
        if status == "out_for_delivery":
            return (f"{head} It's with {res['carrier']} (tracking {res['tracking_number']}) and should "
                    f"arrive today, {fmt_date(res['expected_delivery'])}.")
        if status == "delivered":
            return f"{head} It was delivered on {fmt_date(res['delivered_at'])}."
        return f"{head} {res['last_update']}."

    def _cancel(self, message, execute, session) -> str:
        oid, ask = self._resolve_order(execute, session, "cancel")
        if ask:
            return ask
        res = execute("cancel_order", {"order_id": oid})
        if not res["ok"]:
            return res["message"]
        return res["message"].split(". Ask the customer")[0] + (
            ". Shall I go ahead? Reply 'yes' to confirm or 'no' to keep the order.")

    def _refund(self, message, execute, session) -> str:
        oid, ask = self._resolve_order(execute, session, "check a refund for")
        if ask:
            return ask
        res = execute("check_refund_status", {"order_id": oid})
        if not res["ok"]:
            return res["message"]
        if not res["has_refund"]:
            return (f"There's no refund on file for order {res['order_id']} (it's currently "
                    f"{res['order_status'].replace('_', ' ')}). If you'd like to return an item, "
                    f"I can explain how, just ask about our returns policy.")
        amt = f"{res['amount']:.2f} {res['currency']}"
        status = res["status"]
        when = fmt_date(res["expected_by"]) if res["expected_by"][:2].isdigit() else res["expected_by"]
        if status == "completed":
            return (f"Your refund of {amt} for order {res['order_id']} was completed. It was sent to your "
                    f"{res['method']}; if it hasn't appeared yet, your bank may need a few more days.")
        return (f"Your refund of {amt} for order {res['order_id']} is {status} ({res['reason'].lower()}). "
                f"It's going back to your {res['method']} and should arrive by {when}.")

    def _policy(self, message, execute, session) -> str:
        res = execute("search_policy", {"query": message})
        hits = res.get("results", [])
        if not hits:
            return ("I don't have information on that in our policies. I can create a support ticket "
                    "so a human agent can answer. Would you like that?")
        close = [h for h in hits if h["score"] >= 0.85 * hits[0]["score"]][:2]   # near-ties: show both
        parts = []
        for h in close:
            text = h["text"] if len(h["text"]) < 400 else h["text"][:400].rsplit(" ", 1)[0] + "..."
            parts.append(f"{text}\n(Source: {h['source']} > {h['section']})")
        return "\n\n".join(parts)

    def _product(self, message, execute, session) -> str:
        args = {"query": message, "limit": 3}
        m = re.search(r"(?:under|below|less than|max|<)\s*\$?(\d+(?:\.\d+)?)", message.lower())
        if m:
            args["max_price"] = float(m.group(1))
        res = execute("search_products", args)
        items = res.get("results", [])
        if not items:
            return "I couldn't find a matching product. Try different keywords, or tell me what you need it for."
        lines = [f"- {p['name']}: ${p['price']:.2f} ({'in stock' if p['in_stock'] else 'out of stock'})"
                 for p in items]
        return "Here's what I found:\n" + "\n".join(lines)

    def _ticket(self, message, execute, session) -> str:
        res = execute("create_support_ticket", {
            "subject": "Customer request", "description": message, "priority": "normal",
            "order_id": session.get("last_order_id") or ""})
        if not res["ok"]:
            return res["message"]
        return f"I've created ticket {res['ticket_id']}. A human agent will reply {res['response_time']}."

    def _greeting(self, message, execute, session) -> str:
        if re.search(r"thank", message.lower()):
            return "You're welcome! Is there anything else I can help with?"
        return ("Hi! I can check order status, cancel eligible orders, track refunds, find products, "
                "and answer questions about returns, shipping and warranty. What do you need?")

    def _fallback(self, message, execute, session) -> str:
        session["fallbacks"] = session.get("fallbacks", 0) + 1
        if session["fallbacks"] >= 2:
            session["fallbacks"] = 0
            return self._ticket(message, execute, session) + " Sorry I couldn't help directly."
        return ("I'm not sure I understood. I can help with order status, cancellations, refunds, "
                "products, and policies. Could you tell me a bit more?")
