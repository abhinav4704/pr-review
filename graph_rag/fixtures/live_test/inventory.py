"""In-memory inventory tracking."""
_stock = {"widget": 20, "gadget": 5}


def reserve_stock(item_id, qty):
    available = _stock.get(item_id, 0)
    if available < qty:
        return False
    _stock[item_id] = available - qty
    return True
