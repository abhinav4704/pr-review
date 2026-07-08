"""Order business logic layer."""
from .repository import OrderRepository


def normalize_term(term):
    return term.strip().lower()


class OrderService:
    def search_orders(self, term):
        cleaned = normalize_term(term)
        return OrderRepository().search(cleaned)

    def get_order(self, order_id):
        rows = OrderRepository().fetch_order(order_id)
        return rows[0]

    def place_order(self, order_id, item_id, qty):
        return OrderRepository().save_order(order_id, item_id, qty)

    def notify(self, order_id):
        OrderRepository().log_notification(order_id)
        return True
