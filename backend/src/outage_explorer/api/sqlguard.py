"""Static checks on user SQL (US-04, US-05): what may run, and every table it reads.

The SQL is parsed with sqlglot (DuckDB dialect) and accepted only if:
- it is exactly one statement, and that statement is a query: SELECT, or UNION / INTERSECT / EXCEPT of
  SELECTs, optionally with CTEs (WITH, WITH RECURSIVE) and subqueries anywhere;
- nothing inside it writes, changes settings or describes the engine (INSERT, CREATE, SET, PRAGMA,
  ATTACH, COPY, DESCRIBE, ...);
- every FROM / JOIN source is a plain table name: no table functions (read_parquet, glob, range,
  duckdb_tables, query_table...), no file paths, no schema- or catalog-qualified names;
- every function it calls is in ALLOWED_FUNCTIONS (none reads files or builds a huge value).

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

# The only functions a query may call (DECISIONS #27), by the name sqlglot gives them (DuckDB's
# spellings map onto these: string_agg -> group_concat, strftime -> time_to_str, list -> array_agg...).
# An allow-list for the same reason as the grants (#20): a function nobody reviewed stays out.
# Every one here returns a value at most linear in the size of its inputs. Left out on purpose:
# - size amplifiers: repeat, lpad/rpad, space, printf/format (a width), replace/regexp_replace (nested,
#   each multiplies), range/generate_series, list_resize, array_to_string, list_reduce. DuckDB's memory
#   limit doesn't count one scalar value, and its interrupt waits for the function to return, so
#   `repeat('x', 2e9)` would take 2 GB and outlive the timeout;
# - anything reading files, the environment or engine state: read_*, glob, getenv, current_setting,
#   duckdb_*, version (the serving sandbox blocks file access anyway, #21).
ALLOWED_FUNCTIONS = frozenset(
    {
        # syntax that sqlglot models as functions
        "and", "or", "xor", "case", "if", "cast", "try_cast", "coalesce", "nullif", "greatest", "least",
        "exists", "extract", "columns", "struct", "array", "explode",
        # aggregates (their state counts against the memory limit)
        "count", "count_if", "sum", "avg", "min", "max", "any_value", "arg_max", "arg_min", "first",
        "last", "median", "mode", "percentile_cont", "percentile_disc", "approx_distinct",
        "approx_quantile", "stddev", "stddev_pop", "stddev_samp", "variance", "variance_pop", "corr",
        "covar_pop", "covar_samp", "regr_slope", "regr_intercept", "regr_r2", "regr_count",
        "logical_and", "logical_or", "group_concat", "array_agg", "kurtosis", "skewness",
        # window
        "row_number", "rank", "dense_rank", "percent_rank", "cume_dist", "ntile", "lag", "lead",
        "first_value", "last_value", "nth_value",
        # math
        "abs", "ceil", "floor", "round", "trunc", "sign", "sqrt", "cbrt", "power", "exp", "ln", "log",
        "pi", "is_nan", "is_inf", "degrees", "radians",
        # dates
        "current_date", "current_timestamp", "now", "date", "date_trunc", "timestamp_trunc", "date_add",
        "date_sub", "datediff", "date_bin", "date_from_parts", "date_part", "datepart", "age", "year",
        "quarter", "month", "week", "week_of_year", "yearweek", "day", "day_of_month", "day_of_week",
        "day_of_year", "dayofweek_iso", "dayname", "monthname", "hour", "minute", "second", "last_day",
        "time_to_str", "str_to_time", "str_to_date", "time_to_unix", "unix_to_time",
        # strings
        "length", "strlen", "lower", "upper", "trim", "left", "right", "substring", "split_part", "split",
        "concat", "concat_ws", "starts_with", "ends_with", "contains", "str_position", "regexp_like",
        "regexp_extract", "translate", "reverse", "ascii", "chr",
        # lists
        "array_size", "cardinality", "array_contains", "array_distinct", "sort_array", "list_extract",
    }
)  # fmt: skip


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
        if isinstance(node, exp.Func) and _function_name(node) not in ALLOWED_FUNCTIONS:
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


def with_page(query: CheckedQuery, offset: int, size: int) -> CheckedQuery:
    """Rows [offset, offset + size) of the query's own result.

    The page is merged into the query's LIMIT / OFFSET, so its ORDER BY applies exactly: page
    offset=10, size=5 of `... ORDER BY x LIMIT 12` becomes `LIMIT 2 OFFSET 10`. A LIMIT or OFFSET that
    is not a plain number (an expression, `LIMIT 10%`) is kept as written, and the page is taken
    from outside it.
    """
    tree = query.tree.copy()
    limit, own_offset = _plain_int(tree.args.get("limit")), _plain_int(tree.args.get("offset"))
    if limit is _NOT_PLAIN or own_offset is _NOT_PLAIN:
        tree = exp.select("*").from_(tree.subquery("paged"))
        limit, own_offset = None, None
    count = size if limit is None else max(0, min(size, limit - offset))
    tree.set("limit", exp.Limit(expression=exp.Literal.number(count)))
    start = (own_offset or 0) + offset
    tree.set("offset", exp.Offset(expression=exp.Literal.number(start)) if start else None)
    return CheckedQuery(tree, query.tables)


_NOT_PLAIN = object()


def _plain_int(clause: exp.Expression | None):
    """The number in a LIMIT / OFFSET clause; None if absent; _NOT_PLAIN for anything else."""
    if clause is None:
        return None
    value = clause.expression
    if clause.args.get("limit_options") or not (isinstance(value, exp.Literal) and value.is_int):
        return _NOT_PLAIN
    return int(value.this)


def _function_name(node: exp.Func) -> str:
    return (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).lower()


def _kind(node: exp.Expression) -> str:
    return node.key.upper()


def _first_line(exc: Exception) -> str:
    return str(exc).splitlines()[0][:200]
