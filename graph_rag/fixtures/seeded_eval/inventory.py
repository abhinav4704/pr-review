"""Inventory fixture for the seeded eval branch."""

stock = {"widget": 10}


def reserve_stock(item_id, qty):
    """Seed #5: correctness/race_condition (RMW/TOCTOU) — reads then writes
    the shared `stock` dict with no lock/transaction between the two steps."""
    available = stock.get(item_id, 0)
    if available < qty:
        raise ValueError("not enough stock")
    stock[item_id] = available - qty
    return stock[item_id]
