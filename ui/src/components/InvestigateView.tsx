import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../lib/api";
import type { InvestigationDetail, InvestigationListItem, InvestigationSource, InvestigationSummary } from "../lib/types";
import { ago } from "../lib/format";

const ACCOUNT: Record<string, { label: string; cls: string; tip: string }> = {
  eyewitness: { label: "eyewitness", cls: "first", tip: "Saw or heard it themselves" },
  participant: { label: "involved", cls: "first", tip: "One of the people involved, speaking for themselves" },
  official: { label: "official", cls: "first", tip: "An organisation's own statement" },
  document: { label: "document", cls: "first", tip: "A primary record" },
  original_reporting: { label: "own reporting", cls: "orig", tip: "A reporter's own interviews, documents and checks" },
  secondhand: { label: "secondhand", cls: "second", tip: "Passes on someone else's reporting" },
  commentary: { label: "commentary", cls: "comment", tip: "Opinion or reaction" },
  unrelated: { label: "unrelated", cls: "second", tip: "Not about this story" },
};
const FOUND: Record<string, string> = { you: "you", wassup: "already in Wassup", search: "search", traced: "cited" };
const EVIDENCE: Record<string, string> = {
  "only described": "warn", "seen by a reporter": "orig", "checked independently": "first", disputed: "alert",
};
const FIRSTHAND = new Set(["eyewitness", "participant", "official", "document"]);
const day = (s: string | null) => (s ? new Date(s).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }) : "date unknown");

type Tab = "firsthand" | "all" | "traced" | "unrelated" | "problems";

function Refs({ refs, summary, onJump }: { refs: number[]; summary: InvestigationSummary; onJump: (sourceId: number) => void }) {
  return (
    <span className="iv-refs">
      {refs.map((n) => {
        const id = summary.source_ids[String(n)];
        return id ? <button key={n} className="linkish" onClick={() => onJump(id)} title="Show this source">[{n}]</button> : null;
      })}
    </span>
  );
}

function Summary({ s, onJump, when, from }: { s: InvestigationSummary; onJump: (id: number) => void; when: string | null; from: number }) {
  return (
    <div className="iv-summary">
      <h3>{s.headline}</h3>
      <div className="dimmer mono iv-note">Written by the local model from {from} sources{when ? `, ${ago(when)}` : ""}. Numbers point to the sources below.</div>
      {s.answers.length > 0 && <div className="iv-sec"><div className="h">Your questions</div>
        {s.answers.map((a, i) => (
          <div key={i} className="iv-answer">
            <div className="q">{a.question} <span className={`chip conf-${a.confidence}`} title="How well the sources support this answer">{a.confidence} confidence</span></div>
            <div>{a.answer} <Refs refs={a.sources} summary={s} onJump={onJump} /></div>
          </div>
        ))}</div>}
      {s.evidence.length > 0 && <div className="iv-sec"><div className="h">Evidence</div>
        <table className="iv-table"><tbody>
          {s.evidence.map((e, i) => (
            <tr key={i}>
              <td><b>{e.what}</b><div className="dim">{e.detail} <Refs refs={e.sources} summary={s} onJump={onJump} /></div></td>
              <td className="dim">held by {e.held_by || "?"}</td>
              <td><span className={`chip ev-${EVIDENCE[e.status] ?? "warn"}`}>{e.status}</span></td>
            </tr>
          ))}
        </tbody></table></div>}
      {s.people.length > 0 && <div className="iv-sec"><div className="h">People and their interests</div>
        {s.people.map((p, i) => (
          <div key={i} className="iv-person">
            <b>{p.name}</b> <span className="dim">· {p.role}</span>
            {p.position && <div>{p.position}</div>}
            <div className="dim">Interests: {p.interest || "not reported"} <Refs refs={p.sources} summary={s} onJump={onJump} /></div>
          </div>
        ))}</div>}
      {s.origin && <div className="iv-sec"><div className="h">How the story came out</div><div>{s.origin}</div></div>}
      {s.timeline.length > 0 && <div className="iv-sec"><div className="h">Timeline</div>
        {s.timeline.map((t, i) => (
          <div key={i} className="iv-tl"><span className="mono">{t.date}</span><span>{t.what} <Refs refs={t.sources} summary={s} onJump={onJump} /></span></div>
        ))}</div>}
      {s.disagreements.length > 0 && <div className="iv-sec"><div className="h">Where accounts disagree</div>
        {s.disagreements.map((d, i) => <div key={i} className="iv-person"><b>{d.about}</b><div>{d.sides} <Refs refs={d.sources} summary={s} onJump={onJump} /></div></div>)}
      </div>}
      {s.open_questions.length > 0 && <div className="iv-sec"><div className="h">Still unknown</div>
        <ul>{s.open_questions.map((q, i) => <li key={i}>{q}</li>)}</ul></div>}
    </div>
  );
}

