import { useUsage } from "@/api/hooks";
import { useAuth } from "@/auth/AuthContext";
import { ErrorBox, Spinner, fmtUsd } from "@/components/ui";

export function UsagePage() {
  const { user } = useAuth();
  const usage = useUsage();
  const rows = usage.data ?? [];
  const totals = rows.reduce((a, r) => ({ turns: a.turns + r.turn_count, tokens: a.tokens + r.input_tokens + r.output_tokens, cost: a.cost + r.cost_usd }), { turns: 0, tokens: 0, cost: 0 });
  return (
    <div>
      <div className="page-head">
        <div><h1>Usage &amp; cost</h1><p className="muted">{user?.role === "admin" ? "Every user, per day, last 30 days." : "Your own usage, per day, last 30 days."} Ceilings fail loudly with a 429.</p></div>
        <a className="btn" href="/api/v1/metrics" target="_blank" rel="noreferrer">Open /metrics</a>
      </div>
      {usage.isLoading && <Spinner />}
      <ErrorBox error={usage.error} />
      <div className="cards" style={{ marginBottom: 14 }}>
        <div className="card"><span className="muted small">questions</span><h2>{totals.turns}</h2></div>
        <div className="card"><span className="muted small">tokens</span><h2>{totals.tokens.toLocaleString()}</h2></div>
        <div className="card"><span className="muted small">cost</span><h2>{fmtUsd(totals.cost)}</h2></div>
      </div>
      <div className="table-wrap">
        <table>
          <thead><tr><th>day</th><th>user</th><th>questions</th><th>input tokens</th><th>output tokens</th><th>cost</th></tr></thead>
          <tbody>{rows.map((r) => <tr key={`${r.user_id}-${r.day}`}><td>{r.day}</td><td>{r.email ?? r.user_id}</td><td>{r.turn_count}</td><td>{r.input_tokens.toLocaleString()}</td><td>{r.output_tokens.toLocaleString()}</td><td>{fmtUsd(r.cost_usd)}</td></tr>)}</tbody>
        </table>
      </div>
    </div>
  );
}
