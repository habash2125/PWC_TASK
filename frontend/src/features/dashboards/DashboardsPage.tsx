import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/api/client";
import { useDashboards } from "@/api/hooks";
import { useAuth } from "@/auth/AuthContext";
import { Badge, Button, ErrorBox, Spinner } from "@/components/ui";
import type { Dashboard } from "@/types/api";

export function DashboardsPage() {
  const { user } = useAuth();
  const list = useDashboards();
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const create = useMutation({
    mutationFn: (n: string) => api<Dashboard>("/dashboards", { method: "POST", body: { name: n } }),
    onSuccess: () => { setName(""); qc.invalidateQueries({ queryKey: ["dashboards"] }); },
  });
  const submit = (e: FormEvent) => { e.preventDefault(); if (name.trim()) create.mutate(name.trim()); };
  return (
    <div>
      <div className="page-head">
        <div><h1>Dashboards</h1><p className="muted">Owned and shared with you. Refreshing a dashboard re-runs its SQL under <em>your</em> data scope — zero model calls.</p></div>
        {user?.role !== "viewer" && (
          <form className="row" onSubmit={submit}>
            <input placeholder="New dashboard name" value={name} onChange={(e) => setName(e.target.value)} style={{ width: 240 }} maxLength={120} />
            <Button variant="primary" type="submit" disabled={create.isPending || !name.trim()}>Create</Button>
          </form>
        )}
      </div>
      <ErrorBox error={create.error} />
      {list.isLoading && <Spinner />}
      <div className="cards">
        {list.data?.map((d) => (
          <Link className="card" key={d.id} to={`/dashboards/${d.id}`} style={{ color: "inherit" }}>
            <div className="row spread"><h3>{d.name}</h3><Badge tone={d.effective_role === "owner" ? "info" : "neutral"}>{d.effective_role}</Badge></div>
            {d.description && <p className="muted small">{d.description}</p>}
            <div className="row small muted"><span>{d.tile_count} tile{d.tile_count === 1 ? "" : "s"}</span><span>· {d.visibility}</span><span>· updated {new Date(d.updated_at).toLocaleDateString()}</span></div>
          </Link>
        ))}
        {list.data?.length === 0 && <p className="muted">No dashboards yet.</p>}
      </div>
    </div>
  );
}
