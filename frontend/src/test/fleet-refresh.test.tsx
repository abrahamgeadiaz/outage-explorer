import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { polling } from "../pages/RefreshPage";
import { CATALOG, mockApi, renderAt, signedInAs } from "./helpers";

const METRIC_COLUMNS = [
  "period",
  "capacity_mw",
  "outage_mw",
  "offline_pct",
  "percent_outage_eia",
  "units_reported",
  "units_missing",
  "missing_capacity_mw",
  "units_zero_capacity",
  "offline_pct_min",
  "offline_pct_max",
  "is_complete",
].map((name) => ({ name, type: name === "period" ? "DATE" : name === "is_complete" ? "BOOLEAN" : "DOUBLE" }));

function day(i: number, offline: number, missing = 0) {
  const period = new Date(Date.UTC(2015, 0, 1 + i)).toISOString().slice(0, 10);
  const lo = missing ? offline - 0.5 : offline;
  const hi = missing ? offline + 0.5 : offline;
  return [period, 100000, offline * 1000, offline, offline, 98, missing, missing ? 967 : 0, 0, lo, hi, missing === 0];
}

function metricApi(rows: unknown[][]) {
  return ({ url }: { url: URL }) => {
    const offset = Number(url.searchParams.get("offset"));
    const limit = Number(url.searchParams.get("limit"));
    const page = rows.slice(offset, offset + limit);
    return [
      200,
      {
        dataset: "metric_fleet_offline_daily",
        build_id: "b1",
        columns: METRIC_COLUMNS,
        rows: page,
        page: { limit, offset, total: rows.length, has_more: offset + page.length < rows.length },
      },
    ] as [number, unknown];
  };
}

describe("fleet offline (US-08)", () => {
  it("reads every page of the range and summarizes it", async () => {
    const rows = Array.from({ length: 1500 }, (_, i) => day(i, i === 700 ? 30 : 10, i === 1499 ? 1 : 0));
    const { calls } = mockApi({ ...signedInAs("viewer"), "GET /api/datasets/metric_fleet_offline_daily/rows": metricApi(rows) });
    renderAt("/fleet");
    // Lazy page + two 1,000-row pages can pass the default 1 s wait on a busy machine.
    const tiles = await screen.findByRole("list", { name: "Summary" }, { timeout: 3000 });
    expect(within(tiles).getByText("Latest (2019-02-08)")).toBeInTheDocument();
    expect(within(tiles).getByText("30.00%")).toBeInTheDocument(); // the peak
    expect(within(tiles).getByText("on 2016-12-01")).toBeInTheDocument();
    expect(within(tiles).getByText("1,500 days")).toBeInTheDocument();
    expect(within(tiles).getByText("1")).toBeInTheDocument(); // one incomplete day
    const offsets = calls.filter((c) => c.url.pathname.endsWith("/rows")).map((c) => c.url.searchParams.get("offset"));
    expect(offsets).toEqual(["0", "1000"]);
    expect(screen.getByRole("img", { name: /Line chart of the daily share/ })).toBeInTheDocument();
  });

  it("asks for the chosen range, and 'All' sends no start date", async () => {
    const { calls } = mockApi({
      ...signedInAs("viewer"),
      "GET /api/datasets/metric_fleet_offline_daily/rows": metricApi([day(0, 10)]),
    });
    renderAt("/fleet");
    await screen.findByRole("list", { name: "Summary" });
    await userEvent.click(screen.getByRole("button", { name: "All (2007–)" }));
    await waitFor(() => expect(calls.at(-1)!.url.searchParams.has("start")).toBe(false));
    expect(calls.find((c) => c.url.pathname.endsWith("/rows"))!.url.searchParams.get("start")).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    expect(screen.getByRole("button", { name: "All (2007–)" })).toHaveAttribute("aria-pressed", "true");
  });

  it("offers the numbers as a table", async () => {
    mockApi({ ...signedInAs("viewer"), "GET /api/datasets/metric_fleet_offline_daily/rows": metricApi([day(0, 12.5, 1)]) });
    renderAt("/fleet");
    await userEvent.click(await screen.findByRole("button", { name: "Show the numbers as a table" }));
    const table = screen.getByRole("table", { name: "Daily share of fleet capacity offline" });
    expect(within(table).getByText("offline_pct_max")).toBeInTheDocument();
    expect(within(table).getByText("13")).toBeInTheDocument();
  });
});

function job(status: string, step: string, extra: Record<string, unknown> = {}) {
  return {
    job_id: "20261019T050000Z-aaaaaa",
    status,
    step,
    full: false,
    requested_by: "admin",
    started_at: "2026-10-19T05:00:00+00:00",
    finished_at: status === "running" ? null : "2026-10-19T05:00:34+00:00",
    extraction_run_id: "run-1",
    datasets: {},
    build_id: null,
    dq_issues: {},
    error: null,
    ...extra,
  };
}

