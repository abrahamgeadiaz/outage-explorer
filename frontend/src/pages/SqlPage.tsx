import { useMutation } from "@tanstack/react-query";
import { useRef, useState, type KeyboardEvent } from "react";

import { api, ApiError } from "../api/client";
import { useCatalog } from "../api/hooks";
import type { QueryResult } from "../api/types";
import { useSession } from "../auth/session";
import { DataTable } from "../components/DataTable";
import { ErrorBox, Loading } from "../components/States";
import { downloadCsv, toCsv } from "../csv";
import { formatNumber } from "../format";
import { loadHistory, remember, type HistoryEntry } from "../history";

const EXAMPLES: { label: string; sql: string }[] = [
  {
    label: "US-08, last 14 days",
    sql: "SELECT period, round(offline_pct, 2) AS offline_pct, units_missing, is_complete\nFROM metric_fleet_offline_daily\nORDER BY period DESC\nLIMIT 14",
  },
  {
    label: "Yearly average offline",
    sql: "SELECT year(period) AS year, round(avg(offline_pct), 2) AS avg_offline_pct,\n       count(*) FILTER (WHERE NOT is_complete) AS incomplete_days\nFROM metric_fleet_offline_daily\nGROUP BY ALL\nORDER BY year",
  },
  {
    label: "Units with reporting gaps",
    sql: "SELECT f.facility_name, d.generator_id, sum(d.days) AS days_missing\nFROM dq_issue d JOIN dim_facility f USING (facility_id)\nWHERE d.check_name = 'unit_reporting_gap'\nGROUP BY ALL\nORDER BY days_missing DESC",
  },
];

const PAGE_SIZES = [100, 1000, 5000];

interface Run {
  sql: string;
  offset: number;
  maxRows: number;
}

export function SqlPage() {
  const { user } = useSession();
  const catalog = useCatalog();
  const editor = useRef<HTMLTextAreaElement>(null);
  const [sql, setSql] = useState(EXAMPLES[0].sql);
  const [maxRows, setMaxRows] = useState(1000);
  const [history, setHistory] = useState<HistoryEntry[]>(() => loadHistory(user!.username));
  const [run, setRun] = useState<Run | null>(null);
  const [previous, setPrevious] = useState<number[]>([]); // offsets of earlier pages, for "Prev"

  const query = useMutation({
    mutationFn: (r: Run) =>
      api<QueryResult>("/api/query", { method: "POST", body: { sql: r.sql, max_rows: r.maxRows, offset: r.offset } }),
    onSettled: (_data, error, r) => {
      if (r.offset === 0) setHistory(remember(user!.username, r.sql, !error));
    },
  });

  function execute(next: Run) {
    setRun(next);
    query.mutate(next);
  }

  function runFromEditor() {
    if (!sql.trim() || query.isPending) return;
    setPrevious([]);
    execute({ sql, offset: 0, maxRows });
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      runFromEditor();
    }
  }

  function insert(text: string) {
    const el = editor.current;
    if (!el) return setSql((s) => s + text);
    const { selectionStart: a, selectionEnd: b } = el;
    setSql((s) => s.slice(0, a) + text + s.slice(b));
    requestAnimationFrame(() => {
      el.focus();
      el.setSelectionRange(a + text.length, a + text.length);
    });
  }

  const result = query.data;
  return (
    <div className="sql-layout">
      <section aria-labelledby="sql-title">
        <h1 id="sql-title">SQL</h1>
        <p className="muted small">
          Read-only: one <code>SELECT</code> (joins, CTEs, subqueries, aggregates) over the datasets you may see.
          Ctrl+Enter runs it.
        </p>
        <label className="sr-only" htmlFor="sql-editor">
          SQL query
        </label>
        <textarea
          id="sql-editor"
          ref={editor}
          className="sql-editor mono"
          value={sql}
          onChange={(e) => setSql(e.target.value)}
          onKeyDown={onKeyDown}
          spellCheck={false}
          rows={9}
        />
        <div className="toolbar">
          <button type="button" className="primary" onClick={runFromEditor} disabled={query.isPending || !sql.trim()}>
            {query.isPending ? "Running…" : "Run"}
          </button>
          <label>
            Rows per page{" "}
            <select value={maxRows} onChange={(e) => setMaxRows(Number(e.target.value))}>
              {PAGE_SIZES.map((n) => (
                <option key={n} value={n}>
                  {formatNumber(n)}
                </option>
              ))}
            </select>
          </label>
          <span className="examples">
            Examples:{" "}
            {EXAMPLES.map((ex) => (
              <button key={ex.label} type="button" className="link-button" onClick={() => setSql(ex.sql)}>
                {ex.label}
              </button>
            ))}
          </span>
        </div>

        <div aria-live="polite">
          {query.isPending && <Loading label="Running query…" />}
          {query.isError && <QueryError error={query.error} />}
          {result && !query.isPending && run && (
            <ResultView
              result={result}
              onPage={(offset) => {
                setPrevious((p) => (offset > run.offset ? [...p, run.offset] : p.slice(0, -1)));
                execute({ ...run, offset });
              }}
              previousOffset={previous.at(-1)}
            />
          )}
        </div>
      </section>

      <aside className="sql-side">
        <h2>Datasets</h2>
        {catalog.isPending ? (
          <Loading />
        ) : catalog.isError ? (
          <ErrorBox error={catalog.error} />
        ) : (
          <ul className="schema">
            {catalog.data.datasets.map((d) => (
              <li key={d.name}>
                <details>
                  <summary>
                    <button type="button" className="link-button mono" onClick={() => insert(d.name)} title="Insert at cursor">
                      {d.name}
                    </button>
                  </summary>
                  <ul>
                    {d.columns.map((c) => (
                      <li key={c.name}>
                        <button type="button" className="link-button mono" onClick={() => insert(c.name)} title={c.description}>
                          {c.name}
                        </button>{" "}
                        <span className="muted small">{c.type}</span>
                      </li>
                    ))}
                  </ul>
                </details>
              </li>
            ))}
          </ul>
        )}
        <h2>History</h2>
        {history.length === 0 ? (
          <p className="muted small">Queries you run appear here (this browser only).</p>
        ) : (
          <ol className="history">
            {history.map((h) => (
              <li key={h.at + h.sql}>
                <button type="button" className="link-button mono history-sql" onClick={() => setSql(h.sql)} title={h.sql}>
                  {h.sql.replace(/\s+/g, " ").slice(0, 80)}
                </button>
                <span className={`small ${h.ok ? "muted" : "error-text"}`}>
                  {h.ok ? "" : "failed · "}
                  {new Date(h.at).toLocaleString()}
                </span>
              </li>
            ))}
          </ol>
        )}
      </aside>
    </div>
  );
}

