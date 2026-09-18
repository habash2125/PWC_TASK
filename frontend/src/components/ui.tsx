import { useEffect, useState, type ReactNode } from "react";
import { ApiError } from "@/api/client";

export function Button({ children, variant = "default", size = "md", ...rest }:
  React.ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "default" | "primary" | "danger" | "ghost"; size?: "sm" | "md" }) {
  return (
    <button className={`btn btn-${variant} btn-${size}`} {...rest}>
      {children}
    </button>
  );
}

export function Badge({ children, tone = "neutral" }: { children: ReactNode; tone?: "neutral" | "ok" | "warn" | "danger" | "info" }) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}

export function Spinner({ label }: { label?: string }) {
  return (
    <span className="spinner-wrap">
      <span className="spinner" /> {label && <span className="muted">{label}</span>}
    </span>
  );
}

export function ErrorBox({ error }: { error: unknown }) {
  if (!error) return null;
  const e = error as ApiError;
  const detail = e instanceof ApiError ? e.problem?.detail ?? e.message : (e as Error)?.message ?? String(error);
  const rid = e instanceof ApiError ? e.problem?.request_id : undefined;
  return (
    <div className="error-box">
      <strong>{e instanceof ApiError ? e.problem?.title ?? "Error" : "Error"}</strong> — {detail}
      {rid && <span className="muted"> · request {rid}</span>}
    </div>
  );
}

export function Modal({ title, onClose, children, wide }: { title: string; onClose: () => void; children: ReactNode; wide?: boolean }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <div className={`modal ${wide ? "modal-wide" : ""}`} onMouseDown={(e) => e.stopPropagation()} role="dialog" aria-label={title}>
        <div className="modal-head">
          <h3>{title}</h3>
          <button className="btn btn-ghost btn-sm" onClick={onClose} aria-label="Close">✕</button>
        </div>
        <div className="modal-body">{children}</div>
      </div>
    </div>
  );
}

export function Collapsible({ title, children, defaultOpen = false }: { title: ReactNode; children: ReactNode; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="collapsible">
      <button className="collapsible-head" onClick={() => setOpen((o) => !o)}>
        <span className="chev">{open ? "▾" : "▸"}</span> {title}
      </button>
      {open && <div className="collapsible-body">{children}</div>}
    </div>
  );
}

export function fmtMs(ms: number | null | undefined) {
  if (ms == null) return "–";
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${ms} ms`;
}

export function fmtUsd(v: number | null | undefined) {
  if (v == null) return "–";
  return `$${v.toFixed(4)}`;
}

export function DataTable({ columns, rows, max = 50 }: { columns: string[]; rows: unknown[][]; max?: number }) {
  if (!columns.length) return <p className="muted">No columns.</p>;
  return (
    <div className="table-wrap">
      <table>
        <thead><tr>{columns.map((c) => <th key={c}>{c}</th>)}</tr></thead>
        <tbody>
          {rows.slice(0, max).map((r, i) => (
            <tr key={i}>{r.map((v, j) => <td key={j}>{v == null ? "" : typeof v === "number" ? v.toLocaleString() : String(v)}</td>)}</tr>
          ))}
        </tbody>
      </table>
      {rows.length > max && <p className="muted">… {rows.length - max} more rows</p>}
    </div>
  );
}
