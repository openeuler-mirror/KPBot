"""Verify safe fixed-width copy geometry and readable-tail dispatch."""

import json
from pathlib import Path


def write_string(destination, offset, value):
    end = offset + len(value)
    if offset < 0 or end > len(destination):
        raise ValueError("output region outside buffer")
    view = memoryview(destination)
    if 16 <= len(value) <= 32:
        view[offset:offset+16] = value[:16]
        view[end-16:end] = value[-16:]
        return "fixed"
    view[offset:end] = value
    return "generic"


def read_route(buffer, offset):
    if not 0 <= offset <= len(buffer):
        raise ValueError("offset outside buffer")
    return "wide" if len(buffer) - offset >= 16 else "generic"


def main():
    fixed_copy_lengths = 0
    for size in range(65):
        value = bytes(range(size))
        dest = bytearray(b"!" * (size + 6))
        route = write_string(dest, 3, value)
        assert dest == b"!!!" + value + b"!!!"
        assert route == ("fixed" if 16 <= size <= 32 else "generic")
        fixed_copy_lengths += route == "fixed"

    rejected_writes = 0
    for bad_offset in (-1, 2):
        dest = bytearray(b"!" * 21)
        try:
            write_string(dest, bad_offset, bytes(range(20)))
        except ValueError:
            rejected_writes += 1
        assert dest == b"!" * 21
    assert rejected_writes == 2

    source = bytes(range(24))
    assert read_route(source, 8) == "wide"
    assert read_route(source, 9) == "generic"
    assert read_route(source, 24) == "generic"
    out = Path(__file__).resolve().parent / "results/result.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"success": True, "tested_lengths": 65,
                               "fixed_copy_lengths": fixed_copy_lengths,
                               "rejected_writes": rejected_writes}) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
