"""CockroachDB access. Replaces Cosmos DB, Azure Table Storage and the local JSON files.

Every write path that does read-modify-write must be wrapped in @retry, because
CockroachDB runs SERIALIZABLE by default and will raise 40001 under contention
rather than silently interleaving.
"""
import os
import time
import random
import functools
import logging

log = logging.getLogger(__name__)

DSN = os.environ.get("CRDB_DSN", "")

_pool = None
_unavailable_logged = False


def available() -> bool:
    """True when a DSN is configured and psycopg is importable."""
    if not DSN:
        return False
    try:
        import psycopg  # noqa: F401
        return True
    except ImportError:
        return False


def get_pool():
    """Lazy pool so importing this module never blocks on a network call."""
    global _pool
    if _pool is None:
        from psycopg_pool import ConnectionPool
        from psycopg.rows import dict_row
        _pool = ConnectionPool(
            DSN,
            min_size=1,
            max_size=int(os.environ.get("CRDB_POOL_MAX", "16")),
            kwargs={"row_factory": dict_row, "application_name": "complianceforge"},
            open=True,
        )
    return _pool


def retry(max_attempts: int = 5):
    """Exponential backoff on CockroachDB serialization failures (SQLSTATE 40001)."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            try:
                import psycopg
                serialization_failure = psycopg.errors.SerializationFailure
            except ImportError:
                # Local-file mode: nothing to retry against.
                return fn(*a, **kw)

            last = None
            for attempt in range(max_attempts):
                try:
                    return fn(*a, **kw)
                except serialization_failure as exc:
                    last = exc
                    if attempt == max_attempts - 1:
                        break
                    time.sleep((2 ** attempt) * 0.05 + random.random() * 0.05)
            raise last
        return wrapper
    return deco


def query(sql: str, params=None) -> list:
    with get_pool().connection() as conn:
        return conn.execute(sql, params or ()).fetchall()


def one(sql: str, params=None):
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params=None) -> None:
    with get_pool().connection() as conn:
        conn.execute(sql, params or ())


def executemany(sql: str, rows) -> None:
    with get_pool().connection() as conn, conn.cursor() as cur:
        cur.executemany(sql, rows)


def as_of(sql: str, params, timestamp: str) -> list:
    """Regulator replay. Reads the database exactly as it stood at `timestamp`.

    `sql` must contain the marker {aost}, placed immediately after the table
    name, e.g. "SELECT * FROM verdicts {aost} WHERE tx_id = %s".

    The clause is inlined rather than issued as SET TRANSACTION, because that
    statement has to be the first in its transaction and pooled connections
    make that fragile. `timestamp` is anything CockroachDB accepts after
    AS OF SYSTEM TIME: '2026-08-18 09:15:00+00:00', '-30s', or a decimal HLC.
    """
    ts = str(timestamp).replace("'", "")
    rendered = sql.format(aost=f"AS OF SYSTEM TIME '{ts}'")
    with get_pool().connection() as conn:
        return conn.execute(rendered, params or ()).fetchall()


def vec(embedding) -> str:
    """Python float list -> CockroachDB VECTOR literal."""
    return "[" + ",".join(f"{float(x):.6f}" for x in embedding) + "]"


def warn_unavailable(component: str) -> None:
    """One-line warning the first time a component falls back to local files."""
    global _unavailable_logged
    if not _unavailable_logged:
        log.warning(
            "CRDB_DSN not set or psycopg missing — %s is running on local files. "
            "Set CRDB_DSN to use CockroachDB.", component
        )
        _unavailable_logged = True
