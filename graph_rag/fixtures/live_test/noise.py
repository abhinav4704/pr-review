"""Noise fixture — tests that low-signal heuristics are correctly downgraded.

  session.get("cache_key")  must NOT create a CALLS_API/Endpoint node.
  smtp.send("hello")        emits EMITS_EVENT only as AMBIGUOUS/fuzzy_name.
  button.on("click")        emits CONSUMES_EVENT only as AMBIGUOUS/fuzzy_name.
  helpful_thing()           must NOT get component_role "helper".
"""
import requests

session = requests.Session()


def cache_lookup(key):
    return session.get("cache_key")


def notify(smtp, button):
    smtp.send("hello")
    button.on("click")


def helpful_thing():
    return 1
