import { ReactNode, useEffect, useRef, useState } from "react";

// Validated categorical palette (dark steps). Color follows the node (by registration order), never its rank.
export const SERIES = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#0ca30c", "#9085e9", "#e66767"];
export const nodeColor = (i: number) => (i < SERIES.length ? SERIES[i] : "#8a897f");

export async function api<T = any>(path: string, body?: unknown): Promise<T> {
  const r = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: body === undefined ? {} : { "Content-Type": "application/json", "X-MeshRAM": "1" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || j.error || `HTTP ${r.status}`);
  return j;
}

export function usePoll<T>(path: string, ms = 1000): [T | null, string | null] {
  const [data, setData] = useState<T | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const d = await api<T>(path);
        if (alive) { setData(d); setErr(null); }
      } catch (e: any) {
        if (alive) setErr(e.message);
      }
    };
    tick();
    const id = setInterval(tick, ms);
    return () => { alive = false; clearInterval(id); };
  }, [path, ms]);
  return [data, err];
}

export const gib = (b?: number | null, d = 1) => (b == null ? "—" : `${(b / 2 ** 30).toFixed(d)} GiB`);
export const mib = (b?: number | null) => (b == null ? "—" : `${Math.round(b / 2 ** 20).toLocaleString()} MiB`);
export const bytes = (b?: number | null) => {
  if (b == null) return "—";
  if (b < 1024) return `${b} B`;
  if (b < 2 ** 20) return `${(b / 1024).toFixed(1)} KB`;
  if (b < 2 ** 30) return `${(b / 2 ** 20).toFixed(1)} MB`;
  return `${(b / 2 ** 30).toFixed(2)} GB`;
};
export const rate = (bps?: number) => (bps == null ? "—" : `${bytes(bps)}/s`);
export const ago = (ts?: number) => {
  if (!ts) return "—";
  const s = Math.max(0, Date.now() / 1000 - ts);
  return s < 60 ? `${Math.round(s)}s ago` : s < 3600 ? `${Math.round(s / 60)}m ago` : `${Math.round(s / 3600)}h ago`;
};

export function Panel({ title, right, children, className = "" }: { title?: ReactNode; right?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`panel p-5 ${className}`}>
      {(title || right) && (
        <div className="mb-4 flex items-center justify-between gap-3">
          <h2 className="text-[13px] font-semibold uppercase tracking-[0.08em] text-ink-2">{title}</h2>
          <div className="flex items-center gap-2">{right}</div>
        </div>
      )}
      {children}
    </section>
  );
}

type Tone = "good" | "warn" | "serious" | "critical" | "neutral" | "accent";
const toneCls: Record<Tone, string> = {
  good: "text-good bg-good/10 ring-good/30",
  warn: "text-warn bg-warn/10 ring-warn/30",
  serious: "text-serious bg-serious/10 ring-serious/30",
  critical: "text-critical bg-critical/10 ring-critical/30",
  neutral: "text-ink-2 bg-white/5 ring-white/10",
  accent: "text-accent bg-accent/10 ring-accent/30",
};
const toneIcon: Record<Tone, string> = { good: "●", warn: "▲", serious: "◆", critical: "✕", neutral: "○", accent: "●" };

