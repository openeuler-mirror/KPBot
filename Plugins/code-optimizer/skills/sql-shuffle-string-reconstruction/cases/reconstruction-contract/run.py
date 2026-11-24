"""Model dispatch, value bounds and owner retention; no native SIMD executes."""
from dataclasses import dataclass, replace
import json
from pathlib import Path


@dataclass(frozen=True)
class Environment:
    built: bool = True
    available: bool = True
    little_endian: bool = True
    layout_valid: bool = True
    aligned: bool = True
    unaligned_allowed: bool = False


def reconstruct(buffer, offset, length, env=Environment()):
    if offset < 0 or length < 0 or offset > len(buffer) or length > len(buffer) - offset:
        raise ValueError("invalid value range")
    remaining = len(buffer) - offset
    # Fixture inline limit only. A wide load must fit even for a one-byte value.
    wide = (0 < length <= 12 and remaining >= 16 and env.built and env.available
            and env.little_endian and env.layout_valid and (env.aligned or env.unaligned_allowed))
    route = "wide" if wide else "generic"
    if wide:
        loaded = bytes(memoryview(buffer)[offset:offset + 16])
        value = loaded[:length]
    else:
        # A long output view retains the source owner, even after the caller drops it.
        value = memoryview(buffer)[offset:offset + length] if length > 12 else bytes(buffer[offset:offset + length])
    return route, value


def main():
    source = bytearray(range(32))
    positive, value = reconstruct(source, 16, 12)
    unsupported = [replace(Environment(), **change) for change in (
        {"built": False}, {"available": False}, {"little_endian": False},
        {"layout_valid": False}, {"aligned": False})]
    invalid = 0
    for offset, length in ((-1, 1), (33, 0), (31, 2), (0, -1)):
        try:
            reconstruct(source, offset, length)
        except ValueError:
            invalid += 1
    matrix = []
    for length in range(34):
        for offset in (0, 3):
            for padding in (0, 4, 16):
                buffer = bytearray(b"P" * offset + bytes(range(length)) + b"T" * padding)
                route, result = reconstruct(buffer, offset, length)
                expected_route = "wide" if 0 < length <= 12 and length + padding >= 16 else "generic"
                matrix.append(bytes(result) == bytes(range(length)) and route == expected_route)
    owner = bytearray(range(40))
    route, long_view = reconstruct(owner, 3, 20)
    retained = long_view.obj is owner
    del owner
    checks = {
        "reconstruction_route_positive": positive == "wide" and value == bytes(range(16, 28)),
        "short_tail_falls_back": reconstruct(source, 17, 12)[0] == "generic",
        "unsupported_environment_falls_back": all(reconstruct(source, 0, 1, env)[0] == "generic" for env in unsupported),
        "permitted_unaligned_route": reconstruct(source, 1, 1, replace(Environment(), aligned=False, unaligned_allowed=True))[0] == "wide",
        "invalid_ranges_rejected": invalid == 4,
        "length_offset_padding_matrix": len(matrix) == 204 and all(matrix),
        "long_view_retains_owner": route == "generic" and retained and bytes(long_view) == bytes(range(3, 23)),
        "empty_value_uses_generic": reconstruct(source, len(source), 0) == ("generic", b""),
    }
    result = {"success": all(checks.values()), **checks}
    out = Path(__file__).resolve().parent / "results/result.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
