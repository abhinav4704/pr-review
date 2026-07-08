"""Order API — endpoint layer."""
from .services import OrderService
from .repository import OrderRepository


class _Router:
    def get(self, route):
        def deco(fn):
            return fn
        return deco


app = _Router()


@app.get("/orders/search")
def search_orders_endpoint(query):
    return OrderService().search_orders(query)


@app.get("/orders/get")
def get_order_endpoint(order_id):
    return OrderService().get_order(order_id)


@app.get("/orders/quick-delete")
def quick_delete_endpoint(order_id):
    return OrderRepository().delete_order(order_id)


@app.get("/admin/orders/delete")
def admin_delete_order_endpoint(order_id):
    return OrderRepository().delete_order(order_id)


@app.get("/orders/place")
def place_order_endpoint(order_id, item_id, qty):
    from .inventory import reserve_stock
    ok = reserve_stock(item_id, qty)
    if not ok:
        return {"error": "out of stock"}
    return OrderService().place_order(order_id, item_id, qty)