function QueryError({ error }: { error: Error }) {
  if (error instanceof ApiError && error.code === "query_timeout") {
    return (
      <div className="state state-error" role="alert">
        <strong>The query took too long.</strong> {error.message}. Try a narrower date range or an aggregate.
      </div>
    );
  }
  return <ErrorBox error={error} />;
}

function ResultView({
  result,
  onPage,
  previousOffset,
}: {
  result: QueryResult;
  onPage(offset: number): void;
  previousOffset?: number;
}) {
  const first = result.offset + 1;
  const last = result.offset + result.row_count;
  return (
    <>
      <div className="result-meta">
        <span>
          {result.row_count === 0
            ? "No rows."
            : `Rows ${formatNumber(first)}–${formatNumber(last)}${result.truncated ? " (more available)" : ""}`}
        </span>
        <span className="muted small">
          {result.tables.length > 0 && <>read {result.tables.join(", ")} · </>}
          {result.elapsed_ms} ms{result.cached && <span className="badge">cached</span>}
        </span>
        <span className="pager-buttons">
          <button type="button" disabled={previousOffset === undefined} onClick={() => onPage(previousOffset!)}>
            ‹ Prev
          </button>
          <button type="button" disabled={result.next_offset === null} onClick={() => onPage(result.next_offset!)}>
            Next ›
          </button>
          <button
            type="button"
            disabled={result.row_count === 0}
            onClick={() => downloadCsv(`query-${result.offset}.csv`, toCsv(result.columns, result.rows))}
          >
            Export page as CSV
          </button>
        </span>
      </div>
      {(result.truncated || result.offset > 0) && !result.ordered && (
        <p className="state state-notice small">
          This query has no ORDER BY, so pages may overlap or skip rows. Add an ORDER BY to page reliably.
        </p>
      )}
      {result.columns.length > 0 && <DataTable columns={result.columns} rows={result.rows} caption="Query result" />}
    </>
  );
}
