import { useState, type FormEvent } from "react";
import { Link, Navigate, useNavigate } from "react-router-dom";
import { useAuth } from "@/auth/AuthContext";
import { Button, ErrorBox } from "@/components/ui";

export function SignupPage() {
  const { user, signup } = useAuth();
  const nav = useNavigate();
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  if (user) return <Navigate to="/" replace />;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await signup(email, password, fullName || undefined);
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
        <p className="muted">Create an account to ask questions about your data in plain language.</p>
        <div><label>Full name</label><input value={fullName} onChange={(e) => setFullName(e.target.value)} autoComplete="name" /></div>
        <div><label>E-mail</label><input type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="username" required /></div>
        <div><label>Password</label><input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" minLength={12} required /></div>
        <ErrorBox error={error} />
        <Button variant="primary" type="submit" disabled={busy}>{busy ? "Creating account…" : "Create account"}</Button>
        <p className="muted small">
          New accounts start as viewers with no data access until an admin grants a scope. Already have an account? <Link to="/login">Sign in</Link>.
        </p>
      </form>
    </div>
  );
}
