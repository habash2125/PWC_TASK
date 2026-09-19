import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import GridLayout, { WidthProvider, type Layout } from "react-grid-layout";
import { useCharts, useDashboard, useDashboardMutations, useGrants, useUsers } from "@/api/hooks";
import { PlotlyChart } from "@/components/PlotlyChart";
import { Badge, Button, Collapsible, DataTable, ErrorBox, Modal, Spinner, fmtMs } from "@/components/ui";
import type { Dashboard, DashboardRole, Group, GroupingProposal, Tile, TileRefresh } from "@/types/api";

const Grid = WidthProvider(GridLayout);

export function DashboardPage() {
  const { id = "" } = useParams();
  const nav = useNavigate();
  const q = useDashboard(id);
  const m = useDashboardMutations(id);
  const [refreshes, setRefreshes] = useState<Record<string, TileRefresh>>({});
  const [share, setShare] = useState(false);
  const [addTile, setAddTile] = useState<Group | null>(null);
  const [proposal, setProposal] = useState<GroupingProposal | null>(null);
  const d = q.data;
  const canEdit = d?.effective_role === "editor" || d?.effective_role === "owner";
  const isOwner = d?.effective_role === "owner";

  const refreshAll = () =>
    m.refreshAll.mutate(undefined, {
      onSuccess: (res) => setRefreshes((old) => ({ ...old, ...Object.fromEntries(res.tiles.map((t) => [t.tile_id, t])) })),
    });
  const refreshOne = (tid: string) => m.refreshTile.mutate(tid, { onSuccess: (r) => setRefreshes((old) => ({ ...old, [tid]: r })) });

  if (q.isLoading) return <Spinner label="Loading dashboard…" />;
  if (q.error) return <ErrorBox error={q.error} />;
  if (!d) return null;
  const totalLlm = Object.values(refreshes).reduce((a, r) => a + r.llm_calls, 0);

  return (
    <div>
      <div className="page-head">
        <div>
          <div className="row"><h1>{d.name}</h1><Badge tone={isOwner ? "info" : "neutral"}>{d.effective_role}</Badge><Badge>{d.visibility}</Badge></div>
          {d.description && <p className="muted">{d.description}</p>}
        </div>
        <div className="row">
          <Button onClick={refreshAll} disabled={m.refreshAll.isPending}>{m.refreshAll.isPending ? "Refreshing…" : "Refresh all"}</Button>
          {Object.keys(refreshes).length > 0 && <Badge tone={totalLlm === 0 ? "ok" : "danger"}>{totalLlm} model calls on refresh</Badge>}
          {canEdit && <Button onClick={() => m.addGroup.mutate(prompt("Group title") || "")} disabled={m.addGroup.isPending}>+ Group</Button>}
          {canEdit && d.groups.reduce((n, g) => n + g.tiles.length, 0) >= 2 && (
            <Button onClick={() => m.suggest.mutate(undefined, { onSuccess: setProposal })} disabled={m.suggest.isPending}>{m.suggest.isPending ? "Thinking…" : "Suggest groups"}</Button>
          )}
          {isOwner && <Button onClick={() => setShare(true)}>Share</Button>}
          {isOwner && <Button variant="danger" onClick={() => { if (confirm("Delete this dashboard? Charts are kept in the library.")) m.remove.mutate(undefined, { onSuccess: () => nav("/dashboards") }); }}>Delete</Button>}
        </div>
      </div>
      <ErrorBox error={m.suggest.error ?? m.refreshAll.error ?? m.addGroup.error ?? m.deleteTile.error ?? m.deleteGroup.error ?? m.remove.error} />
      {d.groups.map((g) => (
        <GroupView key={g.id} d={d} g={g} canEdit={!!canEdit} m={m} refreshes={refreshes} onRefresh={refreshOne} onAddTile={() => setAddTile(g)} />
      ))}
      {share && <ShareDialog d={d} m={m} onClose={() => setShare(false)} />}
      {addTile && <AddTileDialog group={addTile} m={m} onClose={() => setAddTile(null)} />}
      {proposal && <GroupingDialog d={d} proposal={proposal} m={m} onClose={() => setProposal(null)} />}
    </div>
  );
}

