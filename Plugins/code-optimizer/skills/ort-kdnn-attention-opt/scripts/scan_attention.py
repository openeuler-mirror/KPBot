#!/usr/bin/env python3
"""Scan an ONNX model for attention subgraphs (Phase 0 recon).

Finds `Softmax -> MatMul(probs, V)` anchored patterns, walks back through
Mul/Div (scale) and Add (mask), and reports shape info where inferable.
Requires: python3 + onnx package (pip install onnx).

Usage:
    python3 scan_attention.py --model model.onnx [--batch 32]
"""
import argparse
import sys

try:
    import onnx
    from onnx import shape_inference
except ImportError:
    sys.exit("需要 onnx 包: pip install onnx (或系统 python3-onnx)")


def tensor_shape(graph, name):
    for vi in list(graph.input) + list(graph.value_info) + list(graph.output):
        if vi.name == name and vi.type.tensor_type.HasField("shape"):
            return [d.dim_value if d.HasField("dim_value") else d.dim_param or "?"
                    for d in vi.type.tensor_type.shape.dim]
    for initializer in graph.initializer:
        if initializer.name == name:
            return list(initializer.dims)
    return None


def trace_scores(name, producer, scalar_constants, remaining=2):
    """Return (QK node, mask tensor) for an unambiguous standard score chain.

    Accept one additive mask and scalar Mul/Div scaling in either order.
    A reverse Div is not scaling. Ambiguous Add branches remain unclassified.
    This recognizes candidates, not the complete fusion contract.
    """
    node = producer.get(name)
    if node is None:
        return None
    if node.op_type == "MatMul":
        return node, None
    if remaining == 0 or len(node.input) != 2:
        return None
    lhs, rhs = node.input
    if node.op_type in ("Mul", "Div"):
        if rhs in scalar_constants and lhs not in scalar_constants:
            return trace_scores(lhs, producer, scalar_constants, remaining - 1)
        if node.op_type == "Mul" and lhs in scalar_constants and rhs not in scalar_constants:
            return trace_scores(rhs, producer, scalar_constants, remaining - 1)
        return None
    if node.op_type == "Add":
        matches = []
        for scores, mask in ((lhs, rhs), (rhs, lhs)):
            traced = trace_scores(scores, producer, scalar_constants, remaining - 1)
            if traced is not None and traced[1] is None:
                matches.append((traced[0], mask))
        return matches[0] if len(matches) == 1 else None
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--batch", type=int, default=None, help="仅在报告中替换首维动态 batch，不改写模型或重新推断")
    args = ap.parse_args()

    model = onnx.load(args.model)
    inferred = shape_inference.infer_shapes(model)
    graph = inferred.graph

    # index: output-name -> producing node
    producer = {}
    for node in graph.node:
        for o in node.output:
            producer[o] = node
    initializers = {i.name for i in graph.initializer}
    scalar_constants = {i.name for i in graph.initializer
                        if all(d == 1 for d in i.dims)}
    consumers = {}
    for node in graph.node:
        for i in node.input:
            consumers.setdefault(i, []).append(node)

    def shape_of(name):
        s = tensor_shape(graph, name)
        if s and args.batch is not None and s and not isinstance(s[0], int):
            s = [args.batch] + s[1:]
        return s

    print(f"model: {args.model}")
    print(f"nodes: {len(graph.node)}, initializers: {len(initializers)}, inputs: {len(graph.input)}, outputs: {len(graph.output)}")
    n_matmul = sum(1 for n in graph.node if n.op_type == "MatMul")
    n_softmax = sum(1 for n in graph.node if n.op_type == "Softmax")
    n_transpose = sum(1 for n in graph.node if n.op_type == "Transpose")
    print(f"MatMul: {n_matmul}, Softmax: {n_softmax}, Transpose: {n_transpose}")

    blocks = []
    for node in graph.node:
        if node.op_type != "Softmax":
            continue
        axis = next((a.i for a in node.attribute if a.name == "axis"), None)
        cons = consumers.get(node.output[0], []) if node.output else []
        if len(cons) != 1 or cons[0].op_type != "MatMul":
            continue
        av = cons[0]
        if av.input[0] != node.output[0]:
            continue  # probs must be slot 0
        traced = trace_scores(node.input[0], producer, scalar_constants)
        if traced is None:
            continue
        qk, mask = traced
        mask_shape = shape_of(mask) if mask is not None else None
        shared = (len(consumers.get(qk.output[0], [])) != 1 or
                  any(out.name == qk.output[0] for out in graph.output))
        blocks.append({
            "softmax": node.name or node.output[0],
            "axis": axis,
            "qk": qk.name or qk.output[0],
            "qk_shapes": (shape_of(qk.input[0]), shape_of(qk.input[1])),
            "v_shape": shape_of(av.input[1]),
            "out_shape": shape_of(av.output[0]),
            "mask_shape": mask_shape,
            "scores_shared": shared,
        })

    print(f"\nattention blocks found: {len(blocks)}")
    for i, b in enumerate(blocks):
        print(f"\n[{i}] softmax={b['softmax']} axis={b['axis']}")
        print(f"    Q shape: {b['qk_shapes'][0]}")
        print(f"    K shape: {b['qk_shapes'][1]}")
        print(f"    V shape: {b['v_shape']}")
        print(f"    out shape: {b['out_shape']}")
        print(f"    mask shape: {b['mask_shape']}")
        if b["scores_shared"]:
            print("    !! scores 被共享或作为图输出；融合需保留该结果")
        q, k = b["qk_shapes"]
        if q and len(q) == 4:
            # [B, H, Sq, d] (拆头后、进 QK 前)
            bh, sq, d = q[1], q[2], q[3]
            sk = k[3] if k and len(k) == 4 else "?"
            print(f"    -> H={bh} Sq={sq} Sk={sk} d={d}  "
                  f"{'decode/单query: GEMV 路线' if sq == 1 else 'prefill/多query: 需 Sq>1 支持'}")
        elif q and len(q) == 3:
            # [B, Sq, hidden]
            print(f"    -> rank-3 [B,Sq,hidden={q[2]}](hidden=H*d, H 需从 mask/常量推断)")
            print(f"    -> Sq={q[1]}  {'decode/单query: GEMV 路线' if q[1] == 1 else 'prefill/多query: 需 Sq>1 支持'}")
    if not blocks:
        print("(未发现标准模式;检查是否被导出器改写成非标准结构)")

if __name__ == "__main__":
    main()
