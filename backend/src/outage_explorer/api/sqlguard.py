"""Static checks on user SQL (US-04, US-05): what may run, and every table it reads.

The SQL is parsed with sqlglot (DuckDB dialect) and accepted only if:
- it is exactly one statement, and that statement is a query: SELECT, or UNION / INTERSECT / EXCEPT of
  SELECTs, optionally with CTEs (WITH, WITH RECURSIVE) and subqueries anywhere;
- nothing inside it writes, changes settings or describes the engine (INSERT, CREATE, SET, PRAGMA,
  ATTACH, COPY, DESCRIBE, ...);
- every FROM / JOIN source is a plain table name: no table functions (read_parquet, glob, range,
  duckdb_tables, query_table...), no file paths, no schema- or catalog-qualified names;
- no function that reads files is called.

Every table reference is then resolved: either to a CTE visible at that point of the query, or to a
model table. The model tables are returned so the caller can check each one against the user's grant.

What runs is the SQL regenerated from the checked tree, not the text the user sent, so the engine
executes exactly what was checked even where sqlglot and DuckDB would read the text differently.
"""

from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

MAX_SQL_LENGTH = 20_000

# Any of these anywhere in the tree rejects the statement (getattr: not every sqlglot version has all).
_FORBIDDEN = tuple(
    cls
    for name in (
        "Insert",
        "Update",
        "Delete",
        "Merge",
        "Create",
        "Drop",
        "Alter",
        "TruncateTable",
        "Copy",
        "Command",
        "Set",
        "Pragma",
        "Attach",
        "Detach",
        "Use",
        "Describe",
        "Summarize",
        "Transaction",
        "Commit",
        "Rollback",
        "Into",
        "Install",
        "Load",
        "Export",
        "Analyze",
    )  # fmt: skip
    if (cls := getattr(exp, name, None)) is not None
)

# Functions that reach outside the allowed tables. The serving sandbox blocks them anyway (no file
# access, DECISIONS #21); rejecting them here gives a clear error instead of an engine one.
_FILE_FUNCTION_PREFIXES = ("read_", "parquet_", "sniff_", "glob", "getenv", "query", "duckdb_")


class SqlRejected(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass(frozen=True)
class CheckedQuery:
    tree: exp.Query
    tables: frozenset[str]  # every model table the query reads, lower case

    def sql(self) -> str:
        return self.tree.sql(dialect="duckdb")


def check(sql: str, known_tables: set[str]) -> CheckedQuery:
    """Parse and check `sql`; raises SqlRejected with a stable code and a message for the user."""
    if len(sql) > MAX_SQL_LENGTH:
        raise SqlRejected("query_too_long", f"queries are limited to {MAX_SQL_LENGTH} characters")
    try:
        statements = [s for s in sqlglot.parse(sql, read="duckdb") if s is not None]
    except SqlglotError as exc:
        raise SqlRejected("syntax_error", f"could not parse the query: {_first_line(exc)}") from None
    if len(statements) != 1:
        raise SqlRejected("one_statement_only", "send exactly one statement")
    tree = statements[0]

    if not isinstance(tree, exp.Select | exp.SetOperation):
        raise SqlRejected("read_only", f"only SELECT queries are allowed, not {_kind(tree)}")
    for node in tree.walk():
        if isinstance(node, _FORBIDDEN):
            raise SqlRejected("read_only", f"only SELECT queries are allowed; found {_kind(node)}")
        if isinstance(node, exp.Func) and _function_name(node).startswith(_FILE_FUNCTION_PREFIXES):
            raise SqlRejected("function_not_allowed", f"function '{_function_name(node)}' is not allowed")

    tables: set[str] = set()
    for table in tree.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            raise SqlRejected("source_not_allowed", "FROM accepts table names only, not table functions")
        if table.args.get("db") or table.args.get("catalog"):
            raise SqlRejected("source_not_allowed", f"use plain table names, not '{table.sql('duckdb')}'")
        name = table.name.lower()
        if _is_cte_reference(table, name):
            continue
        if name not in known_tables:
            raise SqlRejected("unknown_table", f"no dataset named '{table.name}'")
        tables.add(name)
    return CheckedQuery(tree, frozenset(tables))


def _is_cte_reference(table: exp.Table, name: str) -> bool:
    """True if `name` at this point of the query means a CTE rather than a table.

    Walks up from the reference. A WITH attached to an ancestor makes its CTEs visible, except that
    inside a CTE's own body only the CTEs defined before it are (and itself, under WITH RECURSIVE), as
    in DuckDB. So `WITH t AS (SELECT * FROM fact_generator_daily) ...` checks the inner table, and a
    CTE in one subquery never hides a real table of the same name elsewhere.
    """
    child, node = table, table.parent
    while node is not None:
        if isinstance(node, exp.With):  # we came up through one of its CTEs
            ctes = node.expressions
            visible = ctes[: ctes.index(child) + (1 if node.args.get("recursive") else 0)]
            if any(c.alias_or_name.lower() == name for c in visible):
                return True
        else:
            # Found by type, not by arg name: sqlglot renamed "with" to "with_" in v30.
            with_ = next((v for v in node.args.values() if isinstance(v, exp.With)), None)
            if with_ is not None and with_ is not child:
                if any(c.alias_or_name.lower() == name for c in with_.expressions):
                    return True
        child, node = node, node.parent
    return False


def with_row_cap(query: CheckedQuery, cap: int) -> CheckedQuery:
    """The same query returning at most `cap` rows (an existing smaller LIMIT is kept)."""
    tree = query.tree.copy()
    limit = tree.args.get("limit")
    current = limit.expression if isinstance(limit, exp.Limit) else None
    if isinstance(current, exp.Literal) and current.is_int and int(current.this) <= cap:
        return query
    if limit is None or isinstance(current, exp.Literal):
        tree.set("limit", exp.Limit(expression=exp.Literal.number(cap)))
    else:  # LIMIT <expression>: keep it, cap the result from outside
        tree = exp.select("*").from_(tree.subquery("capped")).limit(cap)
    return CheckedQuery(tree, query.tables)


def _function_name(node: exp.Func) -> str:
    return (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).lower()


def _kind(node: exp.Expression) -> str:
    return node.key.upper()


def _first_line(exc: Exception) -> str:
    return str(exc).splitlines()[0][:200]
