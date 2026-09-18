import { useState, type FormEvent } from "react";
import { Navigate, useNavigate } from "react-router-dom";
import { useAuth } from "@/auth/AuthContext";
import { Button, ErrorBox } from "@/components/ui";

export function LoginPage() {
  const { user, login } = useAuth();
  const nav = useNavigate();
  const [email, setEmail] = useState("analyst@lens.demo");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  if (user) return <Navigate to="/" replace />;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(email, password);
      nav("/");
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login">
      <form className="panel stack" onSubmit={submit}>
        <div className="brand" style={{ color: "var(--text)" }}><span className="dot" /> Lens</div>
        <p className="muted">Ask questions about the delivery portfolio, keep the answers as dashboards.</p>
        <div><label>E-mail</label><input value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="username" /></div>
        <div><label>Password</label><input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" /></div>
        <ErrorBox error={error} />
        <Button variant="primary" type="submit" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</Button>
        <p className="muted small">Seeded users: admin@, analyst@, analyst2@, partner@lens.demo — passwords in README.</p>
      </form>
    </div>
  );
}
