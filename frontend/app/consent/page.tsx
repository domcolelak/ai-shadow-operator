import { api } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function ConsentPage() {
  const consent = await api.consent();

  if (!consent) {
    return (
      <>
        <h1>What is recorded</h1>
        <div className="empty">The API is not reachable.</div>
      </>
    );
  }

  const guarantees = [
    { label: "Screenshots", captured: consent.screenshots_captured, detail: "never captured" },
    { label: "Keystrokes", captured: consent.keystrokes_captured, detail: "refused outright" },
    {
      label: "Typed values",
      captured: consent.values_stored,
      detail: "stored as salted hashes and shape descriptions only",
    },
  ];

  return (
    <>
      <h1>What is recorded</h1>
      <p className="subtitle">{consent.note}</p>

      <h2>Guarantees</h2>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Data</th>
              <th>Captured?</th>
              <th>What actually happens</th>
            </tr>
          </thead>
          <tbody>
            {guarantees.map((row) => (
              <tr key={row.label}>
                <td>{row.label}</td>
                <td>
                  <span className={row.captured ? "badge badge-critical" : "badge badge-medium"}>
                    {row.captured ? "yes" : "no"}
                  </span>
                </td>
                <td className="muted" style={{ fontSize: 13 }}>
                  {row.detail}
                </td>
              </tr>
            ))}
            <tr>
              <td>Password &amp; card fields</td>
              <td>
                <span className="badge badge-medium">no</span>
              </td>
              <td className="muted" style={{ fontSize: 13 }}>
                the step is recorded so the workflow still makes sense, but nothing is
                derived from the value — not even a hash
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <h2>Origins recorded</h2>
      {consent.allowed_origins.length === 0 ? (
        <div className="empty">
          Nothing is allowlisted, so nothing can be recorded. That is the default.
        </div>
      ) : (
        <div className="card">
          <p style={{ marginTop: 0 }}>
            {consent.allowed_origins.map((origin) => (
              <span className="pill" key={origin}>
                {origin}
              </span>
            ))}
          </p>
          <p className="muted" style={{ fontSize: 13, marginBottom: 0 }}>
            Activity anywhere else is refused at capture and never stored.
          </p>
        </div>
      )}

      {consent.blocked_origins.length > 0 && (
        <>
          <h2>Explicitly blocked</h2>
          <div className="card">
            {consent.blocked_origins.map((origin) => (
              <span className="pill" key={origin}>
                {origin}
              </span>
            ))}
          </div>
        </>
      )}

      <h2>Connectors an automation may call</h2>
      {consent.allowed_connectors.length === 0 ? (
        <div className="empty">
          None. An <code>api_call</code> step cannot run until a connector is added here —
          adding one is an administrative act, not something a generated workflow can do
          to itself.
        </div>
      ) : (
        <div className="card">
          {consent.allowed_connectors.map((connector) => (
            <span className="pill" key={connector}>
              {connector}
            </span>
          ))}
        </div>
      )}
    </>
  );
}