function GroupView({ d, g, canEdit, m, refreshes, onRefresh, onAddTile }: {
  d: Dashboard; g: Group; canEdit: boolean; m: ReturnType<typeof useDashboardMutations>;
  refreshes: Record<string, TileRefresh>; onRefresh: (tid: string) => void; onAddTile: () => void;
}) {
  const layout: Layout[] = useMemo(() => g.tiles.map((t) => ({ i: t.id, x: t.x, y: t.y, w: t.w, h: t.h, minW: 3, minH: 3, static: !canEdit })), [g.tiles, canEdit]);
  const onLayoutChange = useCallback((l: Layout[]) => {
    const changed = l.filter((item) => { const t = g.tiles.find((x) => x.id === item.i); return t && (t.x !== item.x || t.y !== item.y || t.w !== item.w || t.h !== item.h); });
    if (changed.length) m.layout.mutate(changed.map((item) => ({ tile_id: item.i, x: item.x, y: item.y, w: item.w, h: item.h })));
  }, [g.tiles, m.layout]);
  const otherGroups = d.groups.filter((x) => x.id !== g.id);
  return (
    <section className="group">
      <div className="group-head">
        <div className="row">
          <button className="btn btn-ghost btn-sm" onClick={() => m.updateGroup.mutate({ gid: g.id, is_collapsed: !g.is_collapsed })}>{g.is_collapsed ? "▸" : "▾"}</button>
          <h2>{g.title}</h2>
          <span className="muted small">{g.tiles.length} tile{g.tiles.length === 1 ? "" : "s"}</span>
        </div>
        {canEdit && (
          <div className="row">
            <Button size="sm" onClick={onAddTile}>+ Tile</Button>
            <Button size="sm" onClick={() => { const t = prompt("Rename group", g.title); if (t) m.updateGroup.mutate({ gid: g.id, title: t }); }}>Rename</Button>
            <Button size="sm" onClick={() => m.updateGroup.mutate({ gid: g.id, position: Math.max(0, g.position - 1) })} disabled={g.position === 0}>↑</Button>
            <Button size="sm" onClick={() => m.updateGroup.mutate({ gid: g.id, position: g.position + 1 })} disabled={g.position >= d.groups.length - 1}>↓</Button>
            {g.position > 0 && <Button size="sm" variant="danger" onClick={() => { if (confirm("Remove this group? Its tiles move to the first group.")) m.deleteGroup.mutate(g.id); }}>Remove</Button>}
          </div>
        )}
      </div>
      {!g.is_collapsed && (
        <Grid className="layout" layout={layout} cols={12} rowHeight={80} margin={[12, 12]} draggableHandle=".tile-head" draggableCancel=".tile-head button" isDraggable={canEdit} isResizable={canEdit} onDragStop={onLayoutChange} onResizeStop={onLayoutChange} compactType="vertical">
          {g.tiles.map((t) => (
            <div key={t.id}>
              <TileView t={t} canEdit={canEdit} m={m} refresh={refreshes[t.id]} onRefresh={() => onRefresh(t.id)} otherGroups={otherGroups} />
            </div>
          ))}
        </Grid>
      )}
      {!g.is_collapsed && g.tiles.length === 0 && <p className="muted small">Empty group. {canEdit && "Add a tile from the chart library."}</p>}
    </section>
  );
}

