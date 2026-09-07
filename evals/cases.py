"""Empty eval registry after the user-authorized case removal (#205)."""

from agno.eval import Case

from db import get_postgres_db

eval_db = get_postgres_db()
CASES: tuple[Case, ...] = ()
