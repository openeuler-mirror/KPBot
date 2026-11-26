"""Synthetic ONNX regression cases; run with python3 -m unittest discover -s tests."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import onnx
from onnx import TensorProto, helper

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "scan_attention.py"
spec = importlib.util.spec_from_file_location("scan_attention", SCRIPT)
scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner)


def make_model(scale="Div", reverse_scale=False, mask=True, reverse_mask=False,
               constant_k=False, ambiguous=False):
    shapes = {"q": [2, 3, 1, 4], "k": [2, 3, 4, 5],
              "v": [2, 3, 5, 4], "mask": [1, 1, 1, 5]}
    inputs = [helper.make_tensor_value_info(n, TensorProto.FLOAT, shape)
              for n, shape in shapes.items() if not (constant_k and n == "k")]
    initializers = [helper.make_tensor("scale", TensorProto.FLOAT, [], [0.5])]
    if constant_k:
        initializers.append(helper.make_tensor("k", TensorProto.FLOAT,
                                               shapes["k"], [0.1] * 120))
    nodes = [helper.make_node("MatMul", ["q", "k"], ["qk"])]
    scores = "qk"
    if scale:
        args = ["scale", scores] if reverse_scale else [scores, "scale"]
        nodes.append(helper.make_node(scale, args, ["scaled"]))
        scores = "scaled"
    if mask:
        mask_name = "mask"
        if ambiguous:
            nodes.append(helper.make_node("MatMul", ["q", "k"], ["other_qk"]))
            mask_name = "other_qk"
        args = [mask_name, scores] if reverse_mask else [scores, mask_name]
        nodes.append(helper.make_node("Add", args, ["masked"]))
        scores = "masked"
    nodes.extend([helper.make_node("Softmax", [scores], ["probs"], axis=-1),
                  helper.make_node("MatMul", ["probs", "v"], ["out"])])
    graph = helper.make_graph(nodes, "attention", inputs,
                             [helper.make_tensor_value_info("out", TensorProto.FLOAT,
                                                            [2, 3, 1, 4])], initializers)
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    onnx.checker.check_model(model)
    return model


class ScanAttentionTest(unittest.TestCase):
    def scan(self, **kwargs):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.onnx"
            onnx.save(make_model(**kwargs), path)
            return subprocess.run([sys.executable, str(SCRIPT), "--model", str(path)],
                                  check=True, capture_output=True, text=True).stdout

    def test_input_initializer_and_unknown_shapes(self):
        graph = make_model(constant_k=True).graph
        self.assertEqual(scanner.tensor_shape(graph, "q"), [2, 3, 1, 4])
        self.assertEqual(scanner.tensor_shape(graph, "k"), [2, 3, 4, 5])
        self.assertEqual(scanner.tensor_shape(graph, "scale"), [])
        self.assertEqual(scanner.tensor_shape(graph, "out"), [2, 3, 1, 4])
        self.assertIsNone(scanner.tensor_shape(graph, "missing"))

    def test_dynamic_and_inferred_shapes(self):
        model = make_model()
        dim = model.graph.input[0].type.tensor_type.shape.dim[0]
        dim.ClearField("dim_value")
        dim.dim_param = "batch"
        self.assertEqual(scanner.tensor_shape(model.graph, "q"), ["batch", 3, 1, 4])
        inferred = onnx.shape_inference.infer_shapes(make_model())
        self.assertEqual(scanner.tensor_shape(inferred.graph, "qk"), [2, 3, 1, 5])

    def test_add_input_orders_and_scale_variants(self):
        for reverse_mask in (False, True):
            for scale, reverse_scale in ((None, False), ("Div", False),
                                         ("Mul", False), ("Mul", True)):
                with self.subTest(mask_reversed=reverse_mask, scale=scale,
                                  scale_reversed=reverse_scale):
                    output = self.scan(scale=scale, reverse_scale=reverse_scale,
                                       reverse_mask=reverse_mask)
                    self.assertIn("attention blocks found: 1", output)
                    self.assertIn("mask shape: [1, 1, 1, 5]", output)
                    self.assertIn("H=3 Sq=1 Sk=5 d=4", output)

    def test_constant_k_and_no_mask(self):
        output = self.scan(mask=False, constant_k=True)
        self.assertIn("attention blocks found: 1", output)
        self.assertIn("K shape: [2, 3, 4, 5]", output)
        self.assertIn("mask shape: None", output)

    def test_reverse_div_is_not_scale(self):
        for reverse_mask in (False, True):
            self.assertIn("attention blocks found: 0",
                          self.scan(reverse_scale=True, reverse_mask=reverse_mask))

    def test_ambiguous_add_is_not_guessed(self):
        self.assertIn("attention blocks found: 0", self.scan(ambiguous=True))


if __name__ == "__main__":
    unittest.main()
