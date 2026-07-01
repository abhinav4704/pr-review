"""Billing gateway fixture.

A separate module so that the call from `orders.service.place_order` into
`charge` here crosses a module boundary and materializes a component-level and
module-level USES edge (orders -> billing).
"""


def charge(customer, cart):
    return _record(customer, cart)


def _record(customer, cart):
    return {"customer": customer, "cart": cart}
