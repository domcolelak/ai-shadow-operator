import Link from "next/link";
import { api } from "@/lib/api";
import { duration, percent } from "@/lib/format";

export const dynamic = "force-dynamic";

export default async function CandidatesPage() {
  const candidates = await api.candidates();

  if (!candidates || candidates.length === 0) {
    return (
      <>
        <h1>Discovered workflows</h1>
        <div className="empty">
          Nothing repeated often enough to propose. A workflow needs several observed runs
          before it appears here — one-off work is not a workflow.
        </div>
      </>
    );
  }

  return (
    <>
      <h1>Discovered workflows</h1>
      <p className="subtitle">
        Found by comparing recorded sessions, not by asking a model. Every count below is
        a fact about what was observed.
      </p>

      {candidates.map((candidate) => {
        const optional = candidate.steps.filter((s) => s.optional).length;
        const manual = candidate.steps.filter((s) => s.requires_human).length;
        return (
          <div className="card" key={candidate.id} style={{ marginBottom: 14 }}>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <h3>
                <Link href={`/candidates/${candidate.id}`}>{candidate.name}</Link>
              </h3>
              <div className="row">
                <span className="pill">observed {candidate.observation_count}x</span>
                <span className="pill">confidence {percent(candidate.confidence)}</span>
              </div>
            </div>

            <p className="muted" style={{ fontSize: 13, margin: "6px 0" }}>
              {candidate.steps.length} steps · {candidate.variables.length} variable(s) ·{" "}
              {candidate.branches.length} branch point(s) · {optional} optional step(s)
              {manual > 0 ? ` · ${manual} step(s) stay manual` : ""} · typically{" "}
              {duration(candidate.median_duration_seconds)}
            </p>

            <div className="steps">
              {candidate.steps.slice(0, 10).map((step, index) => (
                <span key={step.position}>
                  <span className="pill" style={step.optional ? { opacity: 0.6 } : undefined}>
                    {step.label || step.action}
                  </span>
                  {index < Math.min(candidate.steps.length, 10) - 1 && (
                    <span className="step-arrow">→</span>
                  )}
                </span>
              ))}
              {candidate.steps.length > 10 && (
                <span className="muted" style={{ fontSize: 12 }}>
                  +{candidate.steps.length - 10} more
                </span>
              )}
            </div>

            {candidate.branches.length > 0 && (
              <p className="muted" style={{ fontSize: 12, marginBottom: 0, marginTop: 8 }}>
                Runs diverged at {candidate.branches.length} point(s). The reason was not
                observable, so a person decides at each one.
              </p>
            )}
          </div>
        );
      })}
    </>
  );
}