function TileView({ t, canEdit, m, refresh, onRefresh, otherGroups }: {
  t: Tile; canEdit: boolean; m: ReturnType<typeof useDashboardMutations>; refresh?: TileRefresh; onRefresh: () => void; otherGroups: Group[];
}) {
  const [showData, setShowData] = useState(false);
  const spec = refresh?.chart_spec ?? t.chart_spec;
  const tone = !refresh ? "neutral" : refresh.status === "ok" ? "ok" : refresh.status === "blocked" ? "danger" : "warn";
  return (
    <div className="tile">
      <div className="tile-head">
        <strong title={t.question}>{t.title}</strong>
        <div className="row" style={{ flexWrap: "nowrap" }}>
          <button className="btn btn-ghost btn-sm" title="Refresh (zero model calls)" onClick={onRefresh}>⟳</button>
          <button className="btn btn-ghost btn-sm" title="Data & SQL" onClick={() => setShowData(true)}>⋯</button>
          {canEdit && <button className="btn btn-ghost btn-sm" title="Remove tile (chart stays in library)" disabled={m.deleteTile.isPending} onClick={() => m.deleteTile.mutate(t.id)}>✕</button>}
        </div>
      </div>
      <div className="tile-body">
        {refresh && refresh.status !== "ok" ? (
          <div className="error-box" style={{ margin: 8 }}>
            <strong>{refresh.status === "invalid_query" ? "Query no longer valid" : refresh.status === "blocked" ? "Blocked" : "Error"}</strong>
            <div className="small">{refresh.message}</div>
            <div className="small muted">Stored SQL hash {t.sql_hash.slice(0, 10)} — open ⋯ to review it.</div>
          </div>
        ) : (
          <PlotlyChart spec={spec} height={Math.max(160, t.h * 80 - 80)} />
        )}
      </div>
      <div className="tile-foot">
        <Badge tone={tone}>{refresh ? `${refresh.status} · ${refresh.row_count ?? 0} rows · ${fmtMs(refresh.duration_ms)} · ${refresh.llm_calls} model calls` : `v${t.chart_version} · pinned snapshot`}</Badge>
        {refresh?.drifted && <Badge tone="warn">sql drift</Badge>}
        {refresh?.trace_id && <Link to={`/traces/${refresh.trace_id}`}>trace</Link>}
      </div>
      {showData && (
        <Modal title={t.title} onClose={() => setShowData(false)} wide>
          <div className="stack">
            <p className="muted">{t.question}</p>
            {refresh && refresh.status === "ok" ? <DataTable columns={refresh.columns} rows={refresh.rows} /> : <p className="muted small">Refresh the tile to see live rows under your scope.</p>}
            <Collapsible title="Stored SQL (scope placeholder bound at execution)" defaultOpen><pre>{t.sql_text}</pre></Collapsible>
            {canEdit && (
              <div className="row">
                <Button size="sm" onClick={() => { const v = prompt("Tile title override (empty to clear)", t.title_override ?? ""); if (v !== null) m.updateTile.mutate({ tid: t.id, title_override: v || null }); }}>Override title</Button>
                {otherGroups.length > 0 && (
                  <select onChange={(e) => e.target.value && m.updateTile.mutate({ tid: t.id, group_id: e.target.value })} defaultValue="">
                    <option value="">Move to group…</option>
                    {otherGroups.map((g) => <option key={g.id} value={g.id}>{g.title}</option>)}
                  </select>
                )}
              </div>
            )}
          </div>
        </Modal>
      )}
    </div>
  );
}

function AddTileDialog({ group, m, onClose }: { group: Group; m: ReturnType<typeof useDashboardMutations>; onClose: () => void }) {
  const [search, setSearch] = useState("");
  const charts = useCharts(search);
  return (
    <Modal title={`Add a chart to “${group.title}”`} onClose={onClose}>
      <div className="stack">
        <input placeholder="Search your chart library…" value={search} onChange={(e) => setSearch(e.target.value)} />
        {charts.isLoading && <Spinner />}
        {charts.data?.items.filter((c) => !c.is_archived).map((c) => (
          <div key={c.id} className="row spread" style={{ borderBottom: "1px solid var(--border)", padding: "6px 0" }}>
            <div><strong>{c.title}</strong><div className="muted small">{c.question}</div></div>
            <Button size="sm" variant="primary" disabled={m.addTile.isPending} onClick={() => m.addTile.mutate({ saved_chart_id: c.id, group_id: group.id }, { onSuccess: onClose })}>Add</Button>
          </div>
        ))}
        {charts.data?.items.length === 0 && <p className="muted">Your library is empty — pin a chart from a conversation first.</p>}
        <ErrorBox error={m.addTile.error} />
      </div>
    </Modal>
  );
}