/** Status always carries an icon + label, never color alone. */
export function Pill({ tone = "neutral", children }: { tone?: Tone; children: ReactNode }) {
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium ring-1 ${toneCls[tone]}`}>
      <span className="text-[9px]">{toneIcon[tone]}</span>
      {children}
    </span>
  );
}

export function Btn({ children, onClick, kind = "ghost", disabled, title, className = "" }: {
  children: ReactNode; onClick?: () => void; kind?: "primary" | "ghost" | "danger"; disabled?: boolean; title?: string; className?: string;
}) {
  const k = {
    primary: "bg-accent text-white hover:bg-[#4b95ec] shadow-[0_0_0_1px_rgba(57,135,229,.4),0_8px_24px_-8px_rgba(57,135,229,.6)]",
    ghost: "bg-white/[0.04] text-ink ring-1 ring-white/10 hover:bg-white/[0.08]",
    danger: "bg-critical/10 text-[#f08a8a] ring-1 ring-critical/40 hover:bg-critical/20",
  }[kind];
  return (
    <button title={title} disabled={disabled} onClick={onClick}
      className={`rounded-lg px-3 py-1.5 text-sm font-medium transition disabled:cursor-not-allowed disabled:opacity-40 ${k} ${className}`}>
      {children}
    </button>
  );
}

export function Stat({ label, value, sub }: { label: string; value: ReactNode; sub?: ReactNode }) {
  return (
    <div className="panel px-4 py-3.5">
      <div className="text-[11px] font-medium uppercase tracking-[0.08em] text-ink-3">{label}</div>
      <div className="num mt-1 text-[22px] font-semibold leading-tight tracking-tight">{value}</div>
      {sub && <div className="num mt-0.5 text-xs text-ink-3">{sub}</div>}
    </div>
  );
}

/** Horizontal meter; segments separated by 2px surface gaps. */
export function Meter({ segs, total, h = 8 }: { segs: { v: number; c: string; label: string }[]; total: number; h?: number }) {
  return (
    <div className="flex w-full gap-[2px] overflow-hidden rounded-full bg-white/[0.06]" style={{ height: h }}>
      {segs.filter((s) => s.v > 0).map((s, i) => (
        <div key={i} title={`${s.label}: ${gib(s.v, 2)}`} style={{ width: `${Math.min(100, (s.v / Math.max(total, 1)) * 100)}%`, background: s.c }}
          className="h-full transition-all duration-500 first:rounded-l-full last:rounded-r-full" />
      ))}
    </div>
  );
}

/** Line/area sparkline with crosshair tooltip. One y-axis; multiple series share units. */
export function Spark({ data, series, fmt, height = 92, max }: {
  data: any[]; series: { key: string; color: string; label: string }[]; fmt: (v: number) => string; height?: number; max?: number;
}) {
  const ref = useRef<SVGSVGElement>(null);
  const [hover, setHover] = useState<number | null>(null);
  const W = 320, H = height, pad = 4;
  const n = data.length;
  const top = max ?? Math.max(1e-9, ...data.flatMap((d) => series.map((s) => d[s.key] || 0))) * 1.15;
  const x = (i: number) => (n <= 1 ? W : (i / (n - 1)) * W);
  const y = (v: number) => H - pad - (v / top) * (H - pad * 2);
  const last = data[n - 1];
  return (
    <div className="relative">
      <svg ref={ref} viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" className="h-[92px] w-full overflow-visible"
        onMouseMove={(e) => {
          const r = ref.current!.getBoundingClientRect();
          setHover(Math.round(((e.clientX - r.left) / r.width) * (n - 1)));
        }}
        onMouseLeave={() => setHover(null)}>
        {[0.25, 0.5, 0.75].map((f) => <line key={f} x1={0} x2={W} y1={H * f} y2={H * f} stroke="#ffffff" strokeOpacity={0.04} />)}
        {series.map((s) => {
          const pts = data.map((d, i) => `${x(i)},${y(d[s.key] || 0)}`).join(" ");
          return (
            <g key={s.key}>
              {series.length === 1 && n > 1 && <polygon points={`0,${H} ${pts} ${W},${H}`} fill={s.color} fillOpacity={0.12} />}
              <polyline points={pts} fill="none" stroke={s.color} strokeWidth={2} vectorEffect="non-scaling-stroke" strokeLinejoin="round" />
            </g>
          );
        })}
        {hover != null && data[hover] && <line x1={x(hover)} x2={x(hover)} y1={0} y2={H} stroke="#fff" strokeOpacity={0.25} vectorEffect="non-scaling-stroke" />}
      </svg>
      {hover != null && data[hover] && (
        <div className="pointer-events-none absolute top-0 z-10 rounded-md bg-[#262624] px-2 py-1 text-xs shadow-lg ring-1 ring-white/10"
          style={{ left: `${Math.min(70, (hover / Math.max(n - 1, 1)) * 100)}%` }}>
          <div className="text-ink-3">{new Date(data[hover].ts * 1000).toLocaleTimeString()}</div>
          {series.map((s) => (
            <div key={s.key} className="num flex items-center gap-1.5"><span className="h-2 w-2 rounded-full" style={{ background: s.color }} />{s.label} {fmt(data[hover][s.key] || 0)}</div>
          ))}
        </div>
      )}
      <div className="mt-2 flex flex-wrap gap-3 text-xs text-ink-2">
        {series.map((s) => (
          <span key={s.key} className="num inline-flex items-center gap-1.5">
            <span className="h-[3px] w-3 rounded-full" style={{ background: s.color }} />
            {series.length > 1 && s.label} <span className="text-ink">{last ? fmt(last[s.key] || 0) : "—"}</span>
          </span>
        ))}
      </div>
    </div>
  );
}

export function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return (
    <label className="block">
      <div className="mb-1 text-xs font-medium text-ink-2">{label}</div>
      {children}
      {hint && <div className="mt-1 text-[11px] text-ink-3">{hint}</div>}
    </label>
  );
}

export const inputCls = "w-full rounded-lg bg-black/30 px-3 py-2 text-sm text-ink ring-1 ring-white/10 outline-none focus:ring-accent/60 num";

export function Toggle({ on, onChange, label }: { on: boolean; onChange: (v: boolean) => void; label: string }) {
  return (
    <button type="button" onClick={() => onChange(!on)} className="flex w-full items-center justify-between gap-3 py-1.5 text-left text-sm">
      <span className="text-ink-2">{label}</span>
      <span className={`relative h-5 w-9 rounded-full transition ${on ? "bg-accent" : "bg-white/15"}`}>
        <span className={`absolute top-0.5 h-4 w-4 rounded-full bg-white transition-all ${on ? "left-[18px]" : "left-0.5"}`} />
      </span>
    </button>
  );
}

export function Logo() {
  return (
    <svg width="28" height="28" viewBox="0 0 32 32" fill="none" aria-hidden>
      <rect x="1" y="1" width="30" height="30" rx="9" fill="#3987e5" fillOpacity=".14" stroke="#3987e5" strokeOpacity=".5" />
      <path d="M9 22 L16 9 L23 22 Z M9 22 L23 22" stroke="#86b6ef" strokeWidth="1.6" strokeLinejoin="round" />
      <circle cx="16" cy="9" r="2.6" fill="#3987e5" /><circle cx="9" cy="22" r="2.6" fill="#199e70" /><circle cx="23" cy="22" r="2.6" fill="#d95926" />
    </svg>
  );
}
