import { Link } from "react-router";

import { useCatalog } from "../api/hooks";
import type { Dataset } from "../api/types";
import { Empty, ErrorBox, Loading } from "../components/States";
import { formatNumber } from "../format";

const KIND_LABEL: Record<Dataset["kind"], string> = {
  metric: "Metrics",
  fact: "Daily data",
  dimension: "Plants and units",
  quality: "Data quality",
};
const KIND_ORDER: Dataset["kind"][] = ["metric", "fact", "dimension", "quality"];

export function DatasetsPage() {
  const catalog = useCatalog();

  if (catalog.isPending) return <Loading label="Loading datasets…" />;
  if (catalog.isError) return <ErrorBox error={catalog.error} onRetry={() => catalog.refetch()} />;

  const { datasets, build } = catalog.data;
  if (datasets.length === 0) return <Empty>Your role has no datasets.</Empty>;

  return (
    <>
      <div className="page-head">
        <h1>Datasets</h1>
        <p className="muted small">
          Data as of build <code>{build.build_id}</code>, built {new Date(build.built_at).toLocaleString()}
        </p>
      </div>
      {KIND_ORDER.filter((kind) => datasets.some((d) => d.kind === kind)).map((kind) => (
        <section key={kind} aria-labelledby={`kind-${kind}`}>
          <h2 id={`kind-${kind}`}>{KIND_LABEL[kind]}</h2>
          <ul className="dataset-grid">
            {datasets
              .filter((d) => d.kind === kind)
              .map((d) => (
                <li key={d.name} className="card dataset-card">
                  <Link to={`/datasets/${d.name}`} className="dataset-name">
                    {d.name}
                  </Link>
                  <p>{d.description}</p>
                  <p className="muted small">
                    {formatNumber(d.rows)} rows · {d.grain} · {d.columns.length} columns
                  </p>
                </li>
              ))}
          </ul>
        </section>
      ))}
    </>
  );
}
