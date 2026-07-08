"""Ops/diagnostics layer for the seeded eval branch."""
import os


def run_diagnostic(hostname):
    """Seed #2 sink: hostname is attacker-controlled by the time it gets here
    (see api.diagnostics_endpoint)."""
    cmd = f"ping -c 1 {hostname}"
    return os.system(cmd)
