"""Model placement: which contiguous layer range goes to which node, within each node's RAM budget.

Pipeline (layer) split only: a single request flows through nodes sequentially, so using FEWER nodes is
faster (fewer network hops). Default strategy "min-nodes" therefore uses the smallest set of nodes that fits.
"""
MARGIN = 1.10  # 10% headroom on every estimate


def node_cost(model, a, b, ctx, ubatch, last):
    # `last` = this node also holds the output head (vocab projection) + logits buffer
    """Estimated bytes a node needs to hold layers [a, b)."""
    n = b - a
    weights = sum(model["layer_bytes"][a:b])
    kv = n * ctx * model["kv_bytes_per_token_layer"]
    # activations for one micro-batch (empirically ~ matches llama.cpp compute buffers within ~15%)
    compute = ubatch * 4 * (3 * model["n_ff"] + 4 * model["n_embd"]) + model["n_head"] * ctx * ubatch * 4
    if last:  # output head + logits buffer live on the last device
        weights += model["output_bytes"]
        compute += model["n_vocab"] * ubatch * 4
    return int((weights + kv + compute) * MARGIN), {"weights": weights, "kv": kv, "compute": compute}


def tensor_split(counts):
    """llama.cpp -ts values that land boundaries exactly on our layer counts (-ngl 999: n_layer+1 slots,
    the last slot is the output layer). llama.cpp maps slot il to the first device whose cumulative
    fraction > il/(n_layer+1); offsetting each boundary by half a layer avoids float ties."""
    if len(counts) == 1:
        return [1.0]
    w = [counts[0] - 0.5] + list(counts[1:-1]) + [counts[-1] + 1.5]
    return [round(x, 3) for x in w]


def _greedy(model, nodes, ctx, ub, head):
    L = model["n_layer"]
    out, a = [], 0
    for i, nd in enumerate(nodes):
        last = i == len(nodes) - 1
        if last:
            b = L
        else:
            b = a
            while b < L and node_cost(model, a, b + 1, ctx, ub, False)[0] <= nd["budget"]:
                b += 1
            if b == L:  # everything fit here; this node is effectively last
                b = L - 1 if L - a > 1 else L
        if b <= a and not last:
            continue
        cost, parts = node_cost(model, a, b, ctx, ub, head and b == L)
        out.append({"node_id": nd["id"], "name": nd.get("name", nd["id"]), "start": a, "end": b,
                    "est_bytes": cost, "budget": nd["budget"], "parts": parts})
        a = b
        if a == L:
            break
    ok = a == L and all(x["est_bytes"] <= x["budget"] for x in out) and all(x["end"] > x["start"] for x in out)
    return ok, out


def _spread(model, nodes, ctx, ub, head):
    L = model["n_layer"]
    k = min(len(nodes), L)
    nodes = nodes[:k]
    tot = sum(n["budget"] for n in nodes)
    raw = [L * n["budget"] / tot for n in nodes]
    counts = [max(1, int(r)) for r in raw]
    while sum(counts) < L:
        i = max(range(k), key=lambda j: raw[j] - counts[j])
        counts[i] += 1
    while sum(counts) > L:
        i = max(range(k), key=lambda j: counts[j] - raw[j])
        counts[i] -= 1
    out, a = [], 0
    for i, (nd, c) in enumerate(zip(nodes, counts)):
        b = a + c
        cost, parts = node_cost(model, a, b, ctx, ub, head and i == k - 1)
        out.append({"node_id": nd["id"], "name": nd.get("name", nd["id"]), "start": a, "end": b,
                    "est_bytes": cost, "budget": nd["budget"], "parts": parts})
        a = b
    return all(x["est_bytes"] <= x["budget"] for x in out), out


def plan(model, nodes, ctx=2048, ubatch=256, strategy="min-nodes", head="host"):
    """nodes: [{id, name, budget (bytes)}]. Returns a placement or an honest reason it can't fit.

    head="host": output head + logits stay on the coordinator. Per token only one hidden state
    (n_embd floats) crosses each link instead of n_vocab logits (e.g. 3.5 KB vs 600 KB for Qwen2.5).
    Costs the coordinator machine output_bytes + logits RAM."""
    nodes = [n for n in nodes if n["budget"] > 0]
    on_dev = head == "last"
    full_single = node_cost(model, 0, model["n_layer"], ctx, ubatch, on_dev)[0]
    best_single = max((n["budget"] for n in nodes), default=0)
    res = {"ok": False, "need_bytes": full_single, "largest_node_budget": best_single,
           "fits_single_node": full_single <= best_single, "assignments": [], "strategy": strategy,
           "ctx": ctx, "ubatch": ubatch, "head": head,
           "host_bytes": model["embd_bytes"] + (0 if on_dev else model["output_bytes"] + model["n_vocab"] * ubatch * 4)}
    if not nodes:
        res["reason"] = "no online nodes with RAM contribution available"
        return res
    by_budget = sorted(nodes, key=lambda n: n["budget"], reverse=True)
    if strategy == "spread" and len(nodes) > 1:
        # biggest node last: it carries output head + logits
        ok, out = _spread(model, by_budget[1:] + by_budget[:1], ctx, ubatch, on_dev)
        if not ok:
            ok, out = _greedy(model, by_budget[1:] + by_budget[:1], ctx, ubatch, on_dev)
    else:
        ok, out = False, []
        for k in range(1, len(by_budget) + 1):
            sel = by_budget[:k]
            ok, out = _greedy(model, sel[1:] + sel[:1], ctx, ubatch, on_dev)
            if ok:
                break
    if ok:
        res.update(ok=True, assignments=out, tensor_split=tensor_split([x["end"] - x["start"] for x in out]))
    else:
        have = sum(n["budget"] for n in nodes)
        res["reason"] = (f"cluster cannot fit model: needs ~{full_single / 2**20:.0f} MiB "
                         f"(+ per-node overhead), contributed {have / 2**20:.0f} MiB across {len(nodes)} node(s)")
    return res
