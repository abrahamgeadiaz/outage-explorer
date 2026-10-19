import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceArea,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { api } from "../api/client";
import type { Cell, RowsPage } from "../api/types";
import { useSession } from "../auth/session";
import { DataTable } from "../components/DataTable";
import { Empty, ErrorBox, Loading } from "../components/States";
import { formatNumber } from "../format";

// US-08: share of total fleet capacity offline per day, from metric_fleet_offline_daily (a Viewer
// may read it, without the unit names). The line is offline_pct; the band is the range the true
// share falls in on days when some units are missing from EIA's data (DECISIONS.md #17).

const METRIC = "metric_fleet_offline_daily";
const RANGES = [
  { label: "90 days", days: 90 },
  { label: "1 year", days: 365 },
  { label: "5 years", days: 5 * 365 },
  { label: "All (2007–)", days: null },
] as const;
const COLUMNS = [
  "period",
  "offline_pct",
  "offline_pct_min",
  "offline_pct_max",
  "outage_mw",
  "capacity_mw",
  "units_missing",
  "is_complete",
] as const;

export interface DayPoint {
  period: string;
  offline: number | null;
  band: [number, number] | null; // [min, max]; null on complete days
  outageMw: number;
  capacityMw: number;
  unitsMissing: number;
  complete: boolean;
}

function toPoints(page: RowsPage): DayPoint[] {
  const at = Object.fromEntries(page.columns.map((c, i) => [c.name, i]));
  const num = (r: Cell[], k: string) => (r[at[k]] === null ? null : Number(r[at[k]]));
  return page.rows.map((r) => {
    const complete = r[at.is_complete] === true;
    const lo = num(r, "offline_pct_min");
    const hi = num(r, "offline_pct_max");
    return {
      period: String(r[at.period]),
      offline: num(r, "offline_pct"),
      band: !complete && lo !== null && hi !== null ? [lo, hi] : null,
      outageMw: num(r, "outage_mw") ?? 0,
      capacityMw: num(r, "capacity_mw") ?? 0,
      unitsMissing: num(r, "units_missing") ?? 0,
      complete,
    };
  });
}

/** Every day in [start, today], read page by page from the preview endpoint (1000 rows per page). */
export async function fetchDays(start: string | undefined, signal?: AbortSignal): Promise<RowsPage> {
  const rows: Cell[][] = [];
  for (;;) {
    const page = await api<RowsPage>(`/api/datasets/${METRIC}/rows`, {
      params: { start, limit: 1000, offset: rows.length },
      signal,
    });
    rows.push(...page.rows);
    if (!page.page.has_more || page.rows.length === 0) return { ...page, rows };
  }
}

function daysAgo(days: number): string {
  const d = new Date();
  d.setUTCDate(d.getUTCDate() - days);
  return d.toISOString().slice(0, 10);
}

/** Consecutive incomplete days as [first, last] periods, shaded behind the line. The min-max band
 * alone is too thin to see over long ranges (a missing unit moves the share by < 1 point). */
export function incompleteRuns(points: DayPoint[]): [string, string][] {
  const runs: [string, string][] = [];
  points.forEach((p, i) => {
    if (p.complete) return;
    if (i > 0 && !points[i - 1].complete) runs[runs.length - 1][1] = p.period;
    else runs.push([p.period, p.period]);
  });
  return runs;
}

const mw = (v: number) => formatNumber(Math.round(v));
const pct = (v: number | null | undefined) => (v === null || v === undefined ? "–" : `${v.toFixed(2)}%`);

export function FleetPage() {
  const { user } = useSession();
  const [range, setRange] = useState<(typeof RANGES)[number]>(RANGES[1]);
  const [showTable, setShowTable] = useState(false);
  const start = range.days === null ? undefined : daysAgo(range.days);

  const data = useQuery({
    queryKey: [user?.username, "fleet", start],
    queryFn: ({ signal }) => fetchDays(start, signal),
    staleTime: 60_000,
  });

  return (
    <>
      <div className="page-head">
        <h1>Fleet capacity offline</h1>
        <p className="muted">
          Share of U.S. nuclear capacity offline each day (US-08): <code>100 × outage MW / capacity MW</code> over the
          units EIA reported that day.
        </p>
      </div>

      <div className="toolbar" role="group" aria-label="Time range">
        {RANGES.map((r) => (
          <button
            key={r.label}
            type="button"
            className={r === range ? "primary" : undefined}
            aria-pressed={r === range}
            onClick={() => setRange(r)}
          >
            {r.label}
          </button>
        ))}
      </div>

      {data.isPending ? (
        <Loading label="Loading daily values…" />
      ) : data.isError ? (
        <ErrorBox error={data.error} onRetry={() => data.refetch()} />
      ) : data.data.rows.length === 0 ? (
        <Empty>No days in this range yet.</Empty>
      ) : (
        <FleetView page={data.data} showTable={showTable} onToggleTable={() => setShowTable((s) => !s)} />
      )}
    </>
  );
}