function ShareDialog({ d, m, onClose }: { d: Dashboard; m: ReturnType<typeof useDashboardMutations>; onClose: () => void }) {
  const grants = useGrants(d.id);
  const users = useUsers();
  const [rows, setRows] = useState<{ principal_id: string; role: DashboardRole }[]>([]);
  useEffect(() => { if (grants.data) setRows(grants.data.map((g) => ({ principal_id: g.principal_id, role: g.role }))); }, [grants.data]);
  const [visibility, setVisibility] = useState(d.visibility);
  const available = users.data?.filter((u) => !rows.some((r) => r.principal_id === u.id)) ?? [];
  const owners = rows.filter((r) => r.role === "owner").length;
  return (
    <Modal title="Share dashboard" onClose={onClose}>
      <div className="stack">
        <p className="muted small">Effective access is <code>min(global role, grant)</code>: a user whose global role is <em>viewer</em> can never edit, whatever the grant. Each viewer sees tile data under <em>their own</em> scope.</p>
        <div><label>Visibility</label>
          <select value={visibility} onChange={(e) => setVisibility(e.target.value as Dashboard["visibility"])}>
            <option value="private">private — explicit grants only</option>
            <option value="shared">shared — explicit grants</option>
            <option value="tenant">tenant — everyone in the tenant may view</option>
          </select>
        </div>
        {rows.map((r) => {
          const u = users.data?.find((x) => x.id === r.principal_id);
          return (
            <div key={r.principal_id} className="row spread">
              <span>{u?.email ?? r.principal_id} <span className="muted small">({u?.role})</span></span>
              <div className="row">
                <select value={r.role} onChange={(e) => setRows(rows.map((x) => x.principal_id === r.principal_id ? { ...x, role: e.target.value as DashboardRole } : x))}>
                  <option value="viewer">viewer</option><option value="editor">editor</option><option value="owner">owner</option>
                </select>
                <Button size="sm" variant="danger" onClick={() => setRows(rows.filter((x) => x.principal_id !== r.principal_id))}>remove</Button>
              </div>
            </div>
          );
        })}
        {available.length > 0 && (
          <select defaultValue="" onChange={(e) => { if (e.target.value) setRows([...rows, { principal_id: e.target.value, role: "viewer" }]); e.target.value = ""; }}>
            <option value="">Add a person…</option>
            {available.map((u) => <option key={u.id} value={u.id}>{u.email} ({u.role})</option>)}
          </select>
        )}
        {owners !== 1 && <div className="error-box">A dashboard must have exactly one owner.</div>}
        <ErrorBox error={m.putGrants.error ?? m.update.error} />
        <div className="row spread">
          <span className="muted small">Ownership transfers with the owner grant.</span>
          <Button variant="primary" disabled={owners !== 1 || m.putGrants.isPending} onClick={async () => {
            if (visibility !== d.visibility) await m.update.mutateAsync({ visibility });
            m.putGrants.mutate(rows, { onSuccess: onClose });
          }}>Save</Button>
        </div>
      </div>
    </Modal>
  );
}

function GroupingDialog({ d, proposal, m, onClose }: { d: Dashboard; proposal: GroupingProposal; m: ReturnType<typeof useDashboardMutations>; onClose: () => void }) {
  const tileCount = d.groups.reduce((n, g) => n + g.tiles.length, 0);
  return (
    <Modal title="Suggested grouping" onClose={onClose} wide>
      <div className="stack">
        <p className="muted">{proposal.rationale}</p>
        <p className="small muted">The model saw tile titles and questions only. Its proposal was validated against the schema and checked for completeness ({proposal.diff.length}/{tileCount} tiles assigned exactly once). Nothing moves until you accept.</p>
        {proposal.groups.map((g) => (
          <div key={g.title} className="panel">
            <h3>{g.title} <span className="muted small">· {g.tile_ids.length}</span></h3>
            {proposal.diff.filter((r) => r.to === g.title).map((r) => (
              <div key={r.tile_id} className={`diff-row ${r.changed ? "changed" : ""}`}>
                <span>{r.title}</span><span className="muted">{r.changed ? `${r.from} →` : "stays in"}</span><span>{r.to}</span>
              </div>
            ))}
          </div>
        ))}
        <ErrorBox error={m.applyGrouping.error} />
        <div className="row spread">
          <Button onClick={onClose}>Reject</Button>
          <Button variant="primary" disabled={m.applyGrouping.isPending} onClick={() => m.applyGrouping.mutate(proposal, { onSuccess: onClose })}>Accept and apply</Button>
        </div>
      </div>
    </Modal>
  );
}
