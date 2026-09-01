import Link from "next/link";
import { notFound } from "next/navigation";
import { api } from "@/lib/api";
import { duration, percent } from "@/lib/format";

export const dynamic = "force-dynamic";

const VALUE_CLASS_LABEL: Record<string, string> = {
  variable: "differs every run",
  constant: "same every run",
  sensitive: "never captured",
  unknown: "",
};

export default async function CandidateDetailPage({ params }: { params: { id: string } }) {
  const candidate = await api.candidate(params.id);
  if (!candidate) notFound();

  const optional = candidate.steps.filter((s) => s.optional);
  const manual = candidate.steps.filter((s) => s.requires_human);

  return (
    <>
      <h1>{candidate.name}</h1>
      <p className="subtitle">
        Observed {candidate.observation_count} times across {candidate.session_ids.length}{" "}
        recorded session(s).
      </p>

      <div className="grid">
        <div className="card">
          <div className="card-label">Times observed</div>
          <div className="card-value">{candidate.observation_count}</div>
          <div className="card-note">this is a count, not an estimate</div>
        </div>
        <div className="card">
          <div className="card-label">Confidence</div>
          <div className="card-value">{percent(candidate.confidence)}</div>
          <div className="card-note">
            repetition and agreement, less a penalty for each branch
          </div>
        </div>
        <div className="card">
          <div className="card-label">Typical duration</div>
          <div className="card-value" style={{ fontSize: 20 }}>
            {duration(candidate.median_duration_seconds)}
          </div>
          <div className="card-note">median across observed runs</div>
        </div>
        <div className="card">
          <div className="card-label">Time this covers</div>
          <div className="card-value" style={{ fontSize: 20 }}>
            {duration(candidate.estimated_seconds_saved)}
          </div>
          <div className="card-note">
            observed repetitions only, excluding steps that stay manual
          </div>
        </div>
      </div>

      <h2>Steps</h2>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>#</th>
              <th>Action</th>
              <th>Where</th>
              <th>Value</th>
              <th>Present in</th>
            </tr>
          </thead>
          <tbody>
            {candidate.steps.map((step) => (
              <tr key={step.position}>
                <td>{step.position}</td>
                <td>
                  <code style={{ fontSize: 12 }}>{step.action}</code>{" "}
                  {step.label || step.target_path}
                  {step.requires_human && (
                    <div>
                      <span className="badge badge-high">stays manual</span>
                    </div>
                  )}
                </td>
                <td className="muted" style={{ fontSize: 12 }}>
                  {step.origin}
                </td>
                <td className="muted" style={{ fontSize: 12 }}>
                  {VALUE_CLASS_LABEL[step.value_class] ?? step.value_class}
                </td>
                <td>
                  {step.optional ? (
                    <span className="pill">optional · {percent(step.presence)}</span>
                  ) : (
                    <span className="muted" style={{ fontSize: 12 }}>
                      every run
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <h2>Inputs the automation would need</h2>
      {candidate.variables.length === 0 ? (
        <div className="empty">No field varied between runs.</div>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Field</th>
                <th>Shape observed</th>
                <th>Distinct values</th>
                <th>Observations</th>
              </tr>
            </thead>
            <tbody>
              {candidate.variables.map((variable) => (
                <tr key={variable.step_index}>
                  <td>{variable.label || variable.field_name}</td>
                  <td className="muted" style={{ fontSize: 13 }}>
                    {variable.value_shape || "—"}
                  </td>
                  <td>{variable.distinct_values}</td>
                  <td>{variable.observations}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="muted" style={{ fontSize: 12, padding: "0 12px 12px" }}>
            Shapes and counts come from salted hashes. The values themselves were never
            recorded, so none can be shown or replayed.
          </p>
        </div>
      )}

      <h2>Where runs diverged</h2>
      {candidate.branches.length === 0 ? (
        <div className="empty">Every observed run took the same path.</div>
      ) : (
        <>
          <p className="muted" style={{ marginTop: -6, fontSize: 13 }}>
            The miner can see that runs split here, but not why. Each of these becomes a
            question for a person rather than a guess.
          </p>
          {candidate.branches.map((branch) => (
            <div className="card" key={branch.after_step} style={{ marginBottom: 12 }}>
              <h3>After step {branch.after_step}</h3>
              <table>
                <tbody>
                  {branch.alternatives.map((alternative) => (
                    <tr key={alternative.signature}>
                      <td style={{ fontSize: 12 }}>
                        <code>{alternative.signature.split("::").slice(-1)[0] || "next step"}</code>
                      </td>
                      <td style={{ width: 140 }}>
                        <div className="bar">
                          <span style={{ width: `${alternative.share * 100}%` }} />
                        </div>
                      </td>
                      <td style={{ fontSize: 13 }}>
                        {percent(alternative.share)} ({alternative.runs} runs)
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ))}
        </>
      )}

      {(optional.length > 0 || manual.length > 0) && (
        <>
          <h2>What would not be automated</h2>
          <div className="card">
            <ul style={{ margin: 0, paddingLeft: 18 }}>
              {manual.map((step) => (
                <li key={`m-${step.position}`}>
                  <strong>{step.label}</strong> — the value was never captured, so this
                  stays with a person.
                </li>
              ))}
              {optional.map((step) => (
                <li key={`o-${step.position}`}>
                  <strong>{step.label}</strong> — done in only {percent(step.presence)} of
                  runs, so it is optional rather than assumed.
                </li>
              ))}
            </ul>
          </div>
        </>
      )}

      {Object.keys(candidate.narrative).length > 0 && (
        <>
          <h2>Description</h2>
          <p className="muted" style={{ marginTop: -6, fontSize: 13 }}>
            Generated from the evidence above. It names the workflow; it does not decide
            anything about it.
          </p>
          <pre className="evidence">{JSON.stringify(candidate.narrative, null, 2)}</pre>
        </>
      )}

      <p style={{ marginTop: 20 }}>
        <Link href="/candidates" className="pill">
          Back to discovered workflows
        </Link>
      </p>
    </>
  );
}
