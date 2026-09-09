#!/usr/bin/env python3
"""Scan an ONNX model for attention subgraphs (Phase 0 recon).

Finds `Softmax -> MatMul(probs, V)` anchored patterns, walks back through
Mul/Div (scale) and Add (mask), and reports shape info where inferable.
Requires: python3 + onnx package (pip install onnx).

Usage:
    python3 scan_attention.py --model model4.onnx [--batch 32]
"""
import argparse
import sys

try:
    import onnx
    from onnx import shape_inference
except ImportError:
    sys.exit("需要 onnx 包: pip install onnx (或系统 python3-onnx)")


def tensor_shape(graph, name):
    for vi in list(graph.input) + [v for n in graph.node for v in n.output if v]:
        pass  # placeholder, real lookup below
    for vi in graph.value_info:
        if vi.name == name:
            return [d.dim_value if d.HasField("dim_value") else d.dim_param or "?" for d in vi.type.tensor_type.shape.dim]
    for out in graph.output:
        if out.name == name:
            return [d.dim_value if d.HasField("dim_value") else d.dim_param or "?" for d in out.type.tensor_type.shape.dim]
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--batch", type=int, default=None, help="替换 shape 推断中的动态 batch")
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
        # walk back through Add(mask) and Mul/Div(scale) to the QK MatMul
        cur = node.input[0]
        mask_shape = None
        for _ in range(2):
            p = producer.get(cur)
            if p is None:
                break
            if p.op_type == "Add":
                other = p.input[1] if p.input[0] == cur else p.input[0]
                mask_shape = shape_of(other)
                cur = p.input[0] if p.input[0] != cur else p.input[1]
            elif p.op_type in ("Mul", "Div"):
                # scale side must be a constant initializer; qk side must be MatMul
                a, b = p.input
                if a in initializers:
                    cur = b
                elif b in initializers:
                    cur = a
                else:
                    cur = None
                    break
            else:
                break
        qk = producer.get(cur) if cur else None
        if qk is None or qk.op_type != "MatMul":
            continue
        shared = len(consumers.get(cur, [])) != 1
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
            print("    !! scores 有其他消费者(共享输出,不可融合)")
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
