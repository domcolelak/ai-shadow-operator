import { api } from "@/lib/api";
import { dateTime } from "@/lib/format";

export const dynamic = "force-dynamic";

const RISK_BADGE: Record<string, string> = {
  high: "badge badge-critical",
  medium: "badge badge-high",
  low: "badge badge-medium",
};

const STATE_NOTE: Record<string, string> = {
  draft: "Accepted but not live. Only dry runs are possible.",
  enabled: "Live. High-risk steps still stop for approval.",
  disabled: "Retired. Kept for the audit trail.",
};

export default async function WorkflowsPage() {
  const [workflows, executions] = await Promise.all([api.workflows(), api.executions()]);

  if (!workflows || workflows.length === 0) {
    return (
      <>
        <h1>Automations</h1>
        <div className="empty">
          Nothing accepted yet. A discovered workflow becomes an automation only when
          somebody reviews and accepts it — and even then it starts as a draft.
        </div>
      </>
    );
  }

  const runsByWorkflow = new Map<string, number>();
  for (const execution of executions ?? []) {
    runsByWorkflow.set(
      execution.workflow_id,
      (runsByWorkflow.get(execution.workflow_id) ?? 0) + 1,
    );
  }

  return (
    <>
      <h1>Automations</h1>
      <p className="subtitle">
        A draft can only be dry-run. Enabling one still does not authorise its
        externally-visible steps: those stop for a person every time.
      </p>

      {workflows.map((workflow) => {
        const highRisk = workflow.definition.actions.filter(
          (a) => a.kind === "click" || a.kind === "api_call",
        );
        return (
          <div className="card" key={workflow.id} style={{ marginBottom: 14 }}>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <h3>{workflow.name}</h3>
              <div className="row">
                <span className="pill">{workflow.definition.actions.length} steps</span>
                <span className="pill">{runsByWorkflow.get(workflow.id) ?? 0} runs</span>
                <span className={RISK_BADGE[workflow.risk] ?? "badge badge-low"}>
                  {workflow.risk} risk
                </span>
              </div>
            </div>

            <p className="muted" style={{ fontSize: 13, margin: "6px 0" }}>
              <strong>{workflow.state}</strong> — {STATE_NOTE[workflow.state]}
              {workflow.autonomous_medium_risk && " Medium-risk steps are pre-authorised."}
            </p>

            {workflow.description && <p style={{ marginTop: 0 }}>{workflow.description}</p>}

            <div className="steps" style={{ marginTop: 8 }}>
              {workflow.definition.actions.slice(0, 12).map((action, index) => (
                <span key={index}>
                  <span
                    className="pill"
                    style={action.kind === "approval" ? { borderColor: "var(--warn)" } : undefined}
                  >
                    {action.kind === "approval" ? "⏸ " : ""}
                    {action.label || action.kind}
                  </span>
                  {index < Math.min(workflow.definition.actions.length, 12) - 1 && (
                    <span className="step-arrow">→</span>
                  )}
                </span>
              ))}
              {workflow.definition.actions.length > 12 && (
                <span className="muted" style={{ fontSize: 12 }}>
                  +{workflow.definition.actions.length - 12} more
                </span>
              )}
            </div>

            {workflow.definition.variables.length > 0 && (
              <p className="muted" style={{ fontSize: 12, marginTop: 8, marginBottom: 0 }}>
                Inputs required:{" "}
                {workflow.definition.variables.map((v) => (
                  <span className="pill" key={v.name}>
                    {v.label || v.name}
                    {v.shape ? ` (${v.shape})` : ""}
                  </span>
                ))}
              </p>
            )}

            {workflow.compilation_notes.length > 0 && (
              <details style={{ marginTop: 8 }}>
                <summary className="muted" style={{ cursor: "pointer", fontSize: 13 }}>
                  {workflow.compilation_notes.length} note(s) on how this was compiled
                </summary>
                <ul className="muted" style={{ fontSize: 13 }}>
                  {workflow.compilation_notes.map((note, index) => (
                    <li key={index}>{note.message}</li>
                  ))}
                </ul>
              </details>
            )}

            <p className="muted" style={{ fontSize: 12, marginTop: 8, marginBottom: 0 }}>
              Updated {dateTime(workflow.updated_at)} · {highRisk.length} step(s) that
              change something outside this system
            </p>
          </div>
        );
      })}

      {executions && executions.length > 0 && (
        <>
          <h2>Recent runs</h2>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Run</th>
                  <th>Mode</th>
                  <th>Status</th>
                  <th>Stopped at</th>
                  <th>When</th>
                </tr>
              </thead>
              <tbody>
                {executions.slice(0, 12).map((execution) => (
                  <tr key={execution.id}>
                    <td>
                      <code style={{ fontSize: 12 }}>{execution.id.slice(0, 8)}</code>
                      {execution.supplied_variables.length > 0 && (
                        <div className="muted" style={{ fontSize: 11 }}>
                          inputs: {execution.supplied_variables.join(", ")}
                        </div>
                      )}
                    </td>
                    <td>
                      <span className={execution.dry_run ? "badge badge-low" : "badge badge-high"}>
                        {execution.dry_run ? "dry run" : "live"}
                      </span>
                    </td>
                    <td style={{ fontSize: 13 }}>{execution.status}</td>
                    <td className="muted" style={{ fontSize: 12 }}>
                      {execution.pending_approval ?? "—"}
                    </td>
                    <td className="muted" style={{ fontSize: 12 }}>
                      {dateTime(execution.created_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="muted" style={{ fontSize: 12, padding: "0 12px 12px" }}>
              Run logs record which input a step used and the shape of its value, never
              the value itself.
            </p>
          </div>
        </>
      )}
    </>
  );
}
