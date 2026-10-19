import { useState, type FormEvent } from "react";
import { Link, useParams, useSearchParams } from "react-router";

import { useDataset, useFacilities, useRows, type RowsParams } from "../api/hooks";
import type { Dataset } from "../api/types";
import { DataTable } from "../components/DataTable";
import { PAGE_SIZES, Pager } from "../components/Pager";
import { Empty, ErrorBox, Loading } from "../components/States";
import { formatNumber } from "../format";

// Filters and the page live in the URL (?start=&end=&facility=&limit=&offset=), so a view can be
// bookmarked or shared, and Back goes to the previous page of results.
function readParams(search: URLSearchParams): RowsParams {
  const limit = Number(search.get("limit"));
  return {
    start: search.get("start") ?? undefined,
    end: search.get("end") ?? undefined,
    facility_id: search.getAll("facility"),
    limit: PAGE_SIZES.includes(limit) ? limit : 100,
    offset: Math.max(0, Number(search.get("offset")) || 0),
  };
}

export function DatasetPage() {
  const { name = "" } = useParams();
  const [search, setSearch] = useSearchParams();
  const params = readParams(search);
  const dataset = useDataset(name);
  const rows = useRows(name, params, dataset.isSuccess);

  if (dataset.isPending) return <Loading label="Loading dataset…" />;
  if (dataset.isError) return <ErrorBox error={dataset.error} />;
  const ds = dataset.data;

  function update(next: Partial<RowsParams>) {
    const merged = { ...params, ...next };
    const out = new URLSearchParams();
    if (merged.start) out.set("start", merged.start);
    if (merged.end) out.set("end", merged.end);
    for (const f of merged.facility_id ?? []) out.append("facility", f);
    if (merged.limit !== 100) out.set("limit", String(merged.limit));
    if (merged.offset) out.set("offset", String(merged.offset));
    setSearch(out);
  }

  return (
    <>
      <div className="page-head">
        <p className="small">
          <Link to="/datasets">Datasets</Link> /
        </p>
        <h1 className="mono">{ds.name}</h1>
        <p>{ds.description}</p>
        <p className="muted small">
          {formatNumber(ds.rows)} rows · {ds.grain} · key: {ds.primary_key.join(", ")}
        </p>
      </div>

      <ColumnList dataset={ds} />
      <Filters
        key={search.toString()}
        dataset={ds}
        params={params}
        onApply={(f) => update({ ...f, offset: 0 })}
      />

      {rows.isPending ? (
        <Loading label="Loading rows…" />
      ) : rows.isError ? (
        <ErrorBox error={rows.error} onRetry={() => rows.refetch()} />
      ) : rows.data.page.total === 0 ? (
        <Empty>No rows match these filters.</Empty>
      ) : (
        <>
          <Pager
            offset={rows.data.page.offset}
            limit={rows.data.page.limit}
            total={rows.data.page.total}
            shown={rows.data.rows.length}
            onChange={(offset, limit) => update({ offset, limit })}
          />
          <DataTable
            columns={rows.data.columns}
            rows={rows.data.rows}
            caption={`Rows of ${ds.name}`}
            busy={rows.isPlaceholderData}
          />
        </>
      )}
    </>
  );
}

function ColumnList({ dataset }: { dataset: Dataset }) {
  return (
    <details className="card columns">
      <summary>
        Columns ({dataset.columns.length}) and their definitions
      </summary>
      <table>
        <thead>
          <tr>
            <th scope="col">Column</th>
            <th scope="col">Type</th>
            <th scope="col">Definition</th>
          </tr>
        </thead>
        <tbody>
          {dataset.columns.map((c) => (
            <tr key={c.name}>
              <td className="mono">
                {c.name}
                {dataset.primary_key.includes(c.name) && <span className="badge">key</span>}
              </td>
              <td className="mono muted">{c.type}</td>
              <td>{c.description}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </details>
  );
}

interface FilterValues {
  start?: string;
  end?: string;
  facility_id: string[];
}

function Filters({
  dataset,
  params,
  onApply,
}: {
  dataset: Dataset;
  params: RowsParams;
  onApply(values: FilterValues): void;
}) {
  const hasDate = dataset.filters.date.length > 0;
  const facilities = useFacilities(dataset.filters.facility);
  const [start, setStart] = useState(params.start ?? "");
  const [end, setEnd] = useState(params.end ?? "");
  const [facility, setFacility] = useState((params.facility_id ?? []).join(", "));

  if (!hasDate && !dataset.filters.facility) return null;

  function submit(event: FormEvent) {
    event.preventDefault();
    const ids = facility
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    onApply({ start: start || undefined, end: end || undefined, facility_id: ids });
  }

  return (
    <form className="filters" onSubmit={submit} aria-label="Filters">
      {hasDate && (
        <>
          <label>
            From
            <input type="date" value={start} onChange={(e) => setStart(e.target.value)} />
          </label>
          <label>
            To
            <input type="date" value={end} onChange={(e) => setEnd(e.target.value)} />
          </label>
        </>
      )}
      {dataset.filters.facility && (
        <label className="grow">
          Facility ids (comma-separated)
          <input
            list="facility-options"
            value={facility}
            placeholder="e.g. 6462, 46"
            onChange={(e) => setFacility(e.target.value)}
          />
          <datalist id="facility-options">
            {facilities.data?.map((f) => (
              <option key={f.id} value={f.id}>
                {f.name}
              </option>
            ))}
          </datalist>
        </label>
      )}
      <button type="submit" className="primary">
        Apply
      </button>
      <button
        type="button"
        onClick={() => {
          setStart("");
          setEnd("");
          setFacility("");
          onApply({ facility_id: [] });
        }}
      >
        Clear
      </button>
      {hasDate && dataset.filters.date.length === 2 && (
        <span className="muted small">
          Keeps rows whose {dataset.filters.date.join("–")} span overlaps the dates.
        </span>
      )}
    </form>
  );
}
