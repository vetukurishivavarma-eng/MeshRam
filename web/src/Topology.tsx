import { nodeColor, rate } from "./ui";

/** Live cluster map. ggml-rpc routes every tensor through the coordinator (host), so the real data path is a
 * star: host -> stage 1 -> host -> stage 2 ... Links animate only when bytes actually move through a tunnel. */
export function Topology({ nodes, deployment, colorOf }: { nodes: any[]; deployment: any; colorOf: (id: string) => number }) {
  const W = 640, H = 340, cx = W / 2, cy = H / 2;
  const R = Math.min(W * 0.36, H * 0.38);
  const parts: Record<string, any> = {};
  (deployment?.state === "ready" || deployment?.state === "loading" ? deployment.parts : []).forEach((p: any, i: number) => (parts[p.node_id] = { ...p, stage: i + 1 }));
  const list = nodes.filter((n) => n.online || parts[n.id]);
  const pos = list.map((n, i) => {
    const a = -Math.PI / 2 + (i / Math.max(list.length, 1)) * Math.PI * 2 + (list.length === 2 ? Math.PI / 2 : 0);
    return { n, x: cx + Math.cos(a) * R * 1.35, y: cy + Math.sin(a) * R };
  });
  const maxRate = Math.max(1, ...list.map((n) => (n.traffic ? n.traffic.tx_bps + n.traffic.rx_bps : 0)));
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="h-full max-h-[360px] w-full">
      <defs>
        <radialGradient id="hub" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stopColor="#3987e5" stopOpacity=".35" />
          <stop offset="100%" stopColor="#3987e5" stopOpacity="0" />
        </radialGradient>
      </defs>
      {[1, 0.66].map((f) => <ellipse key={f} cx={cx} cy={cy} rx={R * 1.35 * f} ry={R * f} fill="none" stroke="#fff" strokeOpacity={0.035} />)}
      {pos.map(({ n, x, y }) => {
        const t = n.traffic ? n.traffic.tx_bps + n.traffic.rx_bps : 0;
        const c = nodeColor(colorOf(n.id));
        const active = t > 0;
        return (
          <g key={n.id + "-l"}>
            <line x1={cx} y1={cy} x2={x} y2={y} stroke={n.online ? c : "#d03b3b"} strokeOpacity={n.online ? 0.22 : 0.5}
              strokeWidth={1.5} strokeDasharray={n.online ? undefined : "3 5"} />
            {active && <line x1={cx} y1={cy} x2={x} y2={y} stroke={c} strokeWidth={1.5 + (t / maxRate) * 3} className="flow" strokeLinecap="round" />}
            {active && (
              <text x={(cx + x) / 2} y={(cy + y) / 2 - 8} textAnchor="middle" className="num" fontSize="11" fill="#c3c2b7">
                {rate(t)}
              </text>
            )}
          </g>
        );
      })}
      <circle cx={cx} cy={cy} r={58} fill="url(#hub)" />
      <circle cx={cx} cy={cy} r={30} fill="#161615" stroke="#3987e5" strokeWidth={1.5} />
      <text x={cx} y={cy - 2} textAnchor="middle" fontSize="11" fontWeight={600} fill="#f0efec">host</text>
      <text x={cx} y={cy + 12} textAnchor="middle" fontSize="9" fill="#8a897f">{deployment?.state === "ready" ? "embed · head" : "coordinator"}</text>
      {pos.map(({ n, x, y }) => {
        const c = nodeColor(colorOf(n.id));
        const p = parts[n.id];
        const st = n.status || {};
        return (
          <g key={n.id} className="fade-in">
            <circle cx={x} cy={y} r={24} fill="#161615" stroke={n.online ? c : "#d03b3b"} strokeWidth={p ? 2.5 : 1.5} />
            <circle cx={x} cy={y} r={5} fill={n.online ? c : "#d03b3b"} />
            {p && (
              <g>
                <circle cx={x + 18} cy={y - 18} r={9} fill={c} />
                <text x={x + 18} y={y - 14.5} textAnchor="middle" fontSize="10" fontWeight={700} fill="#0e0e0d">{p.stage}</text>
              </g>
            )}
            <text x={x} y={y + 40} textAnchor="middle" fontSize="12" fontWeight={600} fill="#f0efec">{n.name.replace(" (coordinator)", "")}</text>
            <text x={x} y={y + 54} textAnchor="middle" fontSize="10" fill="#8a897f" className="num">
              {!n.online ? "offline" : st.paused ? "paused" : p ? `layers ${p.start}–${p.end - 1}` : n.rtt_ms != null ? `${n.rtt_ms} ms` : "online"}
            </text>
          </g>
        );
      })}
      {list.length === 0 && <text x={cx} y={cy + 60} textAnchor="middle" fontSize="12" fill="#8a897f">no nodes online</text>}
    </svg>
  );
}
