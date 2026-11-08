"""SQLite (staging tables + LLM cache) and Neo4j connections, plus batched Cypher helpers."""
import sqlite3
import threading
from neo4j import GraphDatabase
from config import BATCH_SIZE, NEO4J_DB, NEO4J_PASSWORD, NEO4J_URI, NEO4J_USER, SQLITE_PATH

db = sqlite3.connect(SQLITE_PATH, check_same_thread=False)
db.execute("CREATE TABLE IF NOT EXISTS llm_cache (key TEXT PRIMARY KEY, model TEXT, prompt_version TEXT, tag TEXT, response TEXT)")
driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
lock = threading.Lock()  # one SQLite connection is shared by the scoring worker threads


def run_batches(query, rows, **params):
    for i in range(0, len(rows), BATCH_SIZE):
        driver.execute_query(query, rows=rows[i:i + BATCH_SIZE], database_=NEO4J_DB, **params)


def run_cypher_file(path):
    text = "\n".join(l for l in open(path).read().splitlines() if not l.strip().startswith("//"))
    out = []
    for stmt in [s.strip() for s in text.split(";") if s.strip()]:
        records, summary, keys = driver.execute_query(stmt, database_=NEO4J_DB)
        out.append({"query": stmt[:120], "rows": [r.data() for r in records]})
    return out
