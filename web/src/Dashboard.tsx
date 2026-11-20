import { useEffect, useMemo, useRef, useState } from "react";
import { Topology } from "./Topology";
import { ago, api, Btn, bytes, Field, gib, inputCls, Logo, Meter, mib, nodeColor, Panel, Pill, rate, Spark, Stat, usePoll } from "./ui";

const STATUS_TONE: Record<string, any> = {
  "no nodes": "neutral", idle: "neutral", "model ready": "good", "inference running": "accent", "loading model": "warn", degraded: "critical",
};

export default function Dashboard() {
  const [s, err] = usePoll<any>("/api/state", 1000);
  const [models] = usePoll<any[]>("/api/models", 10000);
  const colorIdx = useMemo(() => {
    const m: Record<string, number> = {};
    (s?.nodes || []).forEach((n: any, i: number) => (m[n.id] = i));
    return m;
  }, [s?.nodes?.map((n: any) => n.id).join()]);
  const colorOf = (id: string) => colorIdx[id] ?? 99;

  if (!s) return <div className="grid h-screen place-items-center text-ink-3">{err ? `Coordinator unreachable: ${err}` : "Connecting…"}</div>;
  const sum = s.summary;
  return (
    <div className="mx-auto max-w-[1440px] px-4 pb-16 sm:px-6">
      <Header s={s} />
      {err && <div className="mb-4 rounded-lg bg-critical/10 px-4 py-2 text-sm text-[#f08a8a] ring-1 ring-critical/40">✕ Lost connection to coordinator: {err}</div>}
      <Pending pending={s.pending} />
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-7">
        <Stat label="Computers" value={<>{sum.nodes_online}<span className="text-ink-3">/{sum.nodes_total}</span></>} sub={sum.paused ? `${sum.paused} paused by owner` : "online / registered"} />
        <Stat label="Physical RAM" value={gib(sum.ram_total)} sub="across online nodes" />
        <Stat label="Contributed" value={gib(sum.ram_contributed)} sub={`${gib(sum.ram_budget_now, 2)} free to allocate now`} />
        <Stat label="Allocated" value={gib(sum.ram_allocated, 2)} sub={`${gib(sum.ram_in_use, 2)} actually in use`} />
        <Stat label="CPU" value={`${sum.cpu_logical} thr`} sub={`avg load ${sum.cpu_avg}%`} />
        <Stat label="GPUs" value={sum.gpu_count} sub={sum.vram_total ? `${gib(sum.vram_total)} VRAM (driver-reported)` : "none detected"} />
        <Stat label="Active tasks" value={sum.active_tasks} sub={s.deployment ? `${s.deployment.kind}` : "no workload"} />
      </div>

      <div className="mt-4 grid gap-4 xl:grid-cols-3">
        <Panel title="Cluster map" className="xl:col-span-2" right={<span className="text-xs text-ink-3">links animate only when tensor bytes move</span>}>
          <Topology nodes={s.nodes} deployment={s.deployment} colorOf={colorOf} />
        </Panel>
        <div className="grid gap-4">
          <Panel title="Cluster CPU"><Spark data={s.history} series={[{ key: "cpu", color: "#3987e5", label: "avg CPU" }]} fmt={(v) => `${v.toFixed(0)}%`} max={100} /></Panel>
          <Panel title="Cluster RAM"><Spark data={s.history} series={[{ key: "allocated", color: "#3987e5", label: "allocated" }, { key: "in_use", color: "#d95926", label: "in use" }]} fmt={(v) => gib(v, 2)} /></Panel>
          <Panel title="Tensor traffic"><Spark data={s.history} series={[{ key: "tx", color: "#3987e5", label: "to nodes" }, { key: "rx", color: "#d95926", label: "from nodes" }]} fmt={(v) => rate(v)} /></Panel>
        </div>
      </div>

      <h2 className="mb-3 mt-8 text-[13px] font-semibold uppercase tracking-[0.08em] text-ink-2">Computers</h2>
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {s.nodes.map((n: any) => <NodeCard key={n.id} n={n} color={nodeColor(colorOf(n.id))} deployment={s.deployment} />)}
        {s.nodes.length === 0 && <JoinHelp s={s} />}
      </div>

      <div className="mt-8 grid gap-4 xl:grid-cols-5">
        <Models models={models || []} s={s} colorOf={colorOf} className="xl:col-span-3" />
        <Chat s={s} className="xl:col-span-2" />
      </div>
      <Bench s={s} models={models || []} />
      <Events events={s.events} />
    </div>
  );
}

