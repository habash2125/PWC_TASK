import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import ReactMarkdown from "react-markdown";
import { Link } from "react-router-dom";
import { useAsk, useCreateSession, useFeedback, usePinChart, useSessions, useSuggestions, useTurns } from "@/api/hooks";
import { PlotlyChart } from "@/components/PlotlyChart";
import { Badge, Button, Collapsible, ErrorBox, Spinner, fmtMs, fmtUsd } from "@/components/ui";
import type { Turn, TurnChart } from "@/types/api";

export function ChatPage() {
  const sessions = useSessions();
  const suggestions = useSuggestions();
  const create = useCreateSession();
  const [sessionId, setSessionId] = useState<string | null>(null);
  const turns = useTurns(sessionId);
  const ask = useAsk();
  const [message, setMessage] = useState("");
  const bottom = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!sessionId && sessions.data?.length) setSessionId(sessions.data[0].id);
  }, [sessions.data, sessionId]);
  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns.data?.length, ask.isPending]);

  const submit = async (e?: FormEvent, text?: string) => {
    e?.preventDefault();
    const q = (text ?? message).trim();
    if (!q || ask.isPending) return;
    let sid = sessionId;
    if (!sid) {
      const s = await create.mutateAsync(undefined);
      sid = s.id;
      setSessionId(sid);
    }
    setMessage("");
    ask.mutate({ sessionId: sid, message: q });
  };

  return (
    <div className="chat">
      <aside className="panel chat-sessions stack">
        <div className="row spread">
          <strong>Sessions</strong>
          <Button size="sm" onClick={() => create.mutate(undefined, { onSuccess: (s) => setSessionId(s.id) })}>+ New</Button>
        </div>
        {sessions.isLoading && <Spinner />}
        {sessions.data?.map((s) => (
          <div key={s.id} className={`item ${s.id === sessionId ? "active" : ""}`} onClick={() => setSessionId(s.id)} title={s.title ?? ""}>
            {s.title ?? "New session"}
          </div>
        ))}
        {sessions.data?.length === 0 && <p className="muted small">No sessions yet. Ask your first question.</p>}
      </aside>
      <section className="chat-main">
        <div className="chat-turns">
          {turns.isLoading && <Spinner label="Loading conversation…" />}
          {turns.data?.length === 0 && !ask.isPending && (
            <div className="panel">
              <h2>{suggestions.data?.headline ?? "Ask a question about your data"}</h2>
              <p className="muted">Answers come back as a short narrative plus interactive charts. Pin any chart to a dashboard; refreshing it later re-runs the SQL with zero model calls.</p>
              <div className="suggestions">{(suggestions.data?.questions ?? []).map((s) => <Button key={s} size="sm" onClick={() => void submit(undefined, s)}>{s}</Button>)}</div>
            </div>
          )}
          {turns.data?.map((t) => <TurnView key={t.id} turn={t} />)}
          {ask.isPending && (
            <div className="turn">
              <div className="bubble-q">{ask.variables?.message}</div>
              <div className="bubble-a"><Spinner label="Screening, generating SQL, executing, charting…" /></div>
            </div>
          )}
          {ask.isError && <ErrorBox error={ask.error} />}
          <div ref={bottom} />
        </div>
        <form className="ask" onSubmit={submit}>
          <textarea value={message} onChange={(e) => setMessage(e.target.value)} placeholder="e.g. Which products need reordering in the North Regional Depot?"
            onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void submit(); } }} maxLength={4000} />
          <Button variant="primary" type="submit" disabled={ask.isPending || !message.trim()}>Ask</Button>
        </form>
      </section>
    </div>
  );
}

