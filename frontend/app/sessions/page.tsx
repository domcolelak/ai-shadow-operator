import { api } from "@/lib/api";
import { count, dateTime } from "@/lib/format";

export const dynamic = "force-dynamic";

export default async function SessionsPage() {
  const sessions = await api.sessions();

  if (!sessions || sessions.length === 0) {
    return (
      <>
        <h1>Recordings</h1>
        <div className="empty">
          No recordings. Nothing is captured until somebody explicitly starts one.
        </div>
      </>
    );
  }

  const totalRejected = sessions.reduce((sum, s) => sum + s.rejected_count, 0);

  return (
    <>
      <h1>Recordings</h1>
      <p className="subtitle">
        {sessions.length} session(s). {count(totalRejected)} event(s) were refused at
        capture and never stored.
      </p>

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Session</th>
              <th>Device</th>
              <th>Status</th>
              <th>Kept</th>
              <th>Refused</th>
              <th>Started</th>
            </tr>
          </thead>
          <tbody>
            {sessions.map((session) => (
              <tr key={session.id}>
                <td>
                  <strong>{session.external_id}</strong>
                  {session.label && (
                    <div className="muted" style={{ fontSize: 12 }}>
                      {session.label}
                    </div>
                  )}
                </td>
                <td className="muted" style={{ fontSize: 13 }}>
                  {session.device || "—"}
                </td>
                <td>
                  <span
                    className={
                      session.status === "recording"
                        ? "badge badge-critical"
                        : "badge badge-low"
                    }
                  >
                    {session.status}
                  </span>
                </td>
                <td>{count(session.action_count)}</td>
                <td>
                  {session.rejected_count > 0 ? (
                    <>
                      {session.rejected_count}
                      <div className="muted" style={{ fontSize: 11 }}>
                        {session.rejection_reasons.slice(0, 2).join("; ")}
                      </div>
                    </>
                  ) : (
                    "0"
                  )}
                </td>
                <td className="muted" style={{ fontSize: 13 }}>
                  {dateTime(session.started_at)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
