"""Generate deterministic JPEGs for smoke testing; these are NOT ImageNet."""

import argparse
import json
from pathlib import Path


def generate(output, count):
    if not 1 <= count <= 10000:
        raise ValueError("count must be between 1 and 10000")
    from PIL import Image

    output.mkdir(parents=True, exist_ok=False)
    for index in range(count):
        width, height = 257 + index % 7, 271 + index % 11
        data = bytes(
            (x * 17 + index * 23) % 256 for x in range(width * height * 3)
        )
        Image.frombytes("RGB", (width, height), data).save(
            output / f"sample-{index:05d}.jpg", quality=85
        )
    (output / "fixture.json").write_text(
        json.dumps(
            {
                "kind": "synthetic-jpeg-smoke",
                "count": count,
                "generator": "rgb-byte-pattern-v1",
                "is_imagenet": False,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--count", type=int, default=16)
    args = parser.parse_args()
    try:
        generate(args.output, args.count)
    except (OSError, ValueError, ImportError) as exc:
        parser.exit(1, f"fixture failed: {exc}\n")


if __name__ == "__main__":
    main()