function TurnView({ turn }: { turn: Turn }) {
  const blocks = useMemo(() => splitAnchors(turn.answer_markdown ?? "", turn.charts), [turn]);
  const tone = turn.status === "ok" ? "ok" : turn.status === "blocked" ? "danger" : "warn";
  return (
    <div className="turn">
      <div className="bubble-q">{turn.question}</div>
      <div className={`bubble-a ${turn.status === "blocked" ? "blocked" : ""}`}>
        {blocks.map((b, i) =>
          b.kind === "md" ? <div className="md" key={i}><ReactMarkdown>{b.text}</ReactMarkdown></div> : <ChartBlock key={i} chart={b.chart} />,
        )}
        {turn.guard_events.length > 0 && (
          <Collapsible title={<span>Guard events <Badge tone="warn">{turn.guard_events.length}</Badge></span>}>
            {turn.guard_events.map((e, i) => (
              <div key={i} className="small" style={{ marginBottom: 6 }}>
                <Badge tone={e.verdict === "blocked" ? "danger" : e.verdict === "repaired" ? "warn" : "ok"}>{e.kind} · {e.verdict}</Badge> {e.reason}
                {e.shadow_parser_verdict && <div className="muted">shadow parser: {e.shadow_parser_verdict}</div>}
              </div>
            ))}
          </Collapsible>
        )}
        {turn.sql_text && turn.charts.length === 0 && (
          <Collapsible title="SQL"><pre>{turn.sql_text}</pre></Collapsible>
        )}
        <div className="turn-meta" style={{ marginTop: 8 }}>
          <Badge tone={tone}>{turn.status}{turn.error_code ? ` · ${turn.error_code}` : ""}</Badge>
          <span>{fmtMs(turn.duration_ms)}</span>
          {turn.llm_calls != null && <span>{turn.llm_calls} model calls</span>}
          <span>{turn.input_tokens ?? 0}/{turn.output_tokens ?? 0} tokens</span>
          <span>{fmtUsd(turn.cost_usd)}</span>
          {turn.model && <span>{turn.model}</span>}
          {turn.prompt_version_id && <span title="prompt version">{turn.prompt_version_id}</span>}
          <Link to={`/traces/${turn.trace_id}`}>trace</Link>
          <FeedbackButtons turnId={turn.id} />
        </div>
      </div>
    </div>
  );
}

function ChartBlock({ chart }: { chart: TurnChart }) {
  const pin = usePinChart();
  return (
    <div className="chart-block">
      <div className="chart-head">
        <strong>{chart.title}</strong>
        <div className="row">
          {pin.isSuccess ? <Badge tone="ok">pinned to library</Badge> : (
            <Button size="sm" variant="primary" disabled={pin.isPending} onClick={() => pin.mutate({ turn_chart_id: chart.id })}>
              {pin.isPending ? "Pinning…" : "Pin chart"}
            </Button>
          )}
        </div>
      </div>
      <PlotlyChart spec={chart.chart_spec} height={340} />
      <Collapsible title={<span>SQL <span className="muted small">· {chart.sql_hash.slice(0, 10)}</span></span>}>
        <pre>{chart.sql_text}</pre>
        <p className="muted small" style={{ marginTop: 6 }}>The <code>:lens_scope_*</code> placeholder is bound to the caller's allowed values at execution time.</p>
      </Collapsible>
      {pin.isError && <ErrorBox error={pin.error} />}
    </div>
  );
}

function FeedbackButtons({ turnId }: { turnId: string }) {
  const fb = useFeedback();
  const [sent, setSent] = useState<1 | -1 | null>(null);
  const send = (rating: 1 | -1) => fb.mutate({ turnId, rating }, { onSuccess: () => setSent(rating) });
  return (
    <span className="row" style={{ gap: 2 }}>
      <button className="btn btn-ghost btn-sm" onClick={() => send(1)} disabled={sent !== null} title="Helpful">{sent === 1 ? "👍" : "👍🏻"}</button>
      <button className="btn btn-ghost btn-sm" onClick={() => send(-1)} disabled={sent !== null} title="Wrong or unhelpful">{sent === -1 ? "👎" : "👎🏻"}</button>
    </span>
  );
}

type Block = { kind: "md"; text: string } | { kind: "chart"; chart: TurnChart };

function splitAnchors(markdown: string, charts: TurnChart[]): Block[] {
  const out: Block[] = [];
  const re = /<chart\s+(\d+)\s*>/gi;
  let last = 0;
  let m: RegExpExecArray | null;
  const used = new Set<number>();
  while ((m = re.exec(markdown))) {
    const text = markdown.slice(last, m.index).trim();
    if (text) out.push({ kind: "md", text });
    const n = Number(m[1]);
    const chart = charts[n - 1];
    if (chart && !used.has(n)) {
      out.push({ kind: "chart", chart });
      used.add(n);
    }
    last = m.index + m[0].length;
  }
  const tail = markdown.slice(last).trim();
  if (tail) out.push({ kind: "md", text: tail });
  charts.forEach((c, i) => { if (!used.has(i + 1)) out.push({ kind: "chart", chart: c }); });
  return out;
}