function Header({ s }: { s: any }) {
  const st = s.summary.status;
  return (
    <header className="flex flex-wrap items-center justify-between gap-4 py-6">
      <div className="flex items-center gap-3">
        <Logo />
        <div>
          <div className="text-lg font-semibold tracking-tight">MeshRAM</div>
          <div className="text-xs text-ink-3">Computers that work together as one AI machine</div>
        </div>
        <span className="ml-2"><Pill tone={STATUS_TONE[st] || "neutral"}>{st}</Pill></span>
      </div>
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <div className="panel flex items-center gap-3 px-3 py-2">
          <span className="text-ink-3">Pairing code</span>
          <span className="num font-mono text-base font-semibold tracking-[0.2em]">{s.pairing_code}</span>
          <button className="text-ink-3 hover:text-ink" title="Generate a new code" onClick={() => api("/api/pairing/rotate", {})}>↻</button>
        </div>
        <div className="panel px-3 py-2"><span className="text-ink-3">Fingerprint </span><span className="font-mono">{s.fingerprint}</span></div>
        <div className="panel px-3 py-2 font-mono text-ink-2">{(s.lan_ips[0] || "?")}:{s.node_port}</div>
      </div>
    </header>
  );
}

function Pending({ pending }: { pending: any[] }) {
  if (!pending.length) return null;
  return (
    <div className="mb-4 space-y-2">
      {pending.map((p) => (
        <div key={p.id} className="panel fade-in flex flex-wrap items-center justify-between gap-3 border-warn/40 px-4 py-3">
          <div className="text-sm">
            <Pill tone="warn">approval needed</Pill>
            <span className="ml-3 font-semibold">{p.name}</span>
            <span className="ml-2 text-ink-3">{p.ip} · {p.hw?.os} · {p.hw?.cpu_logical} threads · {gib(p.hw?.ram_total)}</span>
            <div className="mt-1 text-xs text-ink-2">
              Approve only if this fingerprint matches the one printed on that computer:{" "}
              <span className="font-mono text-base text-ink">{p.fp}</span>
            </div>
          </div>
          <div className="flex gap-2">
            <Btn onClick={() => api(`/api/pending/${p.id}/reject`, {})}>Reject</Btn>
            <Btn kind="primary" onClick={() => api(`/api/pending/${p.id}/approve`, {})}>Approve</Btn>
          </div>
        </div>
      ))}
    </div>
  );
}

function JoinHelp({ s }: { s: any }) {
  return (
    <div className="panel p-5 text-sm text-ink-2 md:col-span-2">
      <div className="mb-2 font-semibold text-ink">Add a computer</div>
      On the other computer (same Wi-Fi) run:
      <pre className="mt-2 overflow-x-auto rounded-lg bg-black/40 p-3 font-mono text-xs text-ink">python -m meshram worker --coordinator {s.lan_ips[0] || "<this-ip>"} --code {s.pairing_code}</pre>
      Then approve it here after comparing fingerprints.
    </div>
  );
}

