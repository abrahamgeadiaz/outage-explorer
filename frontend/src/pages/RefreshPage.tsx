import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { api, ApiError } from "../api/client";
import type { RefreshJob } from "../api/types";
import { useSession } from "../auth/session";
import { ErrorBox, Loading } from "../components/States";
import { formatNumber } from "../format";

// US-06: start a refresh and follow it until it says whether it worked. The API runs the job in the
// background; this page polls its status every 2 s while it is running.

const STEPS: { key: RefreshJob["step"]; label: string }[] = [
  { key: "extract", label: "Extract from EIA" },
  { key: "model", label: "Rebuild the model" },
  { key: "done", label: "Done" },
];
export const polling = { ms: 2000 }; // shortened in tests

const STATUS_TEXT: Record<RefreshJob["status"], string> = {
  running: "Running",
  succeeded: "Succeeded",
  partial: "Partly succeeded",
  failed: "Failed",
  interrupted: "Interrupted",
};

export function RefreshPage() {
  const { user } = useSession();
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<string | null>(null);
  const [full, setFull] = useState(false);

  const jobs = useQuery({
    queryKey: [user?.username, "refresh-jobs"],
    queryFn: ({ signal }) => api<RefreshJob[]>("/api/refresh", { signal }),
  });
  const current = selected ?? jobs.data?.[0]?.job_id ?? null;

  const job = useQuery({
    queryKey: [user?.username, "refresh-job", current],
    queryFn: ({ signal }) => api<RefreshJob>(`/api/refresh/${current}`, { signal }),
    enabled: current !== null,
    refetchInterval: (q) => (q.state.data?.status === "running" ? polling.ms : false),
  });

  // When the followed job finishes with a new build, everything else cached is from the old build.
  const finishedBuild = job.data && job.data.status !== "running" ? job.data.build_id : null;
  const finishedJob = job.data && job.data.status !== "running" ? job.data.job_id : null;
  useEffect(() => {
    if (!finishedJob) return;
    if (finishedBuild) {
      void queryClient.invalidateQueries({ predicate: (q) => !String(q.queryKey[1]).startsWith("refresh") });
    }
    void queryClient.invalidateQueries({ queryKey: [user?.username, "refresh-jobs"] });
  }, [finishedJob, finishedBuild, queryClient, user?.username]);

  const start = useMutation({
    mutationFn: () => api<RefreshJob>("/api/refresh", { method: "POST", body: { full } }),
    onSuccess: (started) => {
      queryClient.setQueryData([user?.username, "refresh-job", started.job_id], started);
      setSelected(started.job_id);
      void queryClient.invalidateQueries({ queryKey: [user?.username, "refresh-jobs"] });
    },
  });

  const running = job.data?.status === "running";
  return (
    <>
      <div className="page-head">
        <h1>Refresh data</h1>
        <p className="muted">
          Fetches new and changed days from the EIA API (from each dataset's watermark, minus 30 days) and rebuilds
          the model. Users see the new data as soon as it finishes; nothing changes for them while it runs.
        </p>
      </div>

      <div className="card toolbar">
        <button type="button" className="primary" onClick={() => start.mutate()} disabled={start.isPending || running}>
          {start.isPending ? "Starting…" : running ? "Refresh running…" : "Start refresh"}
        </button>
        <label>
          <input type="checkbox" checked={full} onChange={(e) => setFull(e.target.checked)} /> Full reload from 2007
          (about 40 minutes)
        </label>
      </div>
      {start.isError && <StartError error={start.error} onShow={setSelected} />}

      {job.isPending && current !== null ? (
        <Loading label="Loading refresh status…" />
      ) : job.isError ? (
        <ErrorBox error={job.error} />
      ) : job.data ? (
        <JobView job={job.data} />
      ) : (
        <p className="muted">No refresh has been run from the API yet.</p>
      )}

      {jobs.data && jobs.data.length > 1 && (
        <section aria-labelledby="history-title">
          <h2 id="history-title">Recent refreshes</h2>
          <ul className="jobs">
            {jobs.data.map((j) => (
              <li key={j.job_id}>
                <button
                  type="button"
                  className="link-button mono"
                  onClick={() => setSelected(j.job_id)}
                  aria-current={j.job_id === current || undefined}
                >
                  {j.job_id}
                </button>{" "}
                <span className={`status status-${j.status}`}>{STATUS_TEXT[j.status]}</span>{" "}
                <span className="muted small">by {j.requested_by}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </>
  );
}

function StartError({ error, onShow }: { error: Error; onShow(jobId: string): void }) {
  if (error instanceof ApiError && error.code === "refresh_in_progress") {
    const id = error.message.match(/refresh (\S+) is still running/)?.[1];
    return (
      <p className="state state-notice" role="status">
        A refresh is already running.{" "}
        {id && (
          <button type="button" className="link-button" onClick={() => onShow(id)}>
            Follow it
          </button>
        )}
      </p>
    );
  }
  return <ErrorBox error={error} />;
}

function JobView({ job }: { job: RefreshJob }) {
  const stepIndex = STEPS.findIndex((s) => s.key === job.step);
  const elapsed = Math.round(
    ((job.finished_at ? Date.parse(job.finished_at) : Date.now()) - Date.parse(job.started_at)) / 1000,
  );
  return (
    <section className="card job" aria-labelledby="job-title" aria-live="polite">
      <h2 id="job-title">
        <span className={`status status-${job.status}`}>{STATUS_TEXT[job.status]}</span>{" "}
        <span className="mono small muted">{job.job_id}</span>
      </h2>
      <ol className="steps">
        {STEPS.map((s, i) => (
          <li
            key={s.key}
            className={i < stepIndex || job.step === "done" ? "step-done" : i === stepIndex ? "step-current" : undefined}
            aria-current={i === stepIndex && job.status === "running" ? "step" : undefined}
          >
            {s.label}
          </li>
        ))}
      </ol>
      <p className="muted small">
        {job.full ? "Full reload" : "Incremental"} · started by {job.requested_by} · {elapsed} s
        {job.build_id && (
          <>
            {" "}
            · now serving build <code>{job.build_id}</code>
          </>
        )}
      </p>
      {job.error && (
        <p className={`state ${job.status === "partial" ? "state-notice" : "state-error"}`}>{job.error}</p>
      )}
      {Object.keys(job.datasets).length > 0 && (
        <table className="job-table">
          <thead>
            <tr>
              <th scope="col">Dataset</th>
              <th scope="col">Status</th>
              <th scope="col">Rows fetched</th>
              <th scope="col">Inserted</th>
              <th scope="col">Updated</th>
              <th scope="col">Data up to</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(job.datasets).map(([name, d]) => (
              <tr key={name}>
                <td>{name}</td>
                <td>{d.status}</td>
                <td className="num">{formatNumber(d.rows_fetched)}</td>
                <td className="num">{formatNumber(d.inserted)}</td>
                <td className="num">{formatNumber(d.updated)}</td>
                <td>{d.watermark ?? "–"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {Object.keys(job.dq_issues).length > 0 && (
        <p className="small">
          Data quality findings in the new build:{" "}
          {Object.entries(job.dq_issues)
            .map(([severity, n]) => `${n} ${severity}`)
            .join(" · ")}{" "}
          (see the <code>dq_issue</code> dataset).
        </p>
      )}
    </section>
  );
}
