"""Order service fixture.

Deliberately exercises the semantic edges added in the events/auth/architecture
pass so that `validate_fixtures.py` can lock them with regression assertions:

  - REQUIRES_AUTH      via @requires_auth
  - ENFORCES_POLICY    via @require_role("admin")
  - CONSUMES_EVENT     via @kafka_listener("OrderShipped")  and  bus.subscribe(...)
  - EMITS_EVENT        via events.publish("OrderPlaced")
  - PASSES             via validate(cart, customer)
  - USES (cross-module) via the call into the `billing` module's charge()
  - DEFINES / BELONGS_TO are emitted structurally for every node here.
"""
from billing.gateway import charge


def validate(cart, customer):
    return cart is not None and customer is not None


@requires_auth
@require_role("admin")
def place_order(cart, customer):
    validate(cart, customer)            # CALLS + PASSES (identifier args)
    charge(customer, cart)              # cross-module CALLS -> USES orders->billing
    events.publish("OrderPlaced")       # EMITS_EVENT "OrderPlaced"
    return cart


@kafka_listener("OrderShipped")
def on_order_shipped(record):
    archive(record)                     # in-module CALLS
    bus.subscribe("OrderShipped")       # CONSUMES_EVENT via call form
    tracer.stamp(record)                # unknown receiver -> REFERENCES (audit.log.stamp)


def archive(record):
    return record


def requeue_order(queue):
    queue.emit("order_placed")    # EMITS_EVENT "order_placed" -> normalizes to order.placed