function NodeCard({ n, color, deployment }: { n: any; color: string; deployment: any }) {
  const st = n.status;
  const m = st?.memory;
  const snap = st?.snap;
  const [mb, setMb] = useState(256);
  const [busy, setBusy] = useState("");
  const [confirm, setConfirm] = useState(false);
  const [msg, setMsg] = useState("");
  const part = deployment?.parts?.find((p: any) => p.node_id === n.id);
  const run = async (what: string, fn: () => Promise<any>) => {
    setBusy(what); setMsg("");
    try { await fn(); } catch (e: any) { setMsg(e.message); } finally { setBusy(""); }
  };
  const tone = !n.online ? "critical" : st?.paused ? "warn" : st?.task ? "accent" : "good";
  const label = !n.online ? "offline" : st?.paused ? "paused" : st?.task ? "working" : "online";
  return (
    <div className="panel fade-in relative overflow-hidden p-5">
      <div className="absolute inset-x-0 top-0 h-[3px]" style={{ background: n.online ? color : "#2a2a27" }} />
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 font-semibold"><span className="h-2.5 w-2.5 rounded-full" style={{ background: color }} />{n.name}</div>
          <div className="mt-0.5 text-xs text-ink-3">{n.static?.os || "—"} · {n.ip || "—"} · <span className="font-mono">{n.fingerprint}</span></div>
        </div>
        <Pill tone={tone as any}>{label}</Pill>
      </div>
      {st?.paused && <div className="mt-3 rounded-md bg-warn/10 px-3 py-1.5 text-xs text-warn">▲ {st.paused}</div>}

      {m ? (
        <div className="mt-4">
          <div className="mb-1.5 flex justify-between text-xs"><span className="text-ink-2">Memory</span><span className="num text-ink-3">{gib(m.total)}</span></div>
          <Meter total={m.total} segs={[
            { v: m.used - m.in_use, c: "#5a5953", label: "Owner & OS" },
            { v: m.in_use, c: color, label: "Cluster in use" },
            { v: Math.max(0, m.allocated - m.in_use), c: color + "66", label: "Cluster allocated, not yet used" },
          ]} />
          <dl className="num mt-3 grid grid-cols-3 gap-x-3 gap-y-2 text-xs">
            <KV k="Available" v={gib(m.available)} />
            <KV k="Reserved for owner" v={gib(m.host_reserve)} />
            <KV k="Contribution cap" v={gib(m.contribution_cap)} />
            <KV k="Allocated" v={mib(m.allocated)} />
            <KV k="In use (RSS)" v={mib(m.in_use)} />
            <KV k="Allocatable now" v={gib(m.budget, 2)} />
          </dl>
        </div>
      ) : <div className="mt-4 text-sm text-ink-3">{n.online ? "waiting for first metrics…" : `last seen at ${n.ip || "?"}`}</div>}

      {snap && (
        <dl className="num mt-4 grid grid-cols-3 gap-x-3 gap-y-2 border-t border-line pt-3 text-xs">
          <KV k="CPU" v={`${snap.cpu_pct.toFixed(0)}% · ${n.static?.cpu_logical} thr`} />
          <KV k="Cluster CPU" v={`${snap.cluster_cpu_pct}% (max ${st.policy.max_cpu_pct}%)`} />
          <KV k="Temp" v={snap.temp_c != null ? `${snap.temp_c}°C` : "n/a on this OS"} />
          <KV k="RTT" v={n.rtt_ms != null ? `${n.rtt_ms} ms` : "—"} />
          <KV k="Link (measured)" v={n.net ? `${n.net.mbit_s} Mbit/s` : "—"} />
          <KV k="Power" v={snap.charging == null ? "AC / no battery" : `${snap.battery_pct}% ${snap.charging ? "charging" : "battery"}`} />
          <KV k="GPU" v={(n.static?.gpus || []).map((g: any) => g.name).join(", ") || "none"} wide />
          <KV k="GPU used" v={st.policy.allow_gpu ? (st.gpu_backend ? "allowed" : "allowed, runtime is CPU-only") : "not allowed by owner"} />
        </dl>
      )}
      {n.online && n.rtt_ms != null && n.rtt_ms > 25 && (
        <div className="mt-3 rounded-md bg-warn/10 px-3 py-1.5 text-xs text-warn">
          ▲ {n.rtt_ms} ms round trip (internet / mobile link): every token waits on this. Expect a few tok/s; each model load re-sends this node's layers over the link.
        </div>
      )}
      {part && <div className="mt-3 rounded-md px-3 py-1.5 text-xs" style={{ background: color + "1a", color }}>● serving layers {part.start}–{part.end - 1} of {deployment.model}</div>}
      {st?.task && !part && <div className="mt-3 text-xs text-ink-2">Task: {st.task}</div>}
      {n.alloc && <div className={`mt-2 text-xs ${n.alloc.ok ? "text-ink-2" : "text-serious"}`}>
        {n.alloc.ok ? `Memory test: ${mib(n.alloc.bytes)} committed in ${n.alloc.alloc_seconds}s (${n.alloc.gb_per_s} GiB/s), RSS +${mib(n.alloc.rss_delta)}, released` : `◆ Memory test refused: ${n.alloc.error}`}
      </div>}
      {msg && <div className="mt-2 text-xs text-serious">◆ {msg}</div>}

      {n.online && (
        <div className="mt-4 flex flex-wrap items-center gap-2">
          <Btn disabled={!!busy} onClick={() => run("net", () => api(`/api/nodes/${n.id}/nettest`, {}))}>{busy === "net" ? "Testing…" : "Network test"}</Btn>
          <div className="flex items-center gap-1">
            <input type="number" min={16} step={64} value={mb} onChange={(e) => setMb(+e.target.value)} className={inputCls + " !w-20 !py-1.5"} />
            <Btn disabled={!!busy} onClick={() => run("mem", () => api(`/api/nodes/${n.id}/alloctest`, { mb }))}>{busy === "mem" ? "Allocating…" : "MiB test"}</Btn>
          </div>
          {!n.local && (confirm
            ? <><Btn kind="danger" onClick={() => api(`/api/nodes/${n.id}/revoke`, {})}>Confirm remove</Btn><Btn onClick={() => setConfirm(false)}>Cancel</Btn></>
            : <Btn kind="danger" onClick={() => setConfirm(true)}>Remove</Btn>)}
        </div>
      )}
      {!n.online && !n.local && <div className="mt-4"><Btn kind="danger" onClick={() => api(`/api/nodes/${n.id}/revoke`, {})}>Remove</Btn></div>}
    </div>
  );
}

