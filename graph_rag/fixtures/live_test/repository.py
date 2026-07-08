"""Raw data-access layer — every method here talks to the DB directly."""
import sqlite3

conn = sqlite3.connect(":memory:")


class OrderRepository:
    def fetch_order(self, order_id):
        sql = f"SELECT * FROM orders WHERE id = {order_id}"
        return list(conn.execute(sql))

    def search(self, cleaned_term):
        sql = f"SELECT * FROM orders WHERE name LIKE '%{cleaned_term}%'"
        return list(conn.execute(sql))

    def delete_order(self, order_id):
        sql = f"DELETE FROM orders WHERE id = {order_id}"
        conn.execute(sql)
        return True

    def save_order(self, order_id, item_id, qty):
        sql = f"INSERT INTO orders (id, item_id, qty) VALUES ({order_id}, {item_id}, {qty})"
        conn.execute(sql)
        return True

    def log_notification(self, order_id):
        from .services import OrderService
        OrderService().notify(order_id)
        return True