describe("refresh (US-06)", () => {
  beforeEach(() => {
    polling.ms = 20;
  });
  afterEach(() => {
    polling.ms = 2000;
  });

  it("is only for roles that can refresh", async () => {
    mockApi({ ...signedInAs("analyst") });
    renderAt("/refresh");
    expect(await screen.findByRole("alert")).toHaveTextContent("Your role cannot use this page.");
  });

  it("starts a refresh and follows it until it says whether it worked", async () => {
    let polls = 0;
    const done = job("succeeded", "done", {
      build_id: "20261019T050034Z-bbbbbb",
      datasets: { generator: { status: "success", rows_fetched: 2945, inserted: 3, updated: 1, watermark: "2026-10-17" } },
      dq_issues: { warning: 99, error: 1 },
    });
    const { calls } = mockApi({
      ...signedInAs("admin"),
      "GET /api/datasets": [200, CATALOG],
      "GET /api/refresh": [200, []],
      "POST /api/refresh": [202, job("running", "extract")],
      "GET /api/refresh/20261019T050000Z-aaaaaa": () => {
        polls += 1;
        return [200, polls < 2 ? job("running", "extract") : polls < 3 ? job("running", "model") : done];
      },
    });
    const { client } = renderAt("/datasets");
    await screen.findByRole("heading", { name: "Datasets" });
    await userEvent.click(screen.getByRole("link", { name: "Refresh" }));

    await userEvent.click(await screen.findByRole("button", { name: "Start refresh" }));
    expect(calls.find((c) => c.method === "POST")!.body).toEqual({ full: false });
    expect(await screen.findByRole("button", { name: "Refresh running…" })).toBeDisabled();

    expect(await screen.findByText("Succeeded")).toBeInTheDocument();
    expect(screen.getByText("20261019T050034Z-bbbbbb")).toBeInTheDocument();
    expect(screen.getByText("2,945")).toBeInTheDocument();
    expect(screen.getByText(/99 warning · 1 error/)).toBeInTheDocument();
    expect(polls).toBe(3);
    // Data from the old build is marked stale, so other screens fetch the new build.
    await waitFor(() => expect(client.getQueryState(["admin", "catalog"])?.isInvalidated).toBe(true));
  });

  it("asks for a full reload when ticked", async () => {
    const { calls } = mockApi({
      ...signedInAs("admin"),
      "GET /api/refresh": [200, []],
      "POST /api/refresh": [202, job("failed", "done")],
      "GET /api/refresh/20261019T050000Z-aaaaaa": [200, job("failed", "done")],
    });
    renderAt("/refresh");
    await userEvent.click(await screen.findByLabelText(/Full reload from 2007/));
    await userEvent.click(screen.getByRole("button", { name: "Start refresh" }));
    await waitFor(() => expect(calls.find((c) => c.method === "POST")!.body).toEqual({ full: true }));
  });

  it("shows why a refresh failed", async () => {
    const failed = job("failed", "done", { error: "extraction failed, nothing was rebuilt: EIA rejected the API key" });
    mockApi({
      ...signedInAs("admin"),
      "GET /api/refresh": [200, [failed]],
      "GET /api/refresh/20261019T050000Z-aaaaaa": [200, failed],
    });
    renderAt("/refresh");
    expect(await screen.findByText("Failed")).toBeInTheDocument();
    expect(screen.getByText(/EIA rejected the API key/)).toBeInTheDocument();
  });

  it("offers to follow the refresh that is already running", async () => {
    mockApi({
      ...signedInAs("admin"),
      "GET /api/refresh": [200, []],
      "POST /api/refresh": [409, { error: { code: "refresh_in_progress", message: "refresh 20261019T050000Z-aaaaaa is still running" } }],
      "GET /api/refresh/20261019T050000Z-aaaaaa": [200, job("running", "model")],
    });
    renderAt("/refresh");
    await userEvent.click(await screen.findByRole("button", { name: "Start refresh" }));
    await userEvent.click(await screen.findByRole("button", { name: "Follow it" }));
    expect(await screen.findByText("Running")).toBeInTheDocument();
    expect(screen.getByText("Rebuild the model")).toHaveAttribute("aria-current", "step");
  });
});

describe("incomplete runs", () => {
  it("groups consecutive incomplete days", async () => {
    const { incompleteRuns } = await import("../pages/FleetPage");
    const p = (period: string, complete: boolean) =>
      ({ period, complete, offline: 1, band: null, outageMw: 0, capacityMw: 0, unitsMissing: complete ? 0 : 1 });
    expect(
      incompleteRuns([p("d1", true), p("d2", false), p("d3", false), p("d4", true), p("d5", false)]),
    ).toEqual([["d2", "d3"], ["d5", "d5"]]);
  });
});