function KV({ k, v, wide }: { k: string; v: any; wide?: boolean }) {
  return <div className={wide ? "col-span-2" : ""}><dt className="text-ink-3">{k}</dt><dd className="truncate text-ink" title={String(v)}>{v}</dd></div>;
}

function Models({ models, s, colorOf, className }: { models: any[]; s: any; colorOf: (id: string) => number; className?: string }) {
  const [file, setFile] = useState<string>("");
  const [ctx, setCtx] = useState(2048);
  const [strategy, setStrategy] = useState("min-nodes");
  const [sel, setSel] = useState<string[]>([]);
  const [plan, setPlan] = useState<any>(null);
  const [msg, setMsg] = useState("");
  const online = s.nodes.filter((n: any) => n.online);
  useEffect(() => { if (!file && models[0]?.file) setFile(models[0].file); }, [models]);
  useEffect(() => {
    if (!file) return;
    api("/api/plan", { model: file, ctx, strategy, nodes: sel.length ? sel : null }).then(setPlan).catch((e) => setMsg(e.message));
  }, [file, ctx, strategy, sel.join(), s.summary.ram_budget_now >> 26]);
  const d = s.deployment;
  const model = models.find((m) => m.file === file);
  return (
    <Panel title="Models & placement" className={className}
      right={d && ["ready", "loading", "starting"].includes(d.state) ? <Btn kind="danger" onClick={() => api("/api/undeploy", {})}>Stop model</Btn> : null}>
      {models.length === 0 && <div className="text-sm text-ink-3">No .gguf files in <span className="font-mono">models/</span>. Download one (see README) and it appears here.</div>}
      <div className="space-y-1.5">
        {models.map((m) => (
          <button key={m.file} onClick={() => setFile(m.file)}
            className={`flex w-full items-center justify-between gap-3 rounded-lg px-3 py-2.5 text-left text-sm ring-1 transition ${file === m.file ? "bg-accent/10 ring-accent/50" : "ring-white/5 hover:bg-white/[0.03]"}`}>
            <span className="min-w-0">
              <span className="block truncate font-medium">{m.name}</span>
              <span className="block truncate font-mono text-[11px] text-ink-3">{m.file}</span>
            </span>
            <span className="num shrink-0 text-right text-xs text-ink-2">
              {m.quant} · {m.n_layer} layers · {bytes(m.size_bytes)}<br /><span className="text-ink-3">needs ~{bytes(m.est_bytes_2k)} @2k ctx</span>
            </span>
          </button>
        ))}
      </div>
      {model && (
        <>
          <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-3">
            <Field label="Context"><select className={inputCls} value={ctx} onChange={(e) => setCtx(+e.target.value)}>{[512, 1024, 2048, 4096, 8192, 16384].map((c) => <option key={c}>{c}</option>)}</select></Field>
            <Field label="Placement">
              <select className={inputCls} value={strategy} onChange={(e) => setStrategy(e.target.value)}>
                <option value="min-nodes">Fewest nodes (fastest)</option><option value="spread">Spread across nodes</option>
              </select>
            </Field>
            <Field label="Nodes">
              <div className="flex flex-wrap gap-1">
                {online.map((n: any) => {
                  const on = !sel.length || sel.includes(n.id);
                  return <button key={n.id} onClick={() => setSel(on && sel.length ? sel.filter((x) => x !== n.id) : sel.length ? [...sel, n.id] : online.map((o: any) => o.id).filter((x: string) => x !== n.id))}
                    className={`rounded-md px-2 py-1 text-xs ring-1 ${on ? "ring-white/20" : "opacity-40 ring-white/5"}`}>
                    <span className="mr-1 inline-block h-2 w-2 rounded-full" style={{ background: nodeColor(colorOf(n.id)) }} />{n.name.replace(" (coordinator)", "")}</button>;
                })}
              </div>
            </Field>
          </div>
          {plan && <PlanView plan={plan} model={model} colorOf={colorOf} />}
          <div className="mt-4 flex items-center gap-3">
            <Btn kind="primary" disabled={!plan?.ok || d?.state === "loading"} onClick={async () => {
              setMsg("");
              try { await api("/api/deploy", { model: file, ctx, strategy, nodes: sel.length ? sel : null }); } catch (e: any) { setMsg(e.message); }
            }}>Load model on cluster</Btn>
            {msg && <span className="text-xs text-serious">◆ {msg}</span>}
          </div>
        </>
      )}
      {d && <DeploymentView d={d} colorOf={colorOf} />}
    </Panel>
  );
}

