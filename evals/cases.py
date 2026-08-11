"""Project eval registry; product-agent cases are added with each agent."""

from agno.eval import Case

from db import get_postgres_db

eval_db = get_postgres_db()

# The bootstrap assistant is smoke-tested over REST and MCP. Production agents add
# behavior cases here as part of their own delivery.
CASES: tuple[Case, ...] = ()
