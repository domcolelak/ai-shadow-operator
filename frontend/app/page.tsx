import Link from "next/link";
import { api } from "@/lib/api";
import { count, duration, percent } from "@/lib/format";

export const dynamic = "force-dynamic";

export default async function OverviewPage() {
  const overview = await api.overview();

  if (!overview) {
    return (
      <>
        <h1>Overview</h1>
        <div className="empty">
          <p>The API is not reachable.</p>
          <p className="muted">
            Start the backend with <code>docker compose up</code>, or run{" "}
            <code>uvicorn app.main:app --reload</code> inside <code>backend/</code>.
          </p>
        </div>
      </>
    );
  }

  return (
    <>
      <h1>Overview</h1>
      <p className="subtitle">
        {count(overview.action_count)} actions kept from {overview.session_count} recorded
        session(s). {count(overview.rejected_count)} event(s) were refused at capture.
      </p>

      <div className="grid">
        <div className="card">
          <div className="card-label">Workflows discovered</div>
          <div className="card-value">{overview.candidate_count}</div>
          <div className="card-note">awaiting review</div>
        </div>
        <div className="card">
          <div className="card-label">Automations</div>
          <div className="card-value">{overview.workflow_count}</div>
          <div className="card-note">{overview.enabled_workflows} enabled</div>
        </div>
        <div className="card">
          <div className="card-label">Waiting on a person</div>
          <div className="card-value">{overview.awaiting_approval}</div>
          <div className="card-note">runs paused at an approval</div>
        </div>
        <div className="card">
          <div className="card-label">Time the candidates cover</div>
          <div className="card-value" style={{ fontSize: 20 }}>
            {duration(overview.estimated_seconds_saved)}
          </div>
          <div className="card-note">
            observed repetitions only, excluding steps that stay manual
          </div>
        </div>
      </div>

      <h2>What is being recorded</h2>
      <div className="card">
        <p style={{ marginTop: 0 }}>
          {overview.consent.allowed_origins.length === 0 ? (
            <span className="muted">Nothing is allowlisted, so nothing is recorded.</span>
          ) : (
            overview.consent.allowed_origins.map((origin) => (
              <span className="pill" key={origin}>
                {origin}
              </span>
            ))
          )}
        </p>
        <p className="muted" style={{ fontSize: 13, marginBottom: 0 }}>
          {overview.consent.note}{" "}
          <Link href="/consent">See exactly what is kept</Link>.
        </p>
      </div>

      <h2>Most repeated work</h2>
      {overview.top_candidates.length === 0 ? (
        <div className="empty">
          Nothing repeated often enough yet. A workflow needs several observed runs before
          it is proposed.
        </div>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Workflow</th>
                <th>Times observed</th>
                <th>Steps</th>
                <th>Typical duration</th>
                <th>Confidence</th>
              </tr>
            </thead>
            <tbody>
              {overview.top_candidates.map((candidate) => (
                <tr key={candidate.id}>
                  <td>
                    <Link href={`/candidates/${candidate.id}`}>{candidate.name}</Link>
                  </td>
                  <td>{candidate.observation_count}</td>
                  <td>{candidate.steps.length}</td>
                  <td>{duration(candidate.median_duration_seconds)}</td>
                  <td>{percent(candidate.confidence)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <p style={{ marginTop: 20 }}>
        <Link href="/candidates" className="pill">
          Review discovered workflows
        </Link>
      </p>
    </>
  );
}
