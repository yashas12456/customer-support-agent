"""A tiny data layer standing in for the real order / catalogue / ticketing APIs.

Swap this class for HTTP clients (Shopify, Zendesk, internal OMS ...) in production;
the tools only depend on the methods defined here.
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import DATA_DIR, RUNTIME_DIR


class Store:
    def __init__(self, data_dir: Path = DATA_DIR, runtime_dir: Path | None = RUNTIME_DIR):
        """runtime_dir=None keeps everything in memory (used by tests)."""
        self.runtime_dir = Path(runtime_dir) if runtime_dir else None
        orders_file = data_dir / "orders.json"
        if self.runtime_dir and (self.runtime_dir / "orders.json").exists():
            orders_file = self.runtime_dir / "orders.json"   # state from earlier runs
        self.orders: dict[str, dict] = {
            o["order_id"]: o for o in json.loads(orders_file.read_text())
        }
        self.products: list[dict] = json.loads((data_dir / "products.json").read_text())
        self.tickets: list[dict] = []
        tickets_file = self.runtime_dir / "tickets.json" if self.runtime_dir else None
        if tickets_file and tickets_file.exists():
            self.tickets = json.loads(tickets_file.read_text())

    # ---- orders -------------------------------------------------------
    def get_order(self, order_id: str) -> dict | None:
        return self.orders.get(order_id)

    def orders_for(self, email: str) -> list[dict]:
        email = email.lower()
        return [o for o in self.orders.values() if o["customer_email"].lower() == email]

    # ---- tickets ------------------------------------------------------
    def add_ticket(self, ticket: dict) -> dict:
        ticket = {"ticket_id": f"TKT-{10001 + len(self.tickets)}", **ticket}
        self.tickets.append(ticket)
        self.save()
        return ticket

    # ---- persistence --------------------------------------------------
    def save(self) -> None:
        if not self.runtime_dir:
            return
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        (self.runtime_dir / "orders.json").write_text(
            json.dumps(list(self.orders.values()), indent=2)
        )
        (self.runtime_dir / "tickets.json").write_text(json.dumps(self.tickets, indent=2))
