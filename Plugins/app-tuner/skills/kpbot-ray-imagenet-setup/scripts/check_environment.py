"""Check installed versions and exercise real CPU torchvision operators."""

import json
import platform
import sys


def inspect():
    import numpy
    import PIL
    import pyarrow
    import ray
    import torch
    import torchvision
    from torchvision.ops import nms

    versions = {
        "ray": ray.__version__,
        "torch": torch.__version__,
        "torchvision": torchvision.__version__,
        "numpy": numpy.__version__,
        "pyarrow": pyarrow.__version__,
        "Pillow": PIL.__version__,
    }
    expected = {
        "ray": "2.52.0",
        "torch": "2.6.0",
        "torchvision": "0.21.0",
        "numpy": "2.1.3",
        "pyarrow": "19.0.1",
        "Pillow": "11.3.0",
    }
    for name, version in versions.items():
        if version.split("+")[0] != expected[name]:
            raise ValueError(f"unsupported {name} version: {version}")
    if not (3, 10) <= sys.version_info[:2] <= (3, 12):
        raise ValueError("Python 3.10 through 3.12 required")
    if torch.version.cuda is not None:
        raise ValueError("CPU-only PyTorch wheel required; found a CUDA build")
    value = nms(torch.tensor([[0.0, 0.0, 2.0, 2.0]]), torch.tensor([1.0]), 0.5)
    if value.tolist() != [0]:
        raise ValueError("torchvision CPU operator check failed")
    return {
        "status": "passed",
        "versions": versions,
        "python": platform.python_version(),
        "architecture": platform.machine(),
        "cuda_available": torch.cuda.is_available(),
        "torch_cuda_build": torch.version.cuda,
    }


if __name__ == "__main__":
    try:
        print(json.dumps(inspect()))
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}))
        raise SystemExit(1)
