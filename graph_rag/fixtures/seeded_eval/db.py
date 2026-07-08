"""Data-access layer for the seeded eval branch."""
import sqlite3

conn = sqlite3.connect(":memory:")


def fetch_rows(filter_str):
    """Seed #1 sink: filter_str is attacker-controlled by the time it gets
    here (see api.search_endpoint -> service.run_search)."""
    sql = f"SELECT * FROM docs WHERE name = '{filter_str}'"
    return conn.execute(sql)


def export_report():
    """Clean control: same sink family, but the query is hardcoded — taint
    composition must NOT flag this call as a source of the seed #1 finding."""
    sql = "SELECT * FROM docs WHERE status = 'done'"
    return conn.execute(sql)
