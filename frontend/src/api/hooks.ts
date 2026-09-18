import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, idempotencyKey } from "./client";
import type {
  Chart, ChartSummary, ChartUsage, Dashboard, DashboardRefresh, DashboardRole, DashboardSummary, Grant, GroupingProposal,
  Session, TenantUser, Tile, TileRefresh, Trace, Turn, UsageRow,
} from "@/types/api";

// ── chat ─────────────────────────────────────────────────────────────────────
export const useSessions = () => useQuery({ queryKey: ["sessions"], queryFn: () => api<Session[]>("/sessions") });
export const useTurns = (sessionId: string | null) =>
  useQuery({ queryKey: ["turns", sessionId], queryFn: () => api<Turn[]>(`/sessions/${sessionId}/turns`), enabled: !!sessionId });

export function useCreateSession() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (title?: string) => api<Session>("/sessions", { method: "POST", body: title ? { title } : {} }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["sessions"] }),
  });
}

export function useAsk() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { sessionId: string; message: string }) =>
      api<Turn>("/chat", { method: "POST", body: { session_id: v.sessionId, message: v.message } }),
    onSuccess: (turn, v) => {
      qc.setQueryData<Turn[]>(["turns", v.sessionId], (old) => [...(old ?? []), turn]);
      qc.invalidateQueries({ queryKey: ["sessions"] });
      qc.invalidateQueries({ queryKey: ["usage"] });
    },
  });
}

export const useFeedback = () =>
  useMutation({ mutationFn: (v: { turnId: string; rating: 1 | -1; comment?: string }) =>
    api(`/turns/${v.turnId}/feedback`, { method: "POST", body: { rating: v.rating, comment: v.comment } }) });

// ── charts ───────────────────────────────────────────────────────────────────
export const useCharts = (search = "") =>
  useQuery({ queryKey: ["charts", search], queryFn: () => api<{ items: ChartSummary[]; total: number }>(`/charts?limit=200${search ? `&search=${encodeURIComponent(search)}` : ""}`) });
export const useChart = (id: string | null) => useQuery({ queryKey: ["chart", id], queryFn: () => api<Chart>(`/charts/${id}`), enabled: !!id });
export const useChartUsage = (id: string | null) =>
  useQuery({ queryKey: ["chart-usage", id], queryFn: () => api<ChartUsage>(`/charts/${id}/usage`), enabled: !!id });

export function usePinChart() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { turn_chart_id: string; title?: string }) =>
      api<Chart>("/charts", { method: "POST", body: v, headers: { "Idempotency-Key": idempotencyKey() } }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["charts"] }),
  });
}

