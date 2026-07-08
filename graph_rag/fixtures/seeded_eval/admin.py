"""Admin fixture for the seeded eval branch."""
from .api import app
from .db import conn


@app.get("/admin/users/delete")
def delete_user_endpoint(user_id):
    """Seed #7: security/missing_authorization — a destructive admin action
    with no auth/permission check anywhere on this path (contrast with
    fixtures/orders/service.py's `place_order`, which has @requires_auth)."""
    conn.execute(f"DELETE FROM users WHERE id = {user_id}")
    return True
