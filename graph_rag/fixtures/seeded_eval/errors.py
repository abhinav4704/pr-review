"""Config-loading fixture for the seeded eval branch."""
import json


def parse_config(path):
    """Seed #6: correctness/bad_error_handling — swallows every exception
    (including bugs in json.load itself) and silently returns {}."""
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        pass
    return {}
