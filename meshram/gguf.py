"""Minimal GGUF header reader: metadata + exact per-layer tensor bytes (no weights loaded)."""
import os
import re
import struct

_SCALAR = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d"}
FILE_TYPES = {0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 7: "Q8_0", 8: "Q5_0", 9: "Q5_1", 10: "Q2_K",
              11: "Q3_K_S", 12: "Q3_K_M", 13: "Q3_K_L", 14: "Q4_K_S", 15: "Q4_K_M", 16: "Q5_K_S",
              17: "Q5_K_M", 18: "Q6_K", 19: "IQ2_XXS", 20: "IQ2_XS", 24: "IQ1_S", 25: "IQ4_NL",
              26: "IQ3_S", 28: "IQ4_XS", 32: "BF16"}
MiB = 1024 * 1024


class _R:
    def __init__(self, f):
        self.f = f

    def unpack(self, fmt):
        return struct.unpack(fmt, self.f.read(struct.calcsize(fmt)))[0]

    def string(self):
        n = self.unpack("<Q")
        if n > 1 << 24:
            raise ValueError("bad gguf string length")
        return self.f.read(n).decode("utf-8", "replace")

    def value(self, t, keep_arrays=False):
        if t in _SCALAR:
            return self.unpack(_SCALAR[t])
        if t == 8:
            return self.string()
        if t == 9:
            et, n = self.unpack("<I"), self.unpack("<Q")
            if et in _SCALAR and not keep_arrays:  # skip big arrays (tokenizer) without parsing
                self.f.seek(struct.calcsize(_SCALAR[et]) * n, 1)
                return None
            vals = [self.value(et) for _ in range(n)]
            return vals if keep_arrays else None
        raise ValueError(f"unknown gguf type {t}")


def read(path):
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if f.read(4) != b"GGUF":
            raise ValueError("not a GGUF file")
        r = _R(f)
        version, n_tensors, n_kv = r.unpack("<I"), r.unpack("<Q"), r.unpack("<Q")
        meta = {}
        for _ in range(n_kv):
            k = r.string()
            meta[k] = r.value(r.unpack("<I"))
        tensors = []
        for _ in range(n_tensors):
            name = r.string()
            nd = r.unpack("<I")
            dims = [r.unpack("<Q") for _ in range(nd)]
            if name == "token_embd.weight":
                meta["_vocab"] = dims[-1]
            r.unpack("<I")
            tensors.append((name, r.unpack("<Q")))
        align = meta.get("general.alignment", 32)
        data_start = (f.tell() + align - 1) // align * align

    # tensor byte sizes from offset gaps (exact, includes alignment padding)
    tensors.sort(key=lambda t: t[1])
    sizes = {}
    for i, (name, off) in enumerate(tensors):
        end = tensors[i + 1][1] if i + 1 < len(tensors) else size - data_start
        sizes[name] = end - off

    arch = meta.get("general.architecture", "llama")
    n_layer = meta.get(f"{arch}.block_count", 0)
    layer_bytes = [0] * n_layer
    other = {}
    for name, b in sizes.items():
        m = re.match(r"blk\.(\d+)\.", name)
        if m and int(m.group(1)) < n_layer:
            layer_bytes[int(m.group(1))] += b
        else:
            other[name] = b

    emb = meta.get(f"{arch}.embedding_length", 0)
    heads = meta.get(f"{arch}.attention.head_count", 1) or 1
    kv_heads = meta.get(f"{arch}.attention.head_count_kv", heads) or heads
    k_len = meta.get(f"{arch}.attention.key_length", emb // heads if heads else 0)
    v_len = meta.get(f"{arch}.attention.value_length", k_len)
    output = other.get("output.weight", other.get("token_embd.weight", 0))  # tied embeddings reuse token_embd
    return {
        "path": path,
        "name": meta.get("general.name") or os.path.basename(path),
        "file": os.path.basename(path),
        "size_bytes": size,
        "arch": arch,
        "quant": FILE_TYPES.get(meta.get("general.file_type"), f"type {meta.get('general.file_type')}"),
        "n_layer": n_layer,
        "layer_bytes": layer_bytes,
        "embd_bytes": other.get("token_embd.weight", 0),
        "output_bytes": output + other.get("output_norm.weight", 0),
        "train_ctx": meta.get(f"{arch}.context_length", 0),
        # f16 K+V per token per layer
        "kv_bytes_per_token_layer": kv_heads * (k_len + v_len) * 2,
        "n_embd": emb,
        "n_head": heads,
        "n_ff": meta.get(f"{arch}.feed_forward_length", 4 * emb),
        "n_vocab": meta.get("_vocab", 32000),
        "version": version,
    }