function PlanView({ plan, model, colorOf }: { plan: any; model: any; colorOf: (id: string) => number }) {
  return (
    <div className="mt-4 rounded-xl bg-black/25 p-4 ring-1 ring-white/5">
      <div className="mb-3 flex flex-wrap items-center gap-2 text-xs">
        {plan.fits_single_node
          ? <Pill tone="neutral">fits on one node — splitting adds network hops</Pill>
          : <Pill tone="accent">no single node can hold it ({mib(plan.need_bytes)} &gt; {mib(plan.largest_node_budget)})</Pill>}
        {plan.ok ? <Pill tone="good">placement found</Pill> : <Pill tone="critical">{plan.reason}</Pill>}
      </div>
      {plan.ok && (
        <>
          <div className="flex h-8 w-full gap-[2px]">
            {plan.assignments.map((a: any) => (
              <div key={a.node_id} title={`${a.name}: layers ${a.start}-${a.end - 1}`} className="flex items-center justify-center rounded-[4px] text-[11px] font-semibold text-[#0e0e0d]"
                style={{ width: `${((a.end - a.start) / model.n_layer) * 100}%`, background: nodeColor(colorOf(a.node_id)) }}>
                {a.end - a.start > 2 && `L${a.start}–${a.end - 1}`}
              </div>
            ))}
          </div>
          <table className="num mt-3 w-full text-xs">
            <thead className="text-left text-ink-3"><tr><th className="font-normal">Node</th><th className="font-normal">Layers</th><th className="font-normal">Weights</th><th className="font-normal">KV</th><th className="font-normal">Est. total</th><th className="font-normal">Allowed</th></tr></thead>
            <tbody>
              {plan.assignments.map((a: any) => (
                <tr key={a.node_id} className="border-t border-line">
                  <td className="py-1.5"><span className="mr-1.5 inline-block h-2 w-2 rounded-full" style={{ background: nodeColor(colorOf(a.node_id)) }} />{a.name}</td>
                  <td>{a.start}–{a.end - 1}</td><td>{mib(a.parts.weights)}</td><td>{mib(a.parts.kv)}</td><td>{mib(a.est_bytes)}</td><td className="text-ink-3">{mib(a.budget)}</td>
                </tr>
              ))}
              <tr className="border-t border-line text-ink-3"><td className="py-1.5">host (embeddings + output head)</td><td>—</td><td colSpan={2} /><td>{mib(plan.host_bytes)}</td><td /></tr>
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}

function DeploymentView({ d, colorOf }: { d: any; colorOf: (id: string) => number }) {
  const tone = { ready: "good", loading: "warn", starting: "warn", failed: "critical", stopped: "neutral" }[d.state as string] || "neutral";
  return (
    <div className="mt-5 border-t border-line pt-4">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <Pill tone={tone as any}>{d.state}</Pill>
        <span className="font-medium">{d.model}</span>
        <span className="text-xs text-ink-3">{d.native ? "single machine" : `${d.parts.length} node pipeline`} · ctx {d.ctx}{d.load_seconds ? ` · loaded in ${d.load_seconds}s` : ""}</span>
      </div>
      {d.error && <div className="mt-2 rounded-md bg-critical/10 px-3 py-2 text-xs text-[#f08a8a]">✕ {d.error}</div>}
      {d.parts.some((p: any) => p.actual) && (
        <table className="num mt-3 w-full text-xs">
          <thead className="text-left text-ink-3"><tr><th className="font-normal">Actual allocation (reported by llama.cpp)</th><th className="font-normal">Weights</th><th className="font-normal">KV</th><th className="font-normal">Compute</th><th className="font-normal">vs estimate</th><th className="font-normal">Sent at load</th></tr></thead>
          <tbody>
            {d.parts.map((p: any) => {
              const a = p.actual || {};
              const tot = (a.model || 0) + (a.KV || 0) + (a.compute || 0);
              return <tr key={p.node_id} className="border-t border-line"><td className="py-1.5"><span className="mr-1.5 inline-block h-2 w-2 rounded-full" style={{ background: nodeColor(colorOf(p.node_id)) }} />{p.name} · L{p.start}–{p.end - 1}</td>
                <td>{mib(a.model)}</td><td>{mib(a.KV)}</td><td>{mib(a.compute)}</td><td className="text-ink-3">{mib(tot)} / {mib(p.est_bytes)}</td><td>{bytes(p.load_bytes)}</td></tr>;
            })}
            {d.host_actual && <tr className="border-t border-line text-ink-3"><td className="py-1.5">host</td><td>{mib(d.host_actual.model)}</td><td>{mib(d.host_actual.KV)}</td><td>{mib(d.host_actual.compute)}</td><td /><td /></tr>}
          </tbody>
        </table>
      )}
    </div>
  );
}

function Chat({ s, className }: { s: any; className?: string }) {
  const [msgs, setMsgs] = useState<{ role: string; content: string }[]>([]);
  const [input, setInput] = useState("");
  const [maxTok, setMaxTok] = useState(256);
  const [live, setLive] = useState(false);
  const [stats, setStats] = useState<any>(null);
  const [err, setErr] = useState("");
  const [tick, setTick] = useState({ n: 0, t0: 0 });
  const ctrl = useRef<AbortController | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const ready = s.deployment?.state === "ready";
  useEffect(() => { box.current?.scrollTo(0, box.current.scrollHeight); }, [msgs]);

  const send = async () => {
    if (!input.trim() || live) return;
    const next = [...msgs, { role: "user", content: input.trim() }];
    setMsgs([...next, { role: "assistant", content: "" }]); setInput(""); setErr(""); setStats(null); setLive(true);
    setTick({ n: 0, t0: performance.now() });
    ctrl.current = new AbortController();
    try {
      const r = await fetch("/api/chat", { method: "POST", signal: ctrl.current.signal, headers: { "Content-Type": "application/json", "X-MeshRAM": "1" }, body: JSON.stringify({ messages: next, max_tokens: maxTok }) });
      if (!r.ok) throw new Error((await r.json()).detail || `HTTP ${r.status}`);
      const rd = r.body!.getReader(); const dec = new TextDecoder(); let buf = "";
      while (true) {
        const { value, done } = await rd.read(); if (done) break;
        buf += dec.decode(value, { stream: true });
        const ev = buf.split("\n\n"); buf = ev.pop()!;
        for (const e of ev) {
          if (!e.startsWith("data: ")) continue;
          const j = JSON.parse(e.slice(6));
          if (j.delta) { setMsgs((m) => { const c = [...m]; c[c.length - 1] = { ...c[c.length - 1], content: c[c.length - 1].content + j.delta }; return c; }); setTick((t) => ({ ...t, n: t.n + 1 })); }
          if (j.error) setErr(j.error);
          if (j.done) setStats(j.stats);
        }
      }
    } catch (e: any) { if (e.name !== "AbortError") setErr(e.message); else setErr("cancelled — generation stopped on the cluster"); }
    finally { setLive(false); }
  };
  const liveTps = live && tick.n > 1 ? (tick.n / ((performance.now() - tick.t0) / 1000)).toFixed(1) : null;
  return (
    <Panel title="Inference" className={className} right={msgs.length ? <Btn onClick={() => { setMsgs([]); setStats(null); }}>Clear</Btn> : null}>
      <div ref={box} className="h-[340px] space-y-3 overflow-y-auto rounded-xl bg-black/25 p-3 ring-1 ring-white/5">
        {!msgs.length && <div className="grid h-full place-items-center text-center text-sm text-ink-3">{ready ? "Model ready. Every token below is computed across the cluster." : "Load a model to chat."}</div>}
        {msgs.map((m, i) => (
          <div key={i} className={`max-w-[92%] whitespace-pre-wrap rounded-xl px-3 py-2 text-sm leading-relaxed ${m.role === "user" ? "ml-auto bg-accent/15 text-ink" : "bg-white/[0.04] text-ink"}`}>
            {m.content || (live && i === msgs.length - 1 ? <span className="animate-pulse text-ink-3">▍</span> : "")}
          </div>
        ))}
      </div>
      <div className="num mt-2 flex min-h-5 flex-wrap gap-x-4 text-xs text-ink-3">
        {liveTps && <span className="text-accent">● {liveTps} tok/s live</span>}
        {stats && <><span><b className="text-ink">{stats.gen_tps}</b> tok/s</span><span>{stats.gen_tokens} tokens</span><span>prompt {stats.prompt_tps} tok/s</span><span>{stats.wall_seconds}s total</span>{stats.net_bytes_per_token != null && <span>{bytes(stats.net_bytes_per_token)}/token over network</span>}</>}
        {err && <span className="text-serious">◆ {err}</span>}
      </div>
      <div className="mt-2 flex gap-2">
        <textarea rows={2} value={input} disabled={!ready} onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }}
          placeholder={ready ? "Ask something… (Enter to send)" : "No model loaded"} className={inputCls + " resize-none"} />
        <div className="flex flex-col gap-1.5">
          {live ? <Btn kind="danger" onClick={() => ctrl.current?.abort()}>Stop</Btn> : <Btn kind="primary" disabled={!ready || !input.trim()} onClick={send}>Send</Btn>}
          <select value={maxTok} onChange={(e) => setMaxTok(+e.target.value)} className={inputCls + " !px-1.5 !py-1 text-xs"} title="max tokens">{[64, 128, 256, 512, 1024].map((x) => <option key={x}>{x}</option>)}</select>
        </div>
      </div>
    </Panel>
  );
}

function Bench({ s, models }: { s: any; models: any[] }) {
  const [runs] = usePoll<any[]>("/api/benchmarks", 4000);
  const [file, setFile] = useState("");
  const [n, setN] = useState(64);
  const [ctx, setCtx] = useState(1024);
  const [err, setErr] = useState("");
  useEffect(() => { if (!file && models[0]) setFile(models[0].file); }, [models]);
  const job = s.bench;
  const running = job && !job.finished;
  const last = runs?.[runs.length - 1];
  const maxTps = Math.max(1, ...(last?.results || []).map((r: any) => r.gen_tps || 0));
  return (
    <Panel title="Benchmarks" className="mt-4" right={<span className="text-xs text-ink-3">real runs · same greedy prompt · negative results shown as-is</span>}>
      <div className="flex flex-wrap items-end gap-3">
        <Field label="Model"><select className={inputCls} value={file} onChange={(e) => setFile(e.target.value)}>{models.map((m) => <option key={m.file}>{m.file}</option>)}</select></Field>
        <Field label="Tokens"><input type="number" className={inputCls + " !w-24"} value={n} min={8} max={1024} onChange={(e) => setN(+e.target.value)} /></Field>
        <Field label="Context"><select className={inputCls} value={ctx} onChange={(e) => setCtx(+e.target.value)}>{[512, 1024, 2048, 4096].map((c) => <option key={c}>{c}</option>)}</select></Field>
        <Btn kind="primary" disabled={running || !file} onClick={async () => { setErr(""); try { await api("/api/bench", { model: file, n_predict: n, ctx }); } catch (e: any) { setErr(e.message); } }}>
          {running ? "Running…" : "Run benchmark"}
        </Btn>
        <span className="text-xs text-ink-3">runs native → single node → distributed (replaces the loaded model)</span>
        {running && <Pill tone="warn">{job.step}</Pill>}
        {err && <span className="text-xs text-serious">◆ {err}</span>}
      </div>
      {last && (
        <div className="mt-5">
          <div className="rounded-xl bg-black/25 p-4 ring-1 ring-white/5">
            <div className="text-sm font-semibold">{last.verdict.headline}</div>
            {last.verdict.details.map((x: string, i: number) => <div key={i} className="mt-1 text-xs text-ink-2">{x}</div>)}
            <div className="mt-1 text-xs text-ink-3">{last.model} · {last.quant} · {bytes(last.size_bytes)} · {last.n_predict} tokens · {ago(last.ts)}</div>
          </div>
          <div className="mt-4 overflow-x-auto">
            <table className="num w-full min-w-[720px] text-xs">
              <thead className="text-left text-ink-3"><tr>{["Mode", "Generation speed", "Load", "Prompt", "Net / token", "Peak RSS per node", "Output = first run"].map((h) => <th key={h} className="pb-2 font-normal">{h}</th>)}</tr></thead>
              <tbody>
                {last.results.map((r: any) => (
                  <tr key={r.mode} className="border-t border-line align-top">
                    <td className="py-2 pr-3 font-medium">{r.mode}</td>
                    {r.ran && r.gen_tps ? <>
                      <td className="w-[28%] py-2 pr-3">
                        <div className="flex items-center gap-2"><div className="h-2 rounded-r-[4px] bg-accent" style={{ width: `${(r.gen_tps / maxTps) * 100}%` }} /><span className="text-ink">{r.gen_tps} tok/s</span></div>
                      </td>
                      <td>{r.load_seconds}s</td><td>{r.prompt_tps} tok/s</td><td>{r.net_bytes_per_token != null ? bytes(r.net_bytes_per_token) : "0 (local)"}</td>
                      <td>{Object.entries(r.node_samples || {}).map(([k, v]: any) => <div key={k}>{k.replace(" (coordinator)", "")}: {v.peak_rss_mib} MiB · cpu {v.cluster_cpu}%</div>)}{!r.nodes?.length && "—"}</td>
                      <td>{r.matches_first ? "✓ identical" : "✕ differs"}</td>
                    </> : <td colSpan={6} className="py-2 text-ink-3">{r.skipped ? `skipped: ${r.skipped}` : `✕ ${r.error}`}</td>}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
      {runs && runs.length > 1 && (
        <details className="mt-4 text-xs text-ink-2">
          <summary className="cursor-pointer text-ink-3">History ({runs.length} runs)</summary>
          <div className="mt-2 space-y-1">{[...runs].reverse().map((r, i) => <div key={i}>{new Date(r.ts * 1000).toLocaleString()} · {r.model} · {r.results.filter((x: any) => x.gen_tps).map((x: any) => `${x.mode.split(" (")[0]} ${x.gen_tps}`).join(" | ")} tok/s</div>)}</div>
        </details>
      )}
    </Panel>
  );
}

function Events({ events }: { events: any[] }) {
  return (
    <Panel title="Events" className="mt-4">
      <div className="max-h-64 space-y-1 overflow-y-auto font-mono text-xs">
        {[...events].reverse().map((e, i) => (
          <div key={i} className="flex gap-3">
            <span className="shrink-0 text-ink-3">{new Date(e.ts * 1000).toLocaleTimeString()}</span>
            <span className={e.level === "error" ? "text-[#f08a8a]" : e.level === "warn" ? "text-warn" : "text-ink-2"}>{e.level === "error" ? "✕ " : e.level === "warn" ? "▲ " : ""}{e.msg}</span>
          </div>
        ))}
      </div>
    </Panel>
  );
}
