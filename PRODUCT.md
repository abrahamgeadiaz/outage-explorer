# Product notes

Outage Explorer replaces "export from EIA into a spreadsheet" for people who follow the U.S. nuclear
fleet. The first version covers the brief's eight stories; this page is about the choices around them.

## Stories I added

- **US-09** As an Analyst or Viewer, I want to see when the fleet number is incomplete, so I don't
  read a dip as an outage. On some days units are missing from every EIA grain (Palisades for three
  years), and the share offline silently changes. The fleet page counts incomplete days, shades them
  behind the line, and the tooltip shows the min–max range the true value lies in.
- **US-10** As an Analyst, I want to page through a large SQL result and export it, so a query can
  replace a download. Pages of up to 5,000 rows, a warning when paging a query without `ORDER BY`,
  CSV export.
- **US-11** As an Analyst, I want a head start on SQL: example queries, a schema panel that inserts a
  column at the cursor, and my recent queries.
- **US-12** As an Admin, I want to know what a refresh changed, not only that it ended: rows fetched,
  inserted and updated per dataset, the new build, and the data quality findings it raised.
- **US-13** As any user, when something is not for my role I want to be told so, not see an empty page.

## Key UX decisions

- **Show the gaps, never fill them.** The fleet chart keeps EIA's denominator and puts what's missing
  next to it, because a filled-in number would look more complete than the data is.
- **The server decides, the UI explains.** Links a role can't use are hidden, but every screen also
  handles `403` with "not allowed", and keeps it apart from "empty" and "error".
- **State lives in the URL.** Dataset filters and the page are in the address, so a view can be
  bookmarked or shared and the back button works.
- **SQL tells you what happened.** Errors show their code (`function_not_allowed`, `query_timeout`...),
  results show timing and whether they came from the cache, and Ctrl+Enter runs the query.
- **Refresh is followed, not waited on.** It starts at once and the page polls every 2 s. Starting a
  second one while a job runs says so and offers to follow the running job.
- **Basics on every screen:** loading, empty and error states, light and dark themes, a layout that
  works at phone width.

## What I left out

- SQL autocomplete (the schema panel covers most of it), saved or shared queries.
- Charts other than US-08, and a page per facility or unit.
- User management, password change, single sign-on: three seeded users, as the brief allows.
- A production deployment (HTTPS, one server for the app and the API).

## What I'd build next

1. **Run each SQL query in its own process**, killed on timeout, with a per-user share of the query
   slots: the step before opening SQL to more users (DECISIONS.md #27).
2. **A facility and unit page**: outage history over time, capacity ratings, reporting gaps.
3. **Alerts**: a unit going offline, or the fleet crossing a threshold, after each refresh.
4. **Keep revised values** instead of overwriting them, to measure how far back EIA really revises
   (the 30-day window is still an assumption) and to show what changed.
5. **Saved queries with shareable links**, and sign-in through the organization's identity provider.