function FleetView({ page, showTable, onToggleTable }: { page: RowsPage; showTable: boolean; onToggleTable(): void }) {
  const points = toPoints(page);
  const latest = points[points.length - 1];
  const values = points.map((p) => p.offline).filter((v): v is number => v !== null);
  const average = values.reduce((a, b) => a + b, 0) / Math.max(values.length, 1);
  const peak = points.reduce((best, p) => ((p.offline ?? -1) > (best.offline ?? -1) ? p : best), points[0]);
  const incomplete = points.filter((p) => !p.complete).length;

  return (
    <>
      <ul className="tiles" aria-label="Summary">
        <li className="card tile">
          <span className="tile-label">Latest ({latest.period})</span>
          <span className="tile-value">{pct(latest.offline)}</span>
          <span className="muted small">
            {mw(latest.outageMw)} of {mw(latest.capacityMw)} MW offline
          </span>
        </li>
        <li className="card tile">
          <span className="tile-label">Average over range</span>
          <span className="tile-value">{pct(average)}</span>
          <span className="muted small">{formatNumber(points.length)} days</span>
        </li>
        <li className="card tile">
          <span className="tile-label">Peak</span>
          <span className="tile-value">{pct(peak.offline)}</span>
          <span className="muted small">on {peak.period}</span>
        </li>
        <li className="card tile">
          <span className="tile-label">Incomplete days</span>
          <span className="tile-value">{formatNumber(incomplete)}</span>
          <span className="muted small">some units missing from EIA's data</span>
        </li>
      </ul>

      <figure className="card chart">
        <figcaption>
          <strong>Capacity offline, % of fleet</strong>
          <span className="chart-key muted small">
            <span className="key-item">
              <span className="key-line" aria-hidden="true" /> offline share
            </span>
            <span className="key-item">
              <span className="key-band" aria-hidden="true" /> possible range when units are missing (all online → all
              offline)
            </span>
            <span className="key-item">
              <span className="key-gap" aria-hidden="true" /> days with units missing from EIA's data
            </span>
          </span>
        </figcaption>
        <div
          className="chart-area"
          role="img"
          aria-label={`Line chart of the daily share of fleet capacity offline from ${points[0].period} to ${latest.period}. Average ${pct(average)}, peak ${pct(peak.offline)} on ${peak.period}.`}
        >
          <ResponsiveContainer width="100%" height={320}>
            <ComposedChart data={points} margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
              <CartesianGrid vertical={false} stroke="var(--grid)" strokeWidth={1} />
              <XAxis
                dataKey="period"
                tick={{ fill: "var(--muted)", fontSize: 12 }}
                tickLine={false}
                axisLine={{ stroke: "var(--grid)" }}
                minTickGap={48}
              />
              <YAxis
                tick={{ fill: "var(--muted)", fontSize: 12 }}
                tickLine={false}
                axisLine={false}
                width={44}
                tickFormatter={(v: number) => `${v}%`}
              />
              {incompleteRuns(points).map(([x1, x2]) => (
                <ReferenceArea key={x1} x1={x1} x2={x2} fill="var(--muted)" fillOpacity={0.14} ifOverflow="hidden" />
              ))}
              <Tooltip content={<DayTooltip />} cursor={{ stroke: "var(--muted)", strokeWidth: 1 }} />
              <Area
                dataKey="band"
                stroke="none"
                fill="var(--chart-line)"
                fillOpacity={0.18}
                activeDot={false}
                isAnimationActive={false}
                connectNulls={false}
              />
              <Line
                dataKey="offline"
                stroke="var(--chart-line)"
                strokeWidth={2}
                strokeLinejoin="round"
                strokeLinecap="round"
                dot={false}
                activeDot={{ r: 4, strokeWidth: 2, stroke: "var(--surface)" }}
                isAnimationActive={false}
              />
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      </figure>

      <p className="toolbar">
        <button type="button" onClick={onToggleTable} aria-expanded={showTable}>
          {showTable ? "Hide" : "Show"} the numbers as a table
        </button>
        <Link to={`/datasets/${METRIC}`}>Open the full dataset</Link>
      </p>
      {showTable && (
        <DataTable
          caption="Daily share of fleet capacity offline"
          columns={COLUMNS.map((name) => page.columns.find((c) => c.name === name)!).filter(Boolean)}
          rows={page.rows.map((r) => COLUMNS.map((name) => r[page.columns.findIndex((c) => c.name === name)]))}
        />
      )}
    </>
  );
}

function DayTooltip({ active, payload }: { active?: boolean; payload?: { payload: DayPoint }[] }) {
  const p = payload?.[0]?.payload;
  if (!active || !p) return null;
  return (
    <div className="tooltip">
      <strong>{p.period}</strong>
      <div>{pct(p.offline)} offline</div>
      <div className="muted small">
        {mw(p.outageMw)} / {mw(p.capacityMw)} MW
      </div>
      {p.band ? (
        <div className="small">
          {p.unitsMissing} unit{p.unitsMissing === 1 ? "" : "s"} missing: true share {pct(p.band[0])}–{pct(p.band[1])}
        </div>
      ) : (
        <div className="muted small">complete day</div>
      )}
    </div>
  );
}
