import { useState } from "react";
import { Link } from "react-router-dom";
import { ApiError } from "@/api/client";
import { useChart, useChartUsage, useCharts, useDashboards, useDashboardMutations, useDeleteChart, useUpdateChart } from "@/api/hooks";
import { PlotlyChart } from "@/components/PlotlyChart";
import { Badge, Button, Collapsible, ErrorBox, Modal, Spinner } from "@/components/ui";

export function ChartsPage() {
  const [search, setSearch] = useState("");
  const charts = useCharts(search);
  const [selected, setSelected] = useState<string | null>(null);
  return (
    <div>
      <div className="page-head">
        <div><h1>Chart library</h1><p className="muted">Every pinned chart lives here once, however many dashboards show it.</p></div>
        <input style={{ width: 280 }} placeholder="Search title or question…" value={search} onChange={(e) => setSearch(e.target.value)} />
      </div>
      {charts.isLoading && <Spinner />}
      <ErrorBox error={charts.error} />
      <div className="cards">
        {charts.data?.items.map((c) => (
          <div className="card" key={c.id}>
            <div className="row spread">
              <h3>{c.title}</h3>
              {c.is_archived && <Badge>archived</Badge>}
            </div>
            <p className="muted small">{c.question}</p>
            <div className="row small muted">
              <span>v{c.version}</span>
              <span>· {c.placement_count} placement{c.placement_count === 1 ? "" : "s"}</span>
              <span>· {c.sql_hash.slice(0, 10)}</span>
            </div>
            <div className="row">
              <Button size="sm" onClick={() => setSelected(c.id)}>Open</Button>
            </div>
          </div>
        ))}
        {charts.data?.items.length === 0 && <p className="muted">No charts yet — pin one from a conversation.</p>}
      </div>
      {selected && <ChartDetail id={selected} onClose={() => setSelected(null)} />}
    </div>
  );
}

function ChartDetail({ id, onClose }: { id: string; onClose: () => void }) {
  const chart = useChart(id);
  const usage = useChartUsage(id);
  const dashboards = useDashboards();
  const del = useDeleteChart();
  const upd = useUpdateChart();
  const [target, setTarget] = useState("");
  const dm = useDashboardMutations(target);
  const conflict = del.error instanceof ApiError && del.error.status === 409 ? del.error.problem : null;

  return (
    <Modal title={chart.data?.title ?? "Chart"} onClose={onClose} wide>
      {chart.isLoading && <Spinner />}
      {chart.data && (
        <div className="stack">
          <p className="muted">{chart.data.question}</p>
          <PlotlyChart spec={chart.data.chart_spec} height={320} />
          <Collapsible title={<span>SQL · <span className="muted small">{chart.data.sql_hash}</span></span>}><pre>{chart.data.sql_text}</pre></Collapsible>
          <div className="panel">
            <div className="panel-head"><h3>Usage</h3>{usage.data && <Badge tone="info">{usage.data.dashboard_count} dashboard{usage.data.dashboard_count === 1 ? "" : "s"} · {usage.data.placements.length} tile{usage.data.placements.length === 1 ? "" : "s"}</Badge>}</div>
            {usage.data?.placements.length === 0 && <p className="muted small">Not placed on any dashboard.</p>}
            {usage.data?.placements.map((p) => (
              <div key={p.tile_id} className="row small" style={{ padding: "4px 0" }}>
                <Link to={`/dashboards/${p.dashboard_id}`}>{p.dashboard_name}</Link> <span className="muted">› {p.group_title}</span>
                {p.title_override && <span className="muted">as “{p.title_override}”</span>}
              </div>
            ))}
          </div>
          <div className="panel">
            <h3>Place on a dashboard</h3>
            <div className="row">
              <select value={target} onChange={(e) => setTarget(e.target.value)} style={{ width: 280 }}>
                <option value="">Choose a dashboard…</option>
                {dashboards.data?.filter((d) => d.effective_role !== "viewer").map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
              </select>
              <Button variant="primary" disabled={!target || dm.addTile.isPending} onClick={() => dm.addTile.mutate({ saved_chart_id: id }, { onSuccess: () => usage.refetch() })}>Add tile</Button>
              {dm.addTile.isSuccess && <Badge tone="ok">added</Badge>}
            </div>
            <ErrorBox error={dm.addTile.error} />
          </div>
          <div className="row spread">
            <div className="row">
              <Button size="sm" onClick={() => upd.mutate({ id, is_archived: !chart.data!.is_archived })}>{chart.data.is_archived ? "Unarchive" : "Archive"}</Button>
              <Button size="sm" onClick={() => { const t = prompt("New title", chart.data!.title); if (t) upd.mutate({ id, title: t }); }}>Rename</Button>
            </div>
            <Button size="sm" variant="danger" disabled={del.isPending} onClick={() => del.mutate(id, { onSuccess: onClose })}>Delete</Button>
          </div>
          {conflict && (
            <div className="error-box">
              <strong>Still in use.</strong> {conflict.detail}
              <ul>{(conflict.dashboards as { id: string; name: string }[]).map((d) => <li key={d.id}><Link to={`/dashboards/${d.id}`}>{d.name}</Link></li>)}</ul>
            </div>
          )}
          {del.isError && !conflict && <ErrorBox error={del.error} />}
        </div>
      )}
    </Modal>
  );
}
