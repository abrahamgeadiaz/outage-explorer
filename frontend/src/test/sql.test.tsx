import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { toCsv } from "../csv";
import { loadHistory, remember } from "../history";
import { CATALOG, mockApi, renderAt, signedInAs } from "./helpers";

function result(offset: number, extra: Record<string, unknown> = {}) {
  return {
    build_id: "b1",
    tables: ["fact_national_daily"],
    columns: [
      { name: "period", type: "DATE" },
      { name: "capacity_mw", type: "DECIMAL(12,3)" },
    ],
    rows: [
      [`2015-10-0${offset + 1}`, 100056.7],
      [`2015-10-0${offset + 2}`, 99892],
    ],
    row_count: 2,
    offset,
    truncated: offset === 0,
    next_offset: offset === 0 ? 2 : null,
    ordered: true,
    elapsed_ms: 12,
    cached: false,
    ...extra,
  };
}

async function editorWith(sql: string) {
  const editor = await screen.findByLabelText("SQL query");
  await userEvent.clear(editor);
  await userEvent.type(editor, sql);
  return editor;
}

describe("SQL page", () => {
  it("runs the query with Ctrl+Enter and shows the result", async () => {
    const { calls } = mockApi({
      ...signedInAs("analyst"),
      "GET /api/datasets": [200, CATALOG],
      "POST /api/query": [200, result(0, { cached: true })],
    });
    renderAt("/sql");
    const editor = await editorWith("SELECT * FROM fact_national_daily ORDER BY 1");
    await userEvent.type(editor, "{Control>}{Enter}{/Control}");

    const table = await screen.findByRole("table", { name: "Query result" });
    expect(within(table).getByText("100,056.7")).toBeInTheDocument();
    expect(screen.getByText("Rows 1–2 (more available)")).toBeInTheDocument();
    expect(screen.getByText(/read fact_national_daily/)).toBeInTheDocument();
    expect(screen.getByText("cached")).toBeInTheDocument();
    const post = calls.find((c) => c.method === "POST")!;
    expect(post.body).toEqual({ sql: "SELECT * FROM fact_national_daily ORDER BY 1", max_rows: 1000, offset: 0 });
  });

  it("pages with next_offset and comes back with Prev", async () => {
    const { calls } = mockApi({
      ...signedInAs("analyst"),
      "GET /api/datasets": [200, CATALOG],
      "POST /api/query": ({ body }) => [200, result((body as { offset: number }).offset)],
    });
    renderAt("/sql");
    await userEvent.click(await screen.findByRole("button", { name: "Run" }));
    await userEvent.click(await screen.findByRole("button", { name: "Next ›" }));
    expect(await screen.findByText("Rows 3–4")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next ›" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "‹ Prev" }));
    expect(await screen.findByText("Rows 1–2 (more available)")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "‹ Prev" })).toBeDisabled(); // back on the first page
    const offsets = calls.filter((c) => c.method === "POST").map((c) => (c.body as { offset: number }).offset);
    expect(offsets).toEqual([0, 2, 0]);
  });

  it("running the editor again starts from the first page", async () => {
    const { calls } = mockApi({
      ...signedInAs("analyst"),
      "GET /api/datasets": [200, CATALOG],
      "POST /api/query": ({ body }) => [200, result((body as { offset: number }).offset)],
    });
    renderAt("/sql");
    await userEvent.click(await screen.findByRole("button", { name: "Run" }));
    await userEvent.click(await screen.findByRole("button", { name: "Next ›" }));
    await screen.findByText("Rows 3–4");
    await userEvent.click(screen.getByRole("button", { name: "Run" }));
    expect(await screen.findByText("Rows 1–2 (more available)")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "‹ Prev" })).toBeDisabled();
    expect(calls.filter((c) => c.method === "POST").map((c) => (c.body as { offset: number }).offset)).toEqual([0, 2, 0]);
  });

  it("warns when an unordered result is paged", async () => {
    mockApi({
      ...signedInAs("analyst"),
      "GET /api/datasets": [200, CATALOG],
      "POST /api/query": [200, result(0, { ordered: false })],
    });
    renderAt("/sql");
    await userEvent.click(await screen.findByRole("button", { name: "Run" }));
    expect(await screen.findByText(/has no ORDER BY/)).toBeInTheDocument();
  });

  it("shows a rejected statement with its code", async () => {
    mockApi({
      ...signedInAs("analyst"),
      "GET /api/datasets": [200, CATALOG],
      "POST /api/query": [400, { error: { code: "read_only", message: "only SELECT queries are allowed, not DELETE" } }],
    });
    renderAt("/sql");
    await editorWith("DELETE FROM fact_national_daily");
    await userEvent.click(screen.getByRole("button", { name: "Run" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("only SELECT queries are allowed, not DELETE");
    expect(alert).toHaveTextContent("read_only");
  });

  it("shows a refused table as 'not allowed'", async () => {
    mockApi({
      ...signedInAs("viewer"),
      "GET /api/datasets": [200, CATALOG],
      "POST /api/query": [403, { error: { code: "forbidden", message: "role 'viewer' has no access to: fact_generator_daily" } }],
    });
    renderAt("/sql");
    await editorWith("SELECT * FROM fact_generator_daily");
    await userEvent.click(screen.getByRole("button", { name: "Run" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Not allowed. role 'viewer' has no access to: fact_generator_daily");
  });

  it("lists only the caller's datasets and inserts a name at the cursor", async () => {
    mockApi({ ...signedInAs("viewer"), "GET /api/datasets": [200, CATALOG] });
    renderAt("/sql");
    const editor = (await editorWith("SELECT * FROM ")) as HTMLTextAreaElement;
    await userEvent.click(await screen.findByRole("button", { name: "metric_fleet_offline_daily" }));
    await waitFor(() => expect(editor.value).toBe("SELECT * FROM metric_fleet_offline_daily"));
    expect(screen.queryByRole("button", { name: "fact_generator_daily" })).toBeNull();
  });

  it("remembers queries per user and loads them back", async () => {
    mockApi({
      ...signedInAs("analyst"),
      "GET /api/datasets": [200, CATALOG],
      "POST /api/query": [200, result(0)],
    });
    renderAt("/sql");
    await editorWith("SELECT 42");
    await userEvent.click(screen.getByRole("button", { name: "Run" }));
    await screen.findByRole("table", { name: "Query result" });
    expect(loadHistory("analyst").map((h) => h.sql)).toEqual(["SELECT 42"]);
    expect(loadHistory("viewer")).toEqual([]);

    await editorWith("SELECT 1");
    await userEvent.click(screen.getByRole("button", { name: "SELECT 42" }));
    expect((screen.getByLabelText("SQL query") as HTMLTextAreaElement).value).toBe("SELECT 42");
  });

  it("is not offered to roles without the query capability", async () => {
    mockApi({
      "GET /api/auth/me": [200, { username: "x", display_name: "X", role: "viewer", capabilities: ["catalog"] }],
    });
    localStorage.setItem("outage-explorer.token", "t");
    renderAt("/sql");
    expect(await screen.findByRole("alert")).toHaveTextContent("Your role cannot use this page.");
  });
});

describe("helpers", () => {
  it("escapes CSV fields", () => {
    const csv = toCsv(
      [{ name: "a", type: "VARCHAR" }, { name: "b", type: "VARCHAR[]" }],
      [
        ['say "hi", ok', ["6462/1", "649/3"]],
        [null, []],
      ],
    );
    expect(csv).toBe('a,b\r\n"say ""hi"", ok","[""6462/1"",""649/3""]"\r\n,[]');
  });

  it("keeps the newest 20 distinct queries", () => {
    for (let i = 0; i < 25; i++) remember("u", `SELECT ${i}`, true);
    remember("u", "SELECT 3", true);
    const sqls = loadHistory("u").map((h) => h.sql);
    expect(sqls).toHaveLength(20);
    expect(sqls[0]).toBe("SELECT 3");
    expect(new Set(sqls).size).toBe(20);
  });
});
