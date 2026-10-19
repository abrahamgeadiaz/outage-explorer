import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { mockApi, renderAt, signedInAs } from "./helpers";

const GENERATOR = {
  name: "fact_generator_daily",
  kind: "fact",
  description: "Unit values per day.",
  grain: "one row per day, facility and generator",
  primary_key: ["period", "facility_id", "generator_id"],
  columns: [
    { name: "period", type: "DATE", description: "Day." },
    { name: "facility_id", type: "VARCHAR", description: "EIA plant id." },
    { name: "generator_id", type: "VARCHAR", description: "Unit id." },
    { name: "capacity_mw", type: "DECIMAL(12,3)", description: "Capacity." },
    { name: "percent_outage", type: "DOUBLE", description: "Recomputed." },
  ],
  rows: 713327,
  filters: { date: ["period"], facility: true },
};

function rowsPage(offset: number, limit: number, total = 5) {
  const rows = [];
  for (let i = offset; i < Math.min(offset + limit, total); i++) {
    rows.push([`2015-10-0${i + 1}`, "6462", "1", 967.5 + i, i === 0 ? null : 1.25]);
  }
  return {
    dataset: GENERATOR.name,
    build_id: "b1",
    columns: GENERATOR.columns.map(({ name, type }) => ({ name, type })),
    rows,
    page: { limit, offset, total, has_more: offset + rows.length < total },
  };
}

function analystApi(extra = {}) {
  return mockApi({
    ...signedInAs("analyst"),
    "GET /api/datasets/fact_generator_daily": [200, GENERATOR],
    "GET /api/datasets/fact_generator_daily/rows": ({ url }) => [
      200,
      rowsPage(Number(url.searchParams.get("offset")), Number(url.searchParams.get("limit"))),
    ],
    "GET /api/datasets/dim_facility/rows": [
      200,
      {
        dataset: "dim_facility",
        build_id: "b1",
        columns: [
          { name: "facility_id", type: "VARCHAR" },
          { name: "facility_name", type: "VARCHAR" },
        ],
        rows: [["6462", "River Bend Station"]],
        page: { limit: 1000, offset: 0, total: 1, has_more: false },
      },
    ],
    ...extra,
  });
}

const rowCalls = (calls: ReturnType<typeof mockApi>["calls"]) =>
  calls.filter((c) => c.url.pathname.endsWith("/rows") && c.url.pathname.includes("generator"));

describe("dataset page", () => {
  it("shows the columns with types and definitions, and one page of rows", async () => {
    analystApi();
    renderAt("/datasets/fact_generator_daily");
    expect(await screen.findByRole("heading", { name: "fact_generator_daily" })).toBeInTheDocument();
    expect(screen.getByText("Columns (5) and their definitions")).toBeInTheDocument();
    expect(screen.getByText("EIA plant id.")).toBeInTheDocument();

    const table = await screen.findByRole("table", { name: "Rows of fact_generator_daily" });
    expect(within(table).getAllByRole("row")).toHaveLength(1 + 5);
    expect(within(table).getByText("967.5")).toHaveClass("num");
    expect(within(table).getByText("null")).toBeInTheDocument();
    expect(screen.getByText(/Rows 1–5 of 5 · page 1 of 1/)).toBeInTheDocument();
  });

  it("asks the backend for each page", async () => {
    const { calls } = analystApi({
      "GET /api/datasets/fact_generator_daily/rows": ({ url }: { url: URL }) => [
        200,
        rowsPage(Number(url.searchParams.get("offset")), Number(url.searchParams.get("limit")), 250),
      ],
    });
    const { router } = renderAt("/datasets/fact_generator_daily");
    expect(await screen.findByText(/Rows 1–100 of 250/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Next page" }));
    expect(await screen.findByText(/Rows 101–200 of 250 · page 2 of 3/)).toBeInTheDocument();
    expect(router.state.location.search).toBe("?offset=100");
    await userEvent.click(screen.getByRole("button", { name: "Last page" }));
    expect(await screen.findByText(/Rows 201–250 of 250/)).toBeInTheDocument();
    expect(rowCalls(calls).map((c) => c.url.searchParams.get("offset"))).toEqual(["0", "100", "200"]);
  });

  it("sends date and facility filters, and starts again from the first page", async () => {
    const { calls } = analystApi();
    const { router } = renderAt("/datasets/fact_generator_daily?offset=100");
    await screen.findByRole("form", { name: "Filters" });
    await userEvent.type(screen.getByLabelText("From"), "2015-02-13");
    await userEvent.type(screen.getByLabelText("To"), "2015-02-16");
    await userEvent.type(screen.getByLabelText(/Facility ids/), "6462, 46");
    await userEvent.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() => expect(router.state.location.search).toBe("?start=2015-02-13&end=2015-02-16&facility=6462&facility=46"));
    const last = rowCalls(calls).at(-1)!.url.searchParams;
    expect(last.get("start")).toBe("2015-02-13");
    expect(last.get("end")).toBe("2015-02-16");
    expect(last.getAll("facility_id")).toEqual(["6462", "46"]);
    expect(last.get("offset")).toBe("0");
  });

  it("offers plant names for the facility filter", async () => {
    analystApi();
    const { container } = renderAt("/datasets/fact_generator_daily");
    await screen.findByRole("form", { name: "Filters" });
    await waitFor(() => expect(container.querySelector("#facility-options option")).toHaveTextContent("River Bend Station"));
  });

  it("says when nothing matches", async () => {
    analystApi({
      "GET /api/datasets/fact_generator_daily/rows": [200, rowsPage(0, 100, 0)],
    });
    renderAt("/datasets/fact_generator_daily");
    expect(await screen.findByText("No rows match these filters.")).toBeInTheDocument();
  });

  it("shows the API's message for a bad filter", async () => {
    analystApi({
      "GET /api/datasets/fact_generator_daily/rows": [
        400,
        { error: { code: "invalid_filter", message: "start must not be after end" } },
      ],
    });
    renderAt("/datasets/fact_generator_daily?start=2015-10-03&end=2015-10-01");
    expect(await screen.findByRole("alert")).toHaveTextContent("start must not be after end");
  });

  it("shows 'not allowed' when a Viewer opens a detail dataset by URL", async () => {
    const { calls } = mockApi({
      ...signedInAs("viewer"),
      "GET /api/datasets/fact_generator_daily": [
        403,
        { error: { code: "forbidden", message: "role 'viewer' has no access to dataset 'fact_generator_daily'" } },
      ],
    });
    renderAt("/datasets/fact_generator_daily");
    expect(await screen.findByRole("alert")).toHaveTextContent("Not allowed.");
    expect(rowCalls(calls)).toHaveLength(0); // no attempt to read rows after the 403
  });
});