function SourceCard({ s, inv, parent, onAct, onPaste, flash }: {
  s: InvestigationSource; inv: InvestigationDetail; parent: InvestigationSource | undefined;
  onAct: (s: InvestigationSource, action: string) => void; onPaste: (s: InvestigationSource) => void; flash: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState<string | null>(null);
  const a = s.analysis;
  const acc = a ? ACCOUNT[a.account] : undefined;
  const showText = () => {
    if (text !== null) { setText(null); return; }
    api.investigationSourceText(inv.id, s.id).then((r) => setText(r.text)).catch(() => setText("(could not load the text)"));
  };
  return (
    <div id={`src-${s.id}`} className={`iv-src ${s.hidden ? "hidden" : ""} ${flash ? "flash" : ""}`}>
      {s.thumbnail && <img className="iv-thumb" src={s.thumbnail} alt="" loading="lazy" referrerPolicy="no-referrer" />}
      <div className="iv-src-main">
        <div className="iv-src-head mono">
          <span>{day(s.published_at)}</span>
          <span className="dim">{s.outlet ?? s.kind}{s.author && s.outlet && !s.outlet.includes(s.author) ? ` · ${s.author}` : ""}</span>
          {acc && <span className={`chip acc-${acc.cls}`} title={acc.tip}>{acc.label}</span>}
          <span className="chip" title="How Wassup found it">
            {s.found_by === "traced" && parent ? `cited by ${parent.outlet ?? "a source"}` : FOUND[s.found_by]}
          </span>
          {s.pinned && <span className="chip">pinned</span>}
          {s.partial && !s.pasted && <span className="chip warn" title="Only the start could be read without logging in">partial</span>}
          {s.pasted && <span className="chip">text pasted by you</span>}
          {s.status === "pending" && <span className="chip">reading…</span>}
          {s.status === "fetched" && <span className="chip">waiting for the model…</span>}
        </div>
        <a className="iv-title" href={s.url.startsWith("wassup:") ? undefined : s.url} target="_blank" rel="noreferrer noopener">{s.title ?? s.url}</a>
        {s.status === "failed" && <div className="warn-text">Could not read: {s.error}. {s.kind !== "document" && "You can paste its text instead."}</div>}
        {a?.summary && <div className="iv-sum">{a.summary}</div>}
        {a && a.relies_on.length > 0 && <div className="dim iv-small">Relies on: {a.relies_on.join(", ")}</div>}
        {open && a && (
          <div className="iv-detail">
            {a.evidence.length > 0 && <><div className="h">Evidence it mentions</div>{a.evidence.map((e, i) => (
              <div key={i}>• {e.what} <span className="dim">({e.kind}; held by {e.held_by || "?"}; {e.seen_by_source ? "the author says they saw it" : "described, not seen by the author"}{e.checked_by ? `; checked by ${e.checked_by}` : ""})</span>
                {e.quote && <div className="iv-quote">“{e.quote}”</div>}</div>))}</>}
            {a.people.length > 0 && <><div className="h">People</div>{a.people.map((p, i) => (
              <div key={i}>• <b>{p.name}</b> <span className="dim">{p.role}, {p.stance.replace("_", " ")}</span>{p.interest && <> · {p.interest}</>}</div>))}</>}
            {a.claims.length > 0 && <><div className="h">Claims</div>{a.claims.map((c, i) => (
              <div key={i}>• {c.claim} <span className="dim">({c.who_says})</span></div>))}</>}
            {a.responses.length > 0 && <><div className="h">Responses</div>{a.responses.map((r, i) => (
              <div key={i}>• <b>{r.who}</b>: {r.response}</div>))}</>}
          </div>
        )}
        {text !== null && <pre className="iv-text">{text}</pre>}
        <div className="iv-actions mono">
          {a && <button className="linkish" onClick={() => setOpen(!open)}>{open ? "less" : "evidence, people, claims"}</button>}
          {(s.chars > 0 || s.status === "analyzed") && <button className="linkish" onClick={showText}>{text === null ? "full text" : "hide text"}</button>}
          {(s.partial || s.status === "failed") && s.kind !== "document" && <button className="linkish" onClick={() => onPaste(s)}>paste its text</button>}
          <button className="linkish" onClick={() => onAct(s, s.pinned ? "unpin" : "pin")} title="Pinned sources always go into the summary">{s.pinned ? "unpin" : "pin"}</button>
          <button className="linkish" onClick={() => onAct(s, s.hidden ? "unhide" : "hide")} title="Leave it out of the summary">{s.hidden ? "unhide" : "hide"}</button>
          {s.status !== "pending" && <button className="linkish" onClick={() => onAct(s, "reread")}>read again</button>}
        </div>
      </div>
    </div>
  );
}

function NewForm({ onCreated }: { onCreated: (id: number) => void }) {
  const [title, setTitle] = useState("");
  const [brief, setBrief] = useState("");
  const [links, setLinks] = useState("");
  const [days, setDays] = useState(14);
  const [msg, setMsg] = useState<string | null>(null);
  const start = () => api.newInvestigation(title, brief, links, days).then((r) => onCreated(r.id)).catch((e) => setMsg(String(e.message ?? e)));
  return (
    <div className="iv-new">
      <h2>New investigation</h2>
      <label>Name it<input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="For example: Pastor accused of hiring escorts on ministry trips" /></label>
      <label>What do you want to know?<textarea rows={4} value={brief} onChange={(e) => setBrief(e.target.value)}
        placeholder="What evidence actually exists? Who is the source and what do they gain or lose? How is the story developing?" /></label>
      <label>Links you have, one per line: articles, YouTube, Telegram, X, Facebook<textarea rows={7} value={links} onChange={(e) => setLinks(e.target.value)} placeholder="https://..." /></label>
      <label className="row">Keep watching for new material for
        <select value={days} onChange={(e) => setDays(Number(e.target.value))}>{[3, 7, 14, 30, 90].map((d) => <option key={d} value={d}>{d} days</option>)}</select>
      </label>
      {msg && <div className="warn-text">{msg}</div>}
      <button className="btn primary" disabled={!title.trim()} onClick={start}>START</button>
      <div className="dim iv-small">
        Wassup reads every link, finds related reports it already has, follows what each source cites back toward the original,
        searches news, YouTube and Telegram for more, and writes a summary that answers your questions from the sources.
        Facebook posts can only be partly read without logging in: paste their text afterwards.
      </div>
    </div>
  );
}

export default function InvestigateView() {
  const [list, setList] = useState<InvestigationListItem[]>([]);
  const [current, setCurrent] = useState<number | "new" | null>(null);
  const [inv, setInv] = useState<InvestigationDetail | null>(null);
  const [tab, setTab] = useState<Tab>("firsthand");
  const [adding, setAdding] = useState<"links" | "text" | null>(null);
  const [draft, setDraft] = useState({ links: "", text: "", url: "", author: "" });
  const [msg, setMsg] = useState<string | null>(null);
  const [flash, setFlash] = useState<number | null>(null);

  const loadList = useCallback(() => api.investigations().then((l) => {
    setList(l);
    setCurrent((c) => c ?? (l[0]?.id ?? "new"));
  }).catch((e) => setMsg(String(e))), []);
  const load = useCallback(() => { if (typeof current === "number") api.investigation(current).then(setInv).catch((e) => setMsg(String(e))); }, [current]);
  useEffect(() => { loadList(); }, [loadList]);
  useEffect(() => { setInv(null); load(); const t = setInterval(load, 15_000); return () => clearInterval(t); }, [load]);

  const byId = useMemo(() => new Map((inv?.sources ?? []).map((s) => [s.id, s])), [inv]);
  const groups = useMemo(() => {
    const all = (inv?.sources ?? []).filter((s) => s.status !== "unrelated" && s.status !== "failed");
    return {
      firsthand: all.filter((s) => s.analysis && FIRSTHAND.has(s.analysis.account)),
      all,
      traced: (inv?.sources ?? []).filter((s) => s.found_by === "traced"),
      unrelated: (inv?.sources ?? []).filter((s) => s.status === "unrelated"),
      problems: (inv?.sources ?? []).filter((s) => s.status === "failed" || s.partial && !s.pasted),
    };
  }, [inv]);

  const act = (s: InvestigationSource, action: string) => inv && api.investigationSource(inv.id, s.id, action).then(load);
  const jump = (id: number) => {
    const s = byId.get(id);
    if (s && !groups[tab].some((x) => x.id === id)) setTab("all");
    setTimeout(() => { document.getElementById(`src-${id}`)?.scrollIntoView({ behavior: "smooth", block: "center" }); setFlash(id); }, 50);
    setTimeout(() => setFlash(null), 2000);
  };
  const paste = (s: InvestigationSource) => { setAdding("text"); setDraft((d) => ({ ...d, url: s.url, text: "", author: s.author ?? "" })); window.scrollTo(0, 0); };
  const submit = () => {
    if (!inv) return;
    const p = adding === "links" ? api.investigationLinks(inv.id, draft.links)
      : api.investigationText(inv.id, { text: draft.text, url: draft.url || undefined, author: draft.author || undefined });
    p.then(() => { setAdding(null); setDraft({ links: "", text: "", url: "", author: "" }); setMsg("Added. It is read within a minute or two."); load(); })
      .catch((e) => setMsg(String(e.message ?? e)));
  };

  const counts = inv && {
    waiting: inv.sources.filter((s) => s.status === "pending" || s.status === "fetched").length,
    relevant: inv.sources.filter((s) => s.status === "analyzed" && !s.hidden).length,
  };

  return (
    <div className="investigate-view">
      <div className="iv-list">
        <button className="btn primary" onClick={() => setCurrent("new")}>+ NEW INVESTIGATION</button>
        {list.map((i) => (
          <button key={i.id} className={`iv-item ${current === i.id ? "on" : ""}`} onClick={() => setCurrent(i.id)}>
            <b>{i.title}</b>
            <span className="dim mono">{i.relevant} sources · {i.firsthand} first-hand{i.waiting ? ` · ${i.waiting} to read` : ""}</span>
            <span className="dimmer mono">{i.status === "active" ? `watching until ${day(i.watch_until)}` : i.status}</span>
          </button>
        ))}
      </div>
      <div className="iv-main">
        {msg && <div className="src-msg" onClick={() => setMsg(null)}>{msg}</div>}
        {current === "new" && <NewForm onCreated={(id) => { loadList(); setCurrent(id); }} />}
        {inv && current === inv.id && (
          <>
            <div className="iv-head">
              <div>
                <h2>{inv.title}</h2>
                {inv.brief && <div className="iv-brief">{inv.brief}</div>}
                <div className="dim mono iv-small">
                  {inv.sources.length} sources found, {counts!.relevant} about the story, {groups.firsthand.length} first-hand
                  {counts!.waiting > 0 && <span className="warn-text"> · reading {counts!.waiting} more…</span>}
                  {" · "}{inv.status === "active" ? `watching for new material until ${day(inv.watch_until)}` : inv.status}
                </div>
              </div>
              <div className="iv-buttons">
                <button className="btn" onClick={() => setAdding(adding === "links" ? null : "links")}>ADD LINKS</button>
                <button className="btn" onClick={() => { setAdding(adding === "text" ? null : "text"); setDraft((d) => ({ ...d, url: "" })); }}>PASTE TEXT</button>
                <button className="btn" onClick={() => api.investigationRefresh(inv.id).then(() => { setMsg("Looking again: related reports, a new search and a new summary."); load(); })}>LOOK AGAIN</button>
                <button className="btn" onClick={() => api.investigationEdit(inv.id, { status: inv.status === "active" ? "paused" : "active" }).then(() => { load(); loadList(); })}>
                  {inv.status === "active" ? "PAUSE" : "RESUME"}
                </button>
              </div>
            </div>
            {adding && (
              <div className="iv-add">
                {adding === "links"
                  ? <textarea rows={5} value={draft.links} onChange={(e) => setDraft({ ...draft, links: e.target.value })} placeholder="More links, one per line" />
                  : <>
                      <input value={draft.url} onChange={(e) => setDraft({ ...draft, url: e.target.value })} placeholder="Where it is from (link), if it has one" />
                      <input value={draft.author} onChange={(e) => setDraft({ ...draft, author: e.target.value })} placeholder="Who wrote or posted it" />
                      <textarea rows={8} value={draft.text} onChange={(e) => setDraft({ ...draft, text: e.target.value })} placeholder="Paste the text of a post, a statement, notes..." />
                    </>}
                <button className="btn primary" onClick={submit}>ADD</button>
              </div>
            )}
            {inv.summary
              ? <Summary s={inv.summary} onJump={jump} when={inv.summary_at} from={inv.summary_sources} />
              : <div className="iv-summary dim">The summary is written once the first sources have been read. This takes a few minutes; the page updates by itself.</div>}
            <div className="iv-tabs mono">
              {([["firsthand", "FIRST-HAND"], ["all", "ALL, BY DATE"], ["traced", "CITED SOURCES"], ["unrelated", "NOT ABOUT IT"], ["problems", "COULD NOT READ"]] as [Tab, string][]).map(([k, label]) => (
                <button key={k} className={`btn ${tab === k ? "on" : ""}`} onClick={() => setTab(k)}>{label} <span className="dimmer">{groups[k].length}</span></button>
              ))}
            </div>
            {groups[tab].length ? groups[tab].map((s) => (
              <SourceCard key={s.id} s={s} inv={inv} parent={s.parent_id ? byId.get(s.parent_id) : undefined} onAct={act} onPaste={paste} flash={flash === s.id} />
            )) : <div className="empty dim">{tab === "firsthand" ? "No first-hand sources identified yet." : "None."}</div>}
            {inv.queries.length > 0 && <div className="dimmer mono iv-small" style={{ marginTop: 14 }}>Searched for: {inv.queries.map((q) => q.query).join(" · ")}</div>}
          </>
        )}
      </div>
    </div>
  );
}
