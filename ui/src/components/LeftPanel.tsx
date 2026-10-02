import type { Desk, Story } from "../lib/types";
import { ago, deskColor } from "../lib/format";

interface Props {
  desks: Desk[];
  deskMap: Map<string, Desk>;
  enabled: Set<string>;
  counts: Map<string, number>;
  stories: Story[];
  cold: boolean;
  minSig: number;
  showLinks: boolean;
  showLabels: boolean;
  autoRotate: boolean;
  onToggleDesk: (key: string, solo: boolean) => void;
  onAllDesks: () => void;
  onCold: (v: boolean) => void;
  onMinSig: (v: number) => void;
  onLinks: (v: boolean) => void;
  onLabels: (v: boolean) => void;
  onRotate: (v: boolean) => void;
  onSelectStory: (id: number) => void;
}

function Toggle({ label, on, set }: { label: string; on: boolean; set: (v: boolean) => void }) {
  return (
    <div className="toggle-row" onClick={() => set(!on)}>
      <span>{label}</span>
      <span className={`switch ${on ? "on" : ""}`} />
    </div>
  );
}

export default function LeftPanel(p: Props) {
  const breaking = p.stories.filter((s) => s.breaking).sort((a, b) => b.velocity - a.velocity).slice(0, 8);
  const top = p.stories.filter((s) => !s.breaking).slice(0, 10);
  const allOn = p.enabled.size === p.desks.length;
  return (
    <aside className="left panel">
      <div className="section">
        <div className="h">
          Desks
          {!allOn && <button onClick={p.onAllDesks}>Show all</button>}
        </div>
        {p.desks.map((d) => (
          <div key={d.key} className={`desk-row ${p.enabled.has(d.key) ? "" : "off"}`}
            onClick={(e) => p.onToggleDesk(d.key, e.altKey || e.metaKey || e.ctrlKey)}
            title={`${d.description || d.name}\nClick to toggle. Ctrl or Alt click to show only this desk.`}>
            <span className="swatch" style={{ background: d.color }} />
            <span>{d.name}</span>
            <span className="count">{p.counts.get(d.key) ?? 0}</span>
          </div>
        ))}
      </div>

      <div className="section">
        <div className="h">Filters</div>
        <Toggle label="Include cold storage" on={p.cold} set={p.onCold} />
        <div style={{ marginTop: 8 }}>
          <div className="toggle-row" style={{ cursor: "default" }}>
            <span>Minimum significance</span><span className="mono">{p.minSig.toFixed(1)}</span>
          </div>
          <input type="range" min={0} max={4} step={0.25} value={p.minSig} onChange={(e) => p.onMinSig(Number(e.target.value))} />
        </div>
      </div>

      <div className="section">
        <div className="h">Layers</div>
        <Toggle label="Connection strings" on={p.showLinks} set={p.onLinks} />
        <Toggle label="Place labels" on={p.showLabels} set={p.onLabels} />
        <Toggle label="Auto rotate" on={p.autoRotate} set={p.onRotate} />
      </div>

      {breaking.length > 0 && (
        <div className="section">
          <div className="h"><span><span className="live-dot" />Breaking</span></div>
          {breaking.map((s) => (
            <div key={s.id} className="brk" onClick={() => p.onSelectStory(s.id)}>
              <div className="t">{s.title}</div>
              <div className="m"><span style={{ color: deskColor(p.deskMap, s.desk) }}>■</span> {s.velocity}/hr · {s.source_count} outlets · {ago(s.last_seen)}</div>
            </div>
          ))}
        </div>
      )}

      <div className="section">
        <div className="h">Most significant</div>
        {top.map((s) => (
          <div key={s.id} className="brk" onClick={() => p.onSelectStory(s.id)}>
            <div className="t">{s.title}</div>
            <div className="m"><span style={{ color: deskColor(p.deskMap, s.desk) }}>■</span> sig {s.significance.toFixed(1)} · {s.source_count} outlets · {ago(s.last_seen)}</div>
          </div>
        ))}
        {!top.length && <div className="dimmer">Nothing in this window yet.</div>}
      </div>
    </aside>
  );
}
