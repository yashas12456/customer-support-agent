from support_agent.tools import (ToolContext, cancel_order, check_refund_status, create_support_ticket,
                                 get_order_status, list_orders, search_policy, search_products)


def ctx(store, policies, email="alex@example.com"):
    return ToolContext(store=store, policies=policies, customer_email=email)


def test_order_status_structured(store, policies):
    r = get_order_status(ctx(store, policies), "ORD-1001")
    assert r["ok"] and r["status"] == "shipped"
    assert r["carrier"] == "SwiftShip" and r["expected_delivery"] == "2026-10-10"
    assert r["can_cancel"] is False


def test_order_id_normalised(store, policies):
    assert get_order_status(ctx(store, policies), "my order is ord-1001 thanks")["ok"]


def test_cannot_read_other_customers_order(store, policies):
    r = get_order_status(ctx(store, policies), "ORD-2001")       # belongs to priya
    assert not r["ok"] and r["error"] == "order_not_found"


def test_missing_and_foreign_orders_look_identical(store, policies):
    a = get_order_status(ctx(store, policies), "ORD-2001")
    b = get_order_status(ctx(store, policies), "ORD-9999")
    assert a["error"] == b["error"]          # no order-ID enumeration


def test_cannot_cancel_other_customers_order(store, policies):
    r = cancel_order(ctx(store, policies), "ORD-2001")
    assert not r["ok"]
    assert store.orders["ORD-2001"]["status"] == "out_for_delivery"


def test_cancel_only_when_processing(store, policies):
    c = ctx(store, policies)
    assert cancel_order(c, "ORD-1001")["error"] == "not_cancellable"       # shipped
    assert cancel_order(c, "ORD-1003")["error"] == "not_cancellable"       # delivered
    dry = cancel_order(c, "ORD-1002", dry_run=True)
    assert dry["ok"] and dry["dry_run"]
    assert store.orders["ORD-1002"]["status"] == "processing"              # dry run changes nothing
    done = cancel_order(c, "ORD-1002")
    assert done["ok"] and store.orders["ORD-1002"]["status"] == "cancelled"
    assert store.orders["ORD-1002"]["refund"]["amount"] == 84.50
    assert cancel_order(c, "ORD-1002")["error"] == "already_cancelled"


def test_refund_status(store, policies):
    c = ctx(store, policies)
    r = check_refund_status(c, "ORD-1003")
    assert r["has_refund"] and r["status"] == "processing" and r["amount"] == 59.99
    assert check_refund_status(c, "ORD-1001")["has_refund"] is False


def test_list_orders_scoped_to_customer(store, policies):
    ids = {o["order_id"] for o in list_orders(ctx(store, policies))["orders"]}
    assert ids == {"ORD-1001", "ORD-1002", "ORD-1003"}


def test_product_search_price_and_stock(store, policies):
    c = ctx(store, policies)
    r = search_products(c, "wireless earbuds", max_price=60)["results"]
    assert [p["name"] for p in r] == ["AuraBuds Lite Earbuds"]
    oos = search_products(c, "fitness tracker")["results"][0]
    assert oos["name"] == "PulseBand Fitness Tracker" and oos["in_stock"] is False
    assert search_products(c, "xylophone")["results"] == []


def test_ticket_created_and_pii_redacted(store, policies):
    r = create_support_ticket(ctx(store, policies), "Help", "card 4111 1111 1111 1111 mail a@b.com",
                              priority="bogus", order_id="ORD-1001")
    assert r["ok"] and r["priority"] == "normal"
    t = store.tickets[0]
    assert "4111" not in t["description"] and "a@b.com" not in t["description"]
    assert not create_support_ticket(ctx(store, policies), "x", "y", order_id="ORD-2001")["ok"]


def test_policy_search(store, policies):
    r = search_policy(ctx(store, policies), "how many days do I have to return an item")
    assert r["found"] and r["results"][0]["section"] == "Return window"
    assert search_policy(ctx(store, policies), "do you price match competitors")["found"] is False
