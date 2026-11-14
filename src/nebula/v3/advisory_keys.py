"""Single-integer PostgreSQL advisory keys shared by host, API and workers."""

from __future__ import annotations

# Execution workers hold this key shared for every transaction; a host Work
# batch takes it exclusively. Older releases take it exclusively too, so it
# keeps its single-integer form.
EXECUTION_SQL_KEY = "hashtext('nebula:execution-sql:v1')"


def per_id_key(parameter: str) -> str:
    """``hashtext(:parameter)`` as a single-integer advisory key.

    An id whose hash equals ``EXECUTION_SQL_KEY`` would turn its lock, taken
    inside an execution transaction that already holds that key shared, into
    a shared-to-exclusive upgrade that deadlocks against other workers. Such
    ids lock a fixed alternate key instead; every caller uses this form.
    """

    return (
        f"CASE WHEN hashtext(:{parameter}) = {EXECUTION_SQL_KEY} "
        f"THEN hashtext('nebula:execution-sql:v1:alternate') "
        f"ELSE hashtext(:{parameter}) END"
    )
