import { useState, type FormEvent } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useTrace } from "@/api/hooks";
import { Badge, Button, ErrorBox, Spinner, fmtMs, fmtUsd } from "@/components/ui";
import type { Span } from "@/types/api";

export function TracePage() {
  const { traceId = "" } = useParams();
  const nav = useNavigate();
  const [input, setInput] = useState(traceId);
  const trace = useTrace(traceId || null);
  const submit = (e: FormEvent) => { e.preventDefault(); if (input.trim()) nav(`/traces/${input.trim()}`); };
  const t = trace.data;
  const t0 = t ? Math.min(...flatten(t.roots).map((s) => s.start_ts ? Date.parse(s.start_ts) : Infinity)) : 0;
  const total = t?.total_duration_ms || 1;
  return (
    <div>
      <div className="page-head">
        <div><h1>Trace viewer</h1><p className="muted">The real parent/child runtime tree of a turn or a tile refresh — nesting from parent ids, order from timestamps.</p></div>
        <form className="row" onSubmit={submit}><input value={input} onChange={(e) => setInput(e.target.value)} placeholder="trace id" style={{ width: 340 }} /><Button type="submit">Open</Button></form>
      </div>
      {trace.isLoading && <Spinner label="Loading spans…" />}
      <ErrorBox error={trace.error} />
      {t && (
        <div className="stack">
          <div className="panel">
            <div className="row">
              <Badge tone={t.llm_calls === 0 ? "ok" : "info"}>{t.llm_calls} model call{t.llm_calls === 1 ? "" : "s"}</Badge>
              <Badge>{t.span_count} spans</Badge>
              <Badge>{fmtMs(t.total_duration_ms)}</Badge>
              {t.turn && <Badge tone={t.turn.status === "ok" ? "ok" : "danger"}>{t.turn.status}{t.turn.error_code ? ` · ${t.turn.error_code}` : ""}</Badge>}
            </div>
            {t.turn && (
              <dl className="kv" style={{ marginTop: 10 }}>
                <dt>question</dt><dd>{t.turn.question}</dd>
                <dt>model</dt><dd>{t.turn.model ?? "–"}</dd>
                <dt>prompt</dt><dd>{t.turn.prompt_version_id ?? "–"}</dd>
                <dt>tokens</dt><dd>{t.turn.input_tokens ?? 0} in / {t.turn.output_tokens ?? 0} out · {fmtUsd(t.turn.cost_usd)}</dd>
                <dt>stages</dt><dd>{Object.entries(t.turn.stage_timings ?? {}).map(([k, v]) => `${k} ${fmtMs(v)}`).join(" · ")}</dd>
              </dl>
            )}
          </div>
          {t.guard_events.length > 0 && (
            <div className="panel">
              <h3>Guard events</h3>
              {t.guard_events.map((e, i) => (
                <div key={i} style={{ marginBottom: 8 }}>
                  <Badge tone={e.verdict === "blocked" ? "danger" : e.verdict === "repaired" ? "warn" : "ok"}>{e.kind} · {e.verdict}</Badge> <span className="small">{e.reason}</span>
                  {e.shadow_parser_verdict && <div className="muted small">shadow parser: {e.shadow_parser_verdict}</div>}
                  {e.offending_sql && <pre style={{ marginTop: 4 }}>{e.offending_sql}</pre>}
                </div>
              ))}
            </div>
          )}
          <div className="panel">
            <h3>Spans</h3>
            {t.roots.map((s) => <SpanNode key={s.span_id} s={s} t0={t0} total={total} depth={0} />)}
          </div>
        </div>
      )}
    </div>
  );
}

function flatten(spans: Span[]): Span[] { return spans.flatMap((s) => [s, ...flatten(s.children)]); }

function SpanNode({ s, t0, total, depth }: { s: Span; t0: number; total: number; depth: number }) {
  const [open, setOpen] = useState(depth < 2);
  const start = s.start_ts ? Date.parse(s.start_ts) - t0 : 0;
  const left = Math.max(0, Math.min(100, (start / total) * 100));
  const width = Math.max(0.5, Math.min(100 - left, ((s.duration_ms ?? 0) / total) * 100));
  const attrs = Object.entries(s.attributes).filter(([k]) => k !== "events");
  return (
    <div className="span" style={{ marginLeft: depth * 12 }}>
      <div className="span-head" onClick={() => setOpen((o) => !o)}>
        <span className="chev">{s.children.length ? (open ? "▾" : "▸") : "·"}</span>
        <strong style={{ minWidth: 160 }}>{s.name}</strong>
        <span className="muted small" style={{ minWidth: 60 }}>{fmtMs(s.duration_ms)}</span>
        <div style={{ flex: 1, position: "relative", height: 6 }}><div className="span-bar" style={{ position: "absolute", left: `${left}%`, width: `${width}%` }} /></div>
      </div>
      {open && attrs.length > 0 && <div className="span-attrs">{attrs.map(([k, v]) => <span key={k} style={{ marginRight: 10 }}>{k}=<code>{String(v).slice(0, 120)}</code></span>)}</div>}
      {open && s.children.map((c) => <SpanNode key={c.span_id} s={c} t0={t0} total={total} depth={depth + 1} />)}
    </div>
  );
}
