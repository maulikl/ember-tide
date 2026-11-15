import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import Fold from "../components/Fold";
import Tip from "../components/Tip";
import { HelpLink } from "./Help";
import { useApp } from "../ctx";
import { useOwnPerson } from "../auth";
import { cToF, hm12 } from "../format";

interface Proposal { rhythm_id: string; phase_id?: string; field?: string; schedule?: "bedtime" | "wake"; from?: number | string; to: number | string }
interface Insight { id: string; kind?: string; title: string; detail: string; severity: string; evidence: string[]; proposal: Proposal | null; dismissed?: boolean; applied?: { ts: number; change: string; proposal?: Proposal } }
// a change the assistant proposed (or made, when small changes are allowed); summary is written by the server
interface Change { id: string; action: string; summary: string; status: "proposed" | "applied" | "undone" | "dismissed" | "failed" | "expired"; auto?: boolean; unprompted?: boolean; error?: string | null }
interface Msg { role: "me" | "ai"; text: string; changes?: Change[]; model?: string | null }
const SUGGESTED = ["Why was last night restless?", "Should my Deep phase be cooler?", "When do my hot flashes happen, and what would help?", "Compare this week with last week."];

export default function Insights() {
  const { snap, say } = useApp();
  const mine = useOwnPerson();
  const [pid, setPid] = useState<string | null>(null);
  const [items, setItems] = useState<Insight[] | null>(null);
  const [shown, setShown] = useState<Set<string>>(() => new Set());
  const [askOver, setAskOver] = useState<boolean | null>(null);
  const askTimer = useRef<number | undefined>(undefined);
  const [failed, setFailed] = useState(false);
  const [ai, setAi] = useState<{ configured: boolean; model: string | null; preset?: string; preset_name?: string; local?: boolean } | null>(null);
  const [q, setQ] = useState("");
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [busy, setBusy] = useState(false);
  const person = snap?.people.find(p => p.id === (pid ?? mine ?? snap.people[0]?.id));
  const load = () => person && api.get<Insight[]>(`/api/people/${person.id}/insights`).then(r => { setItems(r); setFailed(false); }).catch(() => setFailed(true));
  const personRef = useRef<string | undefined>(undefined);
  personRef.current = person?.id;                               // an answer that arrives after switching person is dropped
  useEffect(() => { clearTimeout(askTimer.current); setItems(null); setShown(new Set()); setAskOver(null); setMsgs([]); load(); }, [person?.id]);   // the chat and its change cards belong to one person
  useEffect(() => { api.get<{ configured: boolean; model: string | null; preset?: string; preset_name?: string; local?: boolean }>("/api/ai/status").then(setAi).catch(() => {}); return () => clearTimeout(askTimer.current); }, []);
  if (!snap || !person) return null;
  const u = person.settings.units;
  const showAsk = askOver ?? (person.settings.show_ask !== false);
  const fmtP = (p: Proposal) => {
    if (p.schedule) return `${p.schedule === "bedtime" ? "Bedtime" : "Wake"} ${hm12(String(p.from ?? "?"))} → ${hm12(String(p.to))}`;
    const ph = person.phases.find(x => x.id === p.phase_id)?.name ?? p.phase_id;
    const v = (x?: number | string) => x == null ? "?" : p.field === "temp_c" ? `${Math.round(u === "F" ? cToF(Number(x)) : Number(x))}°` : `${x} min`;
    return `${ph}: ${p.field === "temp_c" ? "temperature" : "transition"} ${v(p.from)} → ${v(p.to)}`;
  };
  const groups: [string, string, Insight[]][] = [
    ["From your nights", "What the bed, your check-ins and your changes say.", (items ?? []).filter(i => i.kind !== "guide")],
    ["Tuning guide", "How your nights really go (and your devices: light, phone, calendar), with small changes to match.", (items ?? []).filter(i => i.kind === "guide")],
  ];
  const apply = async (p: Proposal, insightId?: string) => {
    try { const r = await api.post<{ applied: string }>(`/api/people/${person.id}/insights/apply`, { proposal: p, insight_id: insightId }); say(`Applied: ${r.applied}`); load(); }
    catch (e) { say(`Couldn't apply: ${(e as Error).message}`); }
  };
  const dismiss = (i: Insight) => {
    setShown(s => { if (!s.has(i.id)) return s; const n = new Set(s); n.delete(i.id); return n; });
    setItems(cur => cur?.map(x => x.id === i.id ? { ...x, dismissed: true } : x) ?? cur);
    api.post(`/api/people/${person.id}/insights/dismiss`, { id: i.id, sig: i.proposal ? String(i.proposal.to) : i.title }).catch(() => {});
  };
  const reveal = (hidden: Insight[]) => setShown(s => { const n = new Set(s); hidden.forEach(h => n.add(h.id)); return n; });
  const setShowAsk = async (show: boolean) => {
    clearTimeout(askTimer.current);
    setAskOver(show);
    try {
      await api.patch(`/api/people/${person.id}/settings`, { show_ask: show });
      askTimer.current = window.setTimeout(() => setAskOver(null), 4000);
    } catch (e) {
      setAskOver(null);
      say(`Couldn't save: ${(e as Error).message}`);
    }
  };
  const ask = async (text: string) => {
    if (!text.trim()) return;
    if (busy) return;
    const asked = person.id;
    setMsgs(m => [...m, { role: "me", text }]); setQ(""); setBusy(true);
    try { const r = await api.post<{ answer: string; proposals: Change[]; applied: Change[]; model: string | null }>("/api/assistant/ask", { person_id: asked, question: text });
      if (personRef.current === asked) setMsgs(m => [...m, { role: "ai", text: r.answer, changes: [...r.applied, ...r.proposals], model: r.model }]); }
    catch (e) { if (personRef.current === asked) setMsgs(m => [...m, { role: "ai", text: `Sorry, that failed: ${(e as Error).message}` }]); }
    setBusy(false);
  };
  // Apply / Not now / Undo on a change card; the card shows the new state in place
  const setChange = (id: string, ch: Partial<Change>) => setMsgs(m => m.map(x => x.changes ? { ...x, changes: x.changes.map(c => c.id === id ? { ...c, ...ch } : c) } : x));
  const doChange = async (c: Change, what: "apply" | "dismiss" | "undo") => {
    const path = what === "undo" ? `/api/assistant/changes/${c.id}/undo` : `/api/assistant/proposals/${c.id}/${what}`;
    try { const r = await api.post<{ change: Change; message?: string }>(path, { person_id: person.id }); setChange(c.id, r.change); if (r.message) say(r.message); if (what !== "dismiss") load(); }
    catch (e) { say(`Couldn't: ${(e as Error).message}`); }
  };
  const proposed = msgs.flatMap(m => m.changes ?? []).filter(c => c.status === "proposed");
  const smallOn = !!(person.settings as unknown as { assistant_small_changes?: boolean }).assistant_small_changes;
  const setSmall = async (on: boolean) => {
    try { await api.patch(`/api/people/${person.id}/settings`, { assistant_small_changes: on }); say(on ? "The assistant may now make small changes for you" : "The assistant will ask first"); }
    catch (e) { say(`Couldn't save: ${(e as Error).message}`); }
  };
  const sevColor: Record<string, string> = { suggest: "var(--acc)", info: "var(--mute)", good: "var(--good)" };

  return (
    <>
      <div className="row" style={{ alignItems: "flex-end" }}>
        <div><div className="k">Analyze</div>
          <div style={{ display: "flex", alignItems: "baseline", gap: 12, flexWrap: "wrap" }}>
            <h1 style={{ fontSize: 32 }}>Insights &amp; Ask</h1>
            {!showAsk && <button type="button" className="link" style={{ fontSize: 12.5, color: "var(--faint)" }} onClick={() => setShowAsk(true)}>Show Ask</button>}
          </div>
          <p className="mute" style={{ fontSize: 13 }}>Patterns from recent nights, with changes you can apply in one click. Nothing changes until you do.</p></div>
        <div className="pills">{snap.people.map(p => <button key={p.id} className="pill" aria-pressed={p.id === person.id} onClick={() => { setPid(p.id); setMsgs([]); }}>{p.name}</button>)}</div>
      </div>
      <div className={showAsk ? "grid2 even" : undefined}>
        <div style={{ display: "grid", gap: 12, alignContent: "start" }}>
          {items === null && !failed && <div className="box"><span className="mute">Looking at {person.name}'s recent nights…</span></div>}
          {failed && <div className="box"><span className="mute">Couldn't load insights right now.</span><button className="pill" style={{ justifySelf: "start" }} onClick={() => load()}>Try again</button></div>}
          {groups.filter(([, , g]) => g.length).map(([title, blurb, g]) => {
            const vis = g.filter(i => !i.dismissed || shown.has(i.id));
            const hidden = g.filter(i => i.dismissed && !shown.has(i.id));
            return (
              <Fold key={title} id={`studio.insights.${title === "Tuning guide" ? "guide" : "nights"}`} title={title} defaultOpen keepSummary
                summary={`${vis.length} ${vis.length === 1 ? "idea" : "ideas"}${vis.some(i => i.proposal) ? ` · ${vis.filter(i => i.proposal).length} to apply` : ""}`}>
                <p className="faint" style={{ fontSize: 12, marginTop: -6 }}>{blurb} <HelpLink to="guide" /></p>
              {vis.map(i => (
                <div className="box" key={i.id} style={{ borderLeft: `3px solid ${sevColor[i.severity] ?? "var(--line2)"}` }}>
                  {i.proposal
                    ? <b style={{ fontWeight: 500, fontSize: 15 }}>{i.title}</b>
                    : <div className="row" style={{ alignItems: "flex-start" }}><b style={{ fontWeight: 500, fontSize: 15 }}>{i.title}</b><button type="button" className="pill" style={{ flex: "none", whiteSpace: "nowrap" }} onClick={() => dismiss(i)}>Not now</button></div>}
                  <span className="mute" style={{ fontSize: 13.5 }}>{i.detail}</span>
                  {i.evidence.length > 0 && <div className="chips">{i.evidence.map((e, k) => <span key={k} className="chip" style={{ cursor: "default" }}>{e}</span>)}</div>}
                  {i.proposal && <div className="row" style={{ marginTop: 4 }}><span style={{ fontSize: 13 }}>{fmtP(i.proposal)}</span>
                    <span className="pills"><button type="button" className="pill" onClick={() => dismiss(i)}>Not now</button><button className="btn pri" onClick={() => apply(i.proposal!, i.id)}>{i.proposal.schedule ? "Change it" : "Apply to rhythm"}</button></span></div>}
                  {i.applied && <div className="row" style={{ marginTop: 4 }}><span style={{ fontSize: 13, color: "var(--good)" }}>✓ Applied {new Date(i.applied.ts * 1000).toLocaleDateString([], { month: "short", day: "numeric", timeZone: snap.tz })} · {i.applied.proposal ? fmtP(i.applied.proposal) : i.applied.change}</span>
                    <span className="mute" style={{ fontSize: 12 }}>Watching how the next nights go</span></div>}
                </div>
              ))}
              {hidden.length > 0 && <div className="faint" style={{ fontSize: 12, paddingTop: 2 }}>{hidden.length} hidden · <button type="button" className="link" style={{ color: "inherit", fontSize: "inherit" }} onClick={() => reveal(hidden)}>Show</button></div>}
              </Fold>
            );
          })}
        </div>
        {showAsk && <div className="box" style={{ alignContent: "start", minHeight: 420 }}>
          <div className="row"><span className="k">Ask about your sleep</span>
            <span className="row" style={{ gap: 10 }}>
              <Tip text={ai?.configured ? (ai.local === false ? `Answers come from ${ai.preset_name ?? "the hosted AI provider"} connected in Studio → AI model. Night summaries sent for Ask go to that provider.` : `Answers come from ${ai.preset_name ?? "the AI model"} connected in Studio → AI model. Nothing leaves your network.`) : "No AI model is connected, so answers come from the built-in analysis. Connect one in Studio → AI model."}>
                <span className="chip" style={{ cursor: "default", color: ai?.configured ? "var(--good)" : "var(--mute)" }}>{ai?.configured ? `AI: ${ai.model}` : "Built-in analysis (no AI model connected)"}</span></Tip>
              <button type="button" className="link" style={{ fontSize: 12, color: "var(--faint)" }} onClick={() => setShowAsk(false)}>Hide</button>
            </span></div>
          <div style={{ display: "grid", gap: 10, maxHeight: 460, overflowY: "auto" }}>
            {msgs.length === 0 && <div className="chips">{SUGGESTED.map(s => <button key={s} className="chip" onClick={() => ask(s)}>{s}</button>)}</div>}
            {msgs.map((m, i) => (
              <div key={i} style={{ justifySelf: m.role === "me" ? "end" : "start", maxWidth: "92%", background: m.role === "me" ? "color-mix(in srgb,var(--acc) 16%,var(--raise))" : "var(--raise)", borderRadius: 14, padding: "10px 12px", fontSize: 14, whiteSpace: "pre-wrap" }}>
                {m.text}
                {(m.changes ?? []).map(c => (
                  <div key={c.id} className="row" style={{ marginTop: 8, borderTop: "1px solid var(--line)", paddingTop: 8, flexWrap: "wrap", gap: 8 }}>
                    <span style={{ fontSize: 13.5, flex: "1 1 180px" }}>
                      {c.status === "applied" ? "✓ " : c.status === "undone" ? "↩ " : ""}{c.summary}
                      {c.unprompted && c.status === "proposed" && <span className="faint" style={{ display: "block", fontSize: 12 }}>You didn't ask for this; it's only a suggestion.</span>}
                      {c.status === "applied" && <span className="faint" style={{ display: "block", fontSize: 12 }}>{c.auto ? "Done for you (a small change)" : "Done"}</span>}
                      {c.status === "undone" && <span className="faint" style={{ display: "block", fontSize: 12 }}>Undone</span>}
                      {c.status === "dismissed" && <span className="faint" style={{ display: "block", fontSize: 12 }}>Left as it is</span>}
                      {(c.status === "failed" || c.status === "expired") && <span className="faint" style={{ display: "block", fontSize: 12 }}>{c.error ?? "Too old now; ask again"}</span>}
                    </span>
                    <span className="pills">
                      {c.status === "proposed" && <><button className="pill" onClick={() => doChange(c, "dismiss")}>Not now</button><button className="btn pri" onClick={() => doChange(c, "apply")}>Apply</button></>}
                      {c.status === "applied" && <button className="pill" onClick={() => doChange(c, "undo")}>Undo</button>}
                    </span>
                  </div>))}
              </div>
            ))}
            {busy && <span className="mute">Thinking…</span>}
          </div>
          {proposed.length > 1 && <div className="row" style={{ borderTop: "1px solid var(--line)", paddingTop: 8 }}>
            <span style={{ fontSize: 13.5 }}>Apply these {proposed.length} changes?</span>
            <button className="btn pri" onClick={async () => { for (const c of proposed) await doChange(c, "apply"); }}>Apply all</button></div>}
          <div className="row"><input type="text" style={{ flex: 1 }} value={q} placeholder="Ask anything about your nights…" onChange={e => setQ(e.target.value)} onKeyDown={e => e.key === "Enter" && ask(q)} />
            <button className="btn pri" disabled={busy} onClick={() => ask(q)}>Ask</button></div>
          <label className="row" style={{ fontSize: 13, gap: 8, justifyContent: "flex-start", cursor: "pointer" }}>
            <input type="checkbox" checked={smallOn} onChange={e => setSmall(e.target.checked)} style={{ width: 22, height: 22 }} />
            <span>Let the assistant make small changes for me
              <Tip text="Only on your own side, only when you ask: tonight up to 1.5 °C (about 3 °F) warmer or cooler in total, and starting or stopping a flash. Never your rhythm, Autopilot, notifications or turning the night off. Every change can be undone." /></span>
          </label>
          {!ai?.configured && <p className="faint" style={{ fontSize: 12 }}>To ask open questions with an AI model, connect one in <Link to="/studio/ai">Studio → AI model</Link>. Nothing leaves the house unless you choose a hosted provider.</p>}
          {ai?.configured && ai.local === false && <p className="faint" style={{ fontSize: 12 }}>Summaries sent for Ask go to {ai.preset_name ?? "the connected AI provider"} (<Link to="/studio/ai">Studio → AI model</Link>).</p>}
        </div>}
      </div>
    </>
  );
}