export function useDeleteChart() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api<void>(`/charts/${id}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["charts"] }),
  });
}

export function useUpdateChart() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { id: string; title?: string; description?: string; is_archived?: boolean }) =>
      api<Chart>(`/charts/${v.id}`, { method: "PATCH", body: { title: v.title, description: v.description, is_archived: v.is_archived } }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["charts"] }),
  });
}

// ── dashboards ───────────────────────────────────────────────────────────────
export const useDashboards = () => useQuery({ queryKey: ["dashboards"], queryFn: () => api<DashboardSummary[]>("/dashboards") });
export const useDashboard = (id: string | null) =>
  useQuery({ queryKey: ["dashboard", id], queryFn: () => api<Dashboard>(`/dashboards/${id}`), enabled: !!id });

export function useDashboardMutations(id: string) {
  const qc = useQueryClient();
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["dashboard", id] });
    qc.invalidateQueries({ queryKey: ["dashboards"] });
    qc.invalidateQueries({ queryKey: ["charts"] });
  };
  return {
    create: useMutation({ mutationFn: (v: { name: string; description?: string }) => api<Dashboard>("/dashboards", { method: "POST", body: v }), onSuccess: invalidate }),
    update: useMutation({ mutationFn: (v: { name?: string; description?: string; visibility?: string }) => api<Dashboard>(`/dashboards/${id}`, { method: "PATCH", body: v }), onSuccess: invalidate }),
    remove: useMutation({ mutationFn: () => api<void>(`/dashboards/${id}`, { method: "DELETE" }), onSuccess: invalidate }),
    addGroup: useMutation({ mutationFn: (title: string) => api(`/dashboards/${id}/groups`, { method: "POST", body: { title } }), onSuccess: invalidate }),
    updateGroup: useMutation({ mutationFn: (v: { gid: string; title?: string; position?: number; is_collapsed?: boolean }) =>
      api(`/dashboards/${id}/groups/${v.gid}`, { method: "PATCH", body: { title: v.title, position: v.position, is_collapsed: v.is_collapsed } }), onSuccess: invalidate }),
    deleteGroup: useMutation({ mutationFn: (gid: string) => api(`/dashboards/${id}/groups/${gid}`, { method: "DELETE" }), onSuccess: invalidate }),
    addTile: useMutation({ mutationFn: (v: { saved_chart_id: string; group_id?: string; title_override?: string }) =>
      api<Tile>(`/dashboards/${id}/tiles`, { method: "POST", body: v }), onSuccess: invalidate }),
    updateTile: useMutation({ mutationFn: (v: { tid: string; group_id?: string; title_override?: string | null; w?: number; h?: number }) =>
      api<Tile>(`/dashboards/${id}/tiles/${v.tid}`, { method: "PATCH", body: { group_id: v.group_id, title_override: v.title_override, w: v.w, h: v.h } }), onSuccess: invalidate }),
    deleteTile: useMutation({ mutationFn: (tid: string) => api<void>(`/dashboards/${id}/tiles/${tid}`, { method: "DELETE" }), onSuccess: invalidate }),
    layout: useMutation({ mutationFn: (items: { tile_id: string; group_id?: string; x?: number; y?: number; w?: number; h?: number }[]) =>
      api<Dashboard>(`/dashboards/${id}/layout`, { method: "PUT", body: { items } }), onSuccess: (d) => qc.setQueryData(["dashboard", id], d) }),
    refreshTile: useMutation({ mutationFn: (tid: string) =>
      api<TileRefresh>(`/dashboards/${id}/tiles/${tid}/refresh`, { method: "POST", headers: { "Idempotency-Key": idempotencyKey() } }) }),
    refreshAll: useMutation({ mutationFn: () => api<DashboardRefresh>(`/dashboards/${id}/refresh`, { method: "POST" }) }),
    putGrants: useMutation({ mutationFn: (grants: { principal_id: string; role: DashboardRole }[]) =>
      api<Grant[]>(`/dashboards/${id}/grants`, { method: "PUT", body: { grants } }), onSuccess: () => { qc.invalidateQueries({ queryKey: ["grants", id] }); invalidate(); } }),
    suggest: useMutation({ mutationFn: () => api<GroupingProposal>(`/dashboards/${id}/suggest-groups`, { method: "POST" }) }),
    applyGrouping: useMutation({ mutationFn: (p: GroupingProposal) =>
      api(`/dashboards/${id}/apply-grouping`, { method: "POST", body: { proposal_id: p.proposal_id, groups: p.groups } }), onSuccess: invalidate }),
  };
}

export const useGrants = (id: string | null) => useQuery({ queryKey: ["grants", id], queryFn: () => api<Grant[]>(`/dashboards/${id}/grants`), enabled: !!id });
export const useUsers = () => useQuery({ queryKey: ["users"], queryFn: () => api<TenantUser[]>("/users") });

// ── ops ──────────────────────────────────────────────────────────────────────
export const useTrace = (traceId: string | null) =>
  useQuery({ queryKey: ["trace", traceId], queryFn: () => api<Trace>(`/traces/${traceId}`), enabled: !!traceId, retry: 2, retryDelay: 800 });
export const useUsage = () => useQuery({ queryKey: ["usage"], queryFn: () => api<UsageRow[]>("/usage?days=30") });
