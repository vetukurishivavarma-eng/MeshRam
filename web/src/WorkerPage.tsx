import { useEffect, useState } from "react";
import { api, Btn, Field, gib, inputCls, Logo, Meter, mib, Panel, Pill, Toggle, usePoll } from "./ui";

/** The owner's own page on a contributing computer: what is shared, and the limits. Loopback only. */
export default function WorkerPage() {
  const [s] = usePoll<any>("/api/local", 1500);
  const [p, setP] = useState<any>(null);
  const [msg, setMsg] = useState("");
  useEffect(() => { if (s && !p) setP(s.policy); }, [s]);
  if (!s || !p) return <div className="grid h-screen place-items-center text-ink-3">Connecting to local agent…</div>;
  const m = s.memory;
  const set = (k: string, v: any) => setP({ ...p, [k]: v });
  const dirty = JSON.stringify(p) !== JSON.stringify(s.policy);
  const save = async () => {
    try { await api("/api/local/policy", p); setMsg("saved"); } catch (e: any) { setMsg(e.message); }
  };
  return (
    <div className="mx-auto max-w-5xl px-4 pb-16 sm:px-6">
      <header className="flex flex-wrap items-center justify-between gap-4 py-6">
        <div className="flex items-center gap-3">
          <Logo />
          <div><div className="text-lg font-semibold">MeshRAM agent · {s.name}</div><div className="text-xs text-ink-3">You decide what this computer shares.</div></div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {s.paused ? <Pill tone="warn">paused: {s.paused}</Pill> : s.connected ? <Pill tone="good">connected to {s.coordinator}</Pill>
            : s.pending_pair ? <Pill tone="warn">waiting for approval at {s.pending_pair.addr}</Pill> : <Pill tone="critical">not connected</Pill>}
          <span className="panel px-3 py-1.5 text-xs"><span className="text-ink-3">Fingerprint </span><span className="font-mono">{s.fingerprint}</span></span>
        </div>
      </header>

      <Panel title="Memory on this computer">
        <Meter h={14} total={m.total} segs={[
          { v: m.used - m.in_use, c: "#5a5953", label: "You & OS" },
          { v: m.in_use, c: "#3987e5", label: "Cluster (in use)" },
          { v: Math.max(0, m.allocated - m.in_use), c: "#3987e566", label: "Cluster (allocated)" },
        ]} />
        <div className="mt-2 flex flex-wrap gap-4 text-xs text-ink-2">
          <span><span className="mr-1.5 inline-block h-2 w-2 rounded-full bg-[#5a5953]" />You & OS</span>
          <span><span className="mr-1.5 inline-block h-2 w-2 rounded-full bg-[#3987e5]" />Cluster in use</span>
          <span><span className="mr-1.5 inline-block h-2 w-2 rounded-full bg-[#3987e566]" />Cluster allocated</span>
        </div>
        <dl className="num mt-5 grid grid-cols-2 gap-4 text-sm sm:grid-cols-4">
          {[["Total physical", gib(m.total)], ["Currently used", gib(m.used)], ["Available", gib(m.available)], ["Reserved for you", gib(m.host_reserve)],
            ["Contribution limit", gib(m.contribution_cap)], ["Allocated to cluster", mib(m.allocated)], ["In use by cluster", mib(m.in_use)], ["Could allocate now", gib(m.budget, 2)]]
            .map(([k, v]) => <div key={k}><dt className="text-xs text-ink-3">{k}</dt><dd className="text-lg font-semibold">{v}</dd></div>)}
        </dl>
        <p className="mt-4 text-xs text-ink-3">
          Cluster memory is held by a llama.cpp rpc-server process under an OS-enforced limit (Windows Job Object / RLIMIT_AS); allocations beyond it fail.
          At least {gib(m.min_free)} always stays free; if you need the memory back, the agent releases it.
        </p>
        {s.rpc_running && <div className="mt-3"><Btn kind="danger" onClick={() => api("/api/local/stop", {})}>Stop contributing now</Btn></div>}
      </Panel>

      <div className="mt-4 grid gap-4 md:grid-cols-2">
        <Panel title="Your limits" right={dirty ? <Btn kind="primary" onClick={save}>Save</Btn> : msg ? <span className="text-xs text-ink-3">{msg}</span> : null}>
          <div className="space-y-4">
            <Field label={`Max RAM for the cluster: ${p.max_ram_gb} GiB`} hint={`Hard ceiling. Effective cap is also limited to total − reserve = ${gib(m.total - m.host_reserve)}.`}>
              <input type="range" min={0} max={Math.max(1, Math.floor(m.total / 2 ** 30))} step={0.25} value={p.max_ram_gb} onChange={(e) => set("max_ram_gb", +e.target.value)} className="w-full accent-[#3987e5]" />
            </Field>
            <Field label={`Max CPU: ${p.max_cpu_pct}%`} hint={`= ${Math.max(1, Math.floor((s.static.cpu_logical * p.max_cpu_pct) / 100))} of ${s.static.cpu_logical} threads, run at below-normal priority.`}>
              <input type="range" min={5} max={100} step={5} value={p.max_cpu_pct} onChange={(e) => set("max_cpu_pct", +e.target.value)} className="w-full accent-[#3987e5]" />
            </Field>
            <div className="divide-y divide-line">
              <Toggle label="Allow GPU" on={p.allow_gpu} onChange={(v) => set("allow_gpu", v)} />
              <Toggle label="Only when I'm idle" on={p.only_when_idle} onChange={(v) => set("only_when_idle", v)} />
              <Toggle label="Only while charging" on={p.only_when_charging} onChange={(v) => set("only_when_charging", v)} />
              <Toggle label="Stop when I use the computer heavily" on={p.pause_when_busy} onChange={(v) => set("pause_when_busy", v)} />
            </div>
            <div className="grid grid-cols-3 gap-3">
              <Field label="Idle minutes"><input type="number" className={inputCls} value={p.idle_minutes} onChange={(e) => set("idle_minutes", +e.target.value)} /></Field>
              <Field label="Busy CPU %"><input type="number" className={inputCls} value={p.busy_cpu_pct} onChange={(e) => set("busy_cpu_pct", +e.target.value)} /></Field>
              <Field label="Max temp °C"><input type="number" className={inputCls} value={p.max_temp_c} onChange={(e) => set("max_temp_c", +e.target.value)} /></Field>
            </div>
            <Field label="Tunnel port (LAN, TLS)" hint="Allow this TCP port in the firewall for Private networks.">
              <input type="number" className={inputCls} value={p.tunnel_port} onChange={(e) => set("tunnel_port", +e.target.value)} />
            </Field>
            {msg && msg !== "saved" && <div className="text-xs text-serious">◆ {msg}</div>}
          </div>
        </Panel>
        <div className="grid content-start gap-4">
          <Panel title="This computer">
            <dl className="num grid grid-cols-2 gap-3 text-sm">
              {[["OS", s.static.os], ["CPU", `${s.static.cpu_logical} threads`], ["CPU load", `${s.snap.cpu_pct.toFixed(0)}%`], ["Cluster CPU", `${s.snap.cluster_cpu_pct}%`],
                ["Temperature", s.snap.temp_c != null ? `${s.snap.temp_c}°C` : "not exposed by OS"], ["Power", s.snap.charging == null ? "AC / no battery" : `${s.snap.battery_pct}% ${s.snap.charging ? "charging" : "on battery"}`],
                ["Idle for", s.snap.idle_s != null ? `${Math.round(s.snap.idle_s)}s` : "unknown"], ["Weights cache", mib(s.cache_bytes)],
                ["GPU", (s.static.gpus || []).map((g: any) => g.name).join(", ") || "none"], ["Task", s.task || "—"]]
                .map(([k, v]) => <div key={k}><dt className="text-xs text-ink-3">{k}</dt><dd className="truncate">{v}</dd></div>)}
            </dl>
          </Panel>
          <Panel title="Activity">
            <div className="max-h-56 space-y-1 overflow-y-auto font-mono text-xs text-ink-2">
              {[...s.events].reverse().map((e: any, i: number) => <div key={i}><span className="text-ink-3">{new Date(e.ts * 1000).toLocaleTimeString()}</span> {e.msg}</div>)}
            </div>
          </Panel>
        </div>
      </div>
    </div>
  );
}
