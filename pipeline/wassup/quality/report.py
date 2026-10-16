"""`wassup audit-report`: what the audits got wrong, in one page of text to read or share.

For each kind of check: how often it was wrong, which desks lose stories to which (and which
desk the judge put them on), and every wrong verdict with the judge's reason. Connections are
broken down by how they were made (similar meaning, shared people and organizations, or an
agent), with the evidence the link was made on.
"""
from __future__ import annotations

from collections import Counter, defaultdict


def _pct(w: int, n: int) -> str:
    return f"{round(100 * w / n)}%" if n else "-"


def audit_report(conn, days: int = 7, per_aspect: int = 40) -> str:
    rows = conn.execute(
        """SELECT c.*, coalesce(a.judge, c.by) AS judge FROM audit_checks c LEFT JOIN audits a ON a.id = c.audit_id
           WHERE c.judged_at > now() - make_interval(days => %s) AND c.verdict IS NOT NULL ORDER BY c.id""", (days,)).fetchall()
    out = [f"# Wassup audit report, last {days} days", ""]
    audits = conn.execute("""SELECT a.id, a.judge, a.created_at, count(c.*) AS n FROM audits a LEFT JOIN audit_checks c ON c.audit_id = a.id
                             WHERE a.created_at > now() - make_interval(days => %s) GROUP BY a.id ORDER BY a.id""", (days,)).fetchall()
    out.append("Audits: " + ", ".join(f"#{a['id']} {a['created_at']:%b %d} by {a['judge']} ({a['n']} checks)" for a in audits))
    by = defaultdict(list)
    for r in rows:
        by[r["aspect"]].append(r)
    out += ["", "| check | judged | wrong | unsure | wrong rate |", "| --- | --- | --- | --- | --- |"]
    for aspect, rs in by.items():
        w = sum(r["verdict"] == "wrong" for r in rs)
        u = sum(r["verdict"] == "unsure" for r in rs)
        out.append(f"| {aspect} | {len(rs)} | {w} | {u} | {_pct(w, len(rs) - u)} |")

    desk = by.get("desk", [])
    if desk:
        out += ["", "## Desk", "", "Per desk (stories the rules or the review put there):", ""]
        per = defaultdict(Counter)
        moves = Counter()
        for r in desk:
            d = r["subject"].get("desk")
            per[d][r["verdict"]] += 1
            if r["verdict"] == "wrong":
                moves[(d, r["answer"] or "none")] += 1
        for d, c in sorted(per.items(), key=lambda x: -x[1]["wrong"]):
            out.append(f"- {d}: {c['wrong']} wrong of {c['wrong'] + c['right']} ({_pct(c['wrong'], c['wrong'] + c['right'])})")
        out += ["", "Where the judge said they belong (was -> should be):", ""]
        out += [f"- {a} -> {b}: {n}" for (a, b), n in moves.most_common(20)]
        out += ["", "Wrong verdicts, with what put each story on its desk:", ""]
        wrong = [r for r in desk if r["verdict"] == "wrong"][:per_aspect]
        how = {x["id"]: x for x in conn.execute(
            "SELECT id, triage, desk_review FROM stories WHERE id = ANY(%s)", ([r["story_id"] for r in wrong if r["story_id"]],))}
        backends = Counter()
        for r in wrong:
            s = r["subject"]
            why = _why(how.get(r["story_id"]))
            backends[why.split(":")[0]] += 1
            out.append(f"- [{s.get('desk')} -> {r['answer'] or 'none'}] {s.get('title')}  [{why}]" + (f"  ({r['note']})" if r["note"] else ""))
        if backends:
            out += ["", "Who decided the desks judged wrong: " + ", ".join(f"{k} {n}" for k, n in backends.most_common())]

    links = by.get("link", [])
    if links:
        out += ["", "## Connections", "", "By how they were made:", ""]
        kinds = defaultdict(Counter)
        for r in links:
            s = r["subject"]
            how = s.get("kind") if not str(s.get("by") or "").startswith("agent") else f"{s.get('kind')} (agent)"
            kinds[how][r["verdict"]] += 1
        for k, c in sorted(kinds.items(), key=lambda x: -sum(x[1].values())):
            n = c["wrong"] + c["right"]
            out.append(f"- {k}: {c['wrong']} wrong of {n} ({_pct(c['wrong'], n)}), {c['unsure']} unsure")
        sims = defaultdict(list)
        for r in links:
            ev = r["subject"].get("evidence") or {}
            if isinstance(ev, dict) and ev.get("similarity") is not None:
                sims[r["verdict"]].append(float(ev["similarity"]))
        if sims:
            out += ["", "Similarity of 'related' links by verdict (higher is closer in meaning):", ""]
            for v, xs in sims.items():
                xs.sort()
                out.append(f"- {v}: {len(xs)} links, median {xs[len(xs) // 2]:.2f}, lowest {xs[0]:.2f}, highest {xs[-1]:.2f}")
        for verdict in ("wrong", "right"):
            out += ["", f"{verdict.capitalize()} verdicts:", ""]
            for r in [r for r in links if r["verdict"] == verdict][:per_aspect if verdict == "wrong" else 12]:
                s = r["subject"]
                ev = s.get("evidence") or {}
                why = (f"similarity {ev['similarity']}" if isinstance(ev, dict) and "similarity" in ev
                       else f"shared: {', '.join(ev.get('entities', [])[:5])}" if isinstance(ev, dict) and ev.get("entities")
                       else str(ev)[:120])
                out.append(f"- {s.get('kind')} ({why}): {s.get('a_title')}  //  {s.get('b_title')}" + (f"  ({r['note']})" if r["note"] else ""))

    for aspect in ("missed", "place", "grouping", "importance"):
        rs = [r for r in by.get(aspect, []) if r["verdict"] == "wrong"]
        if not rs:
            continue
        out += ["", f"## {aspect.capitalize()}: wrong verdicts", ""]
        for r in rs[:20]:
            s = r["subject"]
            out.append(f"- {s.get('title') or s.get('a_title')} -> {r['answer'] or '-'}" + (f"  ({r['note']})" if r["note"] else ""))
    return "\n".join(out) + "\n"


def _why(st: dict | None) -> str:
    """What decided a story's desk, from what triage and the second look recorded."""
    if not st:
        return "story gone (merged)"
    t, rv = st.get("triage") or {}, st.get("desk_review") or {}
    if rv.get("by") and rv.get("by") not in ("local",):
        return f"audit/you: {rv.get('by')}"
    if rv.get("desk") and rv.get("sure"):
        return f"second look: model chose {rv['desk']}"
    backend = t.get("backend") or "?"
    kws = ((t.get("matched") or {}).get("keywords") or [])[:4]
    return f"{backend}: keywords {', '.join(kws) if kws else 'none'}"
