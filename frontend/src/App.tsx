import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Navigate, NavLink, Outlet, Route, Routes } from "react-router-dom";
import { AuthProvider, useAuth } from "@/auth/AuthContext";
import { LoginPage } from "@/features/auth/LoginPage";
import { ChatPage } from "@/features/chat/ChatPage";
import { ChartsPage } from "@/features/charts/ChartsPage";
import { DashboardsPage } from "@/features/dashboards/DashboardsPage";
import { DashboardPage } from "@/features/dashboards/DashboardPage";
import { TracePage } from "@/features/traces/TracePage";
import { UsagePage } from "@/features/admin/UsagePage";
import { Spinner } from "@/components/ui";

const qc = new QueryClient({ defaultOptions: { queries: { retry: 1, staleTime: 5_000, refetchOnWindowFocus: false } } });

function Shell() {
  const { user, loading, logout } = useAuth();
  if (loading) return <div className="login"><Spinner label="Signing you in…" /></div>;
  if (!user) return <Navigate to="/login" replace />;
  const canAsk = user.role !== "viewer";
  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand"><span className="dot" /> Lens</div>
        <nav className="nav">
          {canAsk && <NavLink to="/chat">Ask</NavLink>}
          <NavLink to="/dashboards">Dashboards</NavLink>
          {canAsk && <NavLink to="/charts">Chart library</NavLink>}
          <NavLink to="/traces">Traces</NavLink>
          <NavLink to="/usage">Usage &amp; cost</NavLink>
        </nav>
        <div className="user">
          <div>{user.full_name ?? user.email}</div>
          <div>{user.email} · <strong>{user.role}</strong></div>
          <div className="small">scope: {user.scopes.map((s) => `${s.scope_key} ∈ {${s.scope_values.join(", ")}}`).join("; ") || "none"}</div>
          <button className="btn btn-ghost btn-sm" style={{ color: "#cbd5e1", marginTop: 6 }} onClick={() => void logout()}>Sign out</button>
        </div>
      </aside>
      <main className="main">
        <Outlet />
      </main>
    </div>
  );
}

export default function App() {
  return (
    <QueryClientProvider client={qc}>
      <AuthProvider>
        <BrowserRouter>
          <Routes>
            <Route path="/login" element={<LoginPage />} />
            <Route element={<Shell />}>
              <Route index element={<Navigate to="/dashboards" replace />} />
              <Route path="/chat" element={<ChatPage />} />
              <Route path="/charts" element={<ChartsPage />} />
              <Route path="/dashboards" element={<DashboardsPage />} />
              <Route path="/dashboards/:id" element={<DashboardPage />} />
              <Route path="/traces" element={<TracePage />} />
              <Route path="/traces/:traceId" element={<TracePage />} />
              <Route path="/usage" element={<UsagePage />} />
            </Route>
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </BrowserRouter>
      </AuthProvider>
    </QueryClientProvider>
  );
}
