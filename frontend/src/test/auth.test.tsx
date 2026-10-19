import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { CATALOG, mockApi, renderAt, signedInAs, USERS } from "./helpers";

describe("login and session", () => {
  it("sends an anonymous visitor to the login page, then back where they were going", async () => {
    const { calls } = mockApi({
      "POST /api/auth/login": ({ body }) =>
        (body as { password: string }).password === "analyst-demo-pass"
          ? [200, { access_token: "tok", token_type: "bearer", expires_in: 3600, user: USERS.analyst }]
          : [401, { error: { code: "invalid_credentials", message: "invalid username or password" } }],
      "GET /api/datasets": [200, CATALOG],
    });
    const { router } = renderAt("/datasets");
    expect(await screen.findByRole("heading", { name: "Outage Explorer" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/login");

    await userEvent.type(screen.getByLabelText("Username"), "analyst");
    await userEvent.type(screen.getByLabelText("Password"), "analyst-demo-pass");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByRole("heading", { name: "Datasets" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/datasets");
    expect(localStorage.getItem("outage-explorer.token")).toBe("tok");
    const catalogCall = calls.find((c) => c.url.pathname === "/api/datasets");
    expect(catalogCall?.headers.Authorization).toBe("Bearer tok");
  });

  it("shows one message for bad credentials and stores nothing", async () => {
    mockApi({
      "POST /api/auth/login": [401, { error: { code: "invalid_credentials", message: "invalid username or password" } }],
    });
    renderAt("/login");
    await userEvent.type(await screen.findByLabelText("Username"), "analyst");
    await userEvent.type(screen.getByLabelText("Password"), "nope");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Invalid username or password.");
    expect(localStorage.getItem("outage-explorer.token")).toBeNull();
  });

  it("shows the rate-limit message from the API", async () => {
    mockApi({
      "POST /api/auth/login": [429, { error: { code: "rate_limited", message: "too many failed logins; try again in 300 s" } }],
    });
    renderAt("/login");
    await userEvent.type(await screen.findByLabelText("Username"), "analyst");
    await userEvent.type(screen.getByLabelText("Password"), "x");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("try again in 300 s");
  });

  it("keeps the session across reloads by checking the stored token with the API", async () => {
    mockApi({ ...signedInAs("viewer"), "GET /api/datasets": [200, CATALOG] });
    renderAt("/datasets");
    expect(await screen.findByRole("heading", { name: "Datasets" })).toBeInTheDocument();
    expect(screen.getByText("Vic Viewer")).toBeInTheDocument();
  });

  it("drops a stored token the API no longer accepts", async () => {
    localStorage.setItem("outage-explorer.token", "expired");
    mockApi({ "GET /api/auth/me": [401, { error: { code: "unauthenticated", message: "access token expired" } }] });
    const { router } = renderAt("/datasets");
    expect(await screen.findByRole("button", { name: "Sign in" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/login");
    expect(localStorage.getItem("outage-explorer.token")).toBeNull();
  });

  it("signs out with a notice when the token expires mid-session", async () => {
    const { calls } = mockApi({
      ...signedInAs("analyst"),
      "GET /api/datasets": [401, { error: { code: "unauthenticated", message: "access token expired" } }],
    });
    renderAt("/datasets");
    expect(await screen.findByText("Your session expired. Please sign in again.")).toBeInTheDocument();
    expect(localStorage.getItem("outage-explorer.token")).toBeNull();
    expect(calls.some((c) => c.url.pathname === "/api/auth/logout")).toBe(false); // nothing left to revoke
  });

  it("sign out clears the token and revokes it on the server", async () => {
    const { calls } = mockApi({
      ...signedInAs("analyst"),
      "GET /api/datasets": [200, CATALOG],
      "POST /api/auth/logout": [204, null],
    });
    renderAt("/datasets");
    await userEvent.click(await screen.findByRole("button", { name: "Sign out" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Sign in" })).toBeInTheDocument());
    expect(localStorage.getItem("outage-explorer.token")).toBeNull();
    const logout = calls.filter((c) => c.url.pathname === "/api/auth/logout");
    expect(logout).toHaveLength(1);
    expect(logout[0].headers.Authorization).toBe("Bearer token-for-analyst");
  });

  it.each([
    ["viewer", false],
    ["analyst", false],
    ["admin", true],
  ] as const)("shows the Refresh link only to roles that can refresh (%s)", async (username, visible) => {
    mockApi({ ...signedInAs(username), "GET /api/datasets": [200, CATALOG] });
    renderAt("/datasets");
    await screen.findByRole("heading", { name: "Datasets" });
    expect(screen.queryByRole("link", { name: "Refresh" }) !== null).toBe(visible);
  });
});

describe("datasets list", () => {
  it("groups the datasets the API returns", async () => {
    mockApi({ ...signedInAs("viewer"), "GET /api/datasets": [200, CATALOG] });
    renderAt("/datasets");
    expect(await screen.findByRole("heading", { name: "Metrics" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "metric_fleet_offline_daily" })).toHaveAttribute(
      "href",
      "/datasets/metric_fleet_offline_daily",
    );
    expect(screen.getByText("7,229 rows · one row per day · 3 columns")).toBeInTheDocument();
  });

  it("explains when no model has been built", async () => {
    mockApi({
      ...signedInAs("analyst"),
      "GET /api/datasets": [503, { error: { code: "model_not_built", message: "no data model has been built yet" } }],
    });
    renderAt("/datasets");
    expect(await screen.findByRole("alert")).toHaveTextContent("No data yet.");
  });
});
