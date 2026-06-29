"""Fixture exercising the HTTP-API layer: a route, an in-repo caller,
an external call, and a call to a route nobody exposes."""
import requests
from fastapi import FastAPI

app = FastAPI()


@app.post("/api/login")
def login(user: str):
    return {"ok": True}


@app.get("/api/users/{user_id}")
def get_user(user_id: int):
    return {"id": user_id}


def client_flow():
    # in-repo: matches POST /api/login exposed above
    requests.post("/api/login", json={"user": "a"})
    # in-repo templated: matches GET /api/users/{user_id}
    requests.get("/api/users/42")
    # external: a third-party API (Stripe)
    requests.post("https://api.stripe.com/v1/charges", data={})
    # missing: no backend exposes this route
    requests.get("/api/orders")
