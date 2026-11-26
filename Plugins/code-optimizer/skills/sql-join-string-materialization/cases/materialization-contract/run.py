"""Standard-library ownership model; not the native StringView ABI."""
from dataclasses import dataclass
import json
from pathlib import Path


@dataclass
class Output:
    values: list
    buffers: list
    inline_count: int
    copied_count: int

    def read(self):
        return [None if value is None else bytes(value) for value in self.values]


def materialize(values, inline_limit=12):
    """12 is a fixture limit; use the actual layout's inline limit in native code."""
    output = Output([], [], 0, 0)
    for value in values:
        if value is None:
            output.values.append(None)
        elif len(value) <= inline_limit:
            output.values.append(bytes(value))
            output.inline_count += 1
        else:
            owned = bytearray(value)
            output.buffers.append(owned)
            output.values.append(memoryview(owned))
            output.copied_count += 1
    return output


def main():
    payloads = [b"", b"a", b"b" * 12, b"c" * 13, b"d" * 64, "库存".encode("utf-8")]
    sources = [bytearray(b"PRE" + value + b"POST") for value in payloads]
    slices = [memoryview(buf)[3:-4] for buf in sources]
    # Repeated dictionary indices model duplicate values and stable output order.
    indices = [4, 2, None, 0, 3, 4, 5, 1]
    values = [None if index is None else slices[index] for index in indices]
    expected = [None if value is None else bytes(value) for value in values]
    output = materialize(values)
    before = output.read()
    for buf in sources:
        buf[:] = b"!" * len(buf)
    after_reuse = output.read()
    del values, slices, sources
    checks = {
        "materialization_route_positive": output.inline_count == 4 and output.copied_count == 3,
        "values_nulls_and_order": before == expected and before[2] is None and before[3] == b"",
        "source_reuse_preserves_values": after_reuse == expected,
        "source_release_preserves_values": output.read() == expected,
        "long_views_owned_by_output": len(output.buffers) == 3 and all(
            not isinstance(value, memoryview) or any(value.obj is buf for buf in output.buffers)
            for value in output.values),
        "duplicate_long_values_preserved": before[0] == before[5] == b"d" * 64,
        "inline_boundary_and_byte_length": before[1] == b"b" * 12 and before[4] == b"c" * 13
            and before[6] == "库存".encode("utf-8"),
    }
    result = {"success": all(checks.values()), **checks}
    out = Path(__file__).resolve().parent / "results/result.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
