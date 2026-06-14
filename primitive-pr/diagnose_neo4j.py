"""Quick Neo4j connectivity check.

Usage (from primitive-pr/):
    NEO4J_URI=bolt://localhost:7687 NEO4J_USER=neo4j NEO4J_PASSWORD=yourpass \
        ../venv/bin/python diagnose_neo4j.py
"""
import os

from neo4j import GraphDatabase

uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
user = os.environ.get("NEO4J_USER", "neo4j")
pwd = os.environ.get("NEO4J_PASSWORD", "")

print(f"Connecting to {uri} as {user!r} ...")
driver = GraphDatabase.driver(uri, auth=(user, pwd))

try:
    driver.verify_connectivity()
    print("✓ bolt connectivity OK")
except Exception as e:
    print(f"✗ connectivity failed: {e}")

# List databases (queried against the system database, which is always up)
try:
    with driver.session(database="system") as s:
        print("\nDatabases:")
        for r in s.run("SHOW DATABASES"):
            print(f"  - name={r['name']!r:20} "
                  f"currentStatus={r.get('currentStatus')!r:12} "
                  f"default={r.get('default')} address={r.get('address')}")
except Exception as e:
    print(f"✗ SHOW DATABASES failed: {e}")

# Try the actual target database the app uses
try:
    with driver.session(database="neo4j") as s:
        n = s.run("RETURN 1 AS x").single()["x"]
        print(f"\n✓ query against database 'neo4j' OK (got {n})")
except Exception as e:
    print(f"\n✗ query against database 'neo4j' failed: {e}")

driver.close()
