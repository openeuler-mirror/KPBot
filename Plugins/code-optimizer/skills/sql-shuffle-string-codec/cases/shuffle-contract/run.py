"""Verify safe fixed-width copy geometry and readable-tail dispatch."""

import json
from pathlib import Path


def write_string(destination, offset, value):
    end = offset + len(value)
    if offset < 0 or end > len(destination):
        raise ValueError("output region outside buffer")
    view = memoryview(destination)
    if 16 <= len(value) <= 32:
        # Both loads must observe the source before either store aliases it.
        head = bytes(value[:16])
        tail = bytes(value[-16:])
        view[offset:offset+16] = head
        view[end-16:end] = tail
        return "fixed"
    view[offset:end] = bytes(value)
    return "generic"


def read_route(buffer, offset):
    if not 0 <= offset <= len(buffer):
        raise ValueError("offset outside buffer")
    return "wide" if len(buffer) - offset >= 16 else "generic"


def main():
    failures = []

    def check(condition, label, **context):
        if not condition:
            failures.append({"check":label,**context})

    fixed_copy_lengths = 0
    tested_lengths = 0
    for size in range(65):
        value = bytes(range(size))
        dest = bytearray(b"!" * (size + 6))
        route = write_string(dest, 3, value)
        check(dest == b"!!!" + value + b"!!!", "original-copy", length=size)
        check(route == ("fixed" if 16 <= size <= 32 else "generic"), "original-route", length=size)
        fixed_copy_lengths += route == "fixed"
        tested_lengths += 1
    check(tested_lengths == 65 and fixed_copy_lengths == 17, "original-length-counts")

    rejected_writes = 0
    for bad_offset in (-1, 2):
        dest = bytearray(b"!" * 21)
        try:
            write_string(dest, bad_offset, bytes(range(20)))
        except ValueError:
            rejected_writes += 1
        check(dest == b"!" * 21, "invalid-write-unchanged", offset=bad_offset)
    check(rejected_writes == 2, "invalid-write-count")

    source = bytes(range(24))
    check(read_route(source, 8) == "wide", "read-exact-width")
    check(read_route(source, 9) == "generic", "read-short-tail")
    check(read_route(source, 24) == "generic", "read-end")

    boundary_cases = exact_capacity_cases = nonzero_offset_cases = 0
    outside_unchanged = source_unchanged = True
    for size in (0,1,13,15,16,17,20,31,32,33,64):
        payload = bytes((i*37+11)%256 for i in range(size))
        for source_offset in (0,3):
            for target_offset in (0,5):
                for suffix in (0,4):
                    original_source = b"S"*source_offset + payload + b"T"*4
                    source = bytearray(original_source)
                    value = memoryview(source)[source_offset:source_offset+size]
                    end = target_offset+size
                    destination = bytearray(b"!"*(end+suffix))
                    before = bytes(destination)
                    context = dict(length=size,source_offset=source_offset,
                                   target_offset=target_offset,suffix=suffix)
                    try:
                        route = write_string(destination,target_offset,value)
                    except (ValueError,IndexError) as error:
                        failures.append({"check":"valid-boundary-rejected","error":type(error).__name__,**context})
                        route = None
                    expected = before[:target_offset]+payload+before[end:]
                    untouched = bytes(destination[:target_offset])==before[:target_offset] and bytes(destination[end:])==before[end:]
                    same_source = bytes(source)==original_source
                    outside_unchanged = outside_unchanged and untouched
                    source_unchanged = source_unchanged and same_source
                    check(bytes(destination)==expected, "boundary-bytes", **context)
                    check(len(destination)==len(before), "boundary-capacity", **context)
                    check(untouched, "outside-region", **context)
                    check(same_source, "source-region", **context)
                    check(route==("fixed" if 16<=size<=32 else "generic"), "boundary-route", **context)
                    boundary_cases += 1
                    exact_capacity_cases += suffix==0
                    nonzero_offset_cases += source_offset>0 and target_offset>0
    check((boundary_cases,exact_capacity_cases,nonzero_offset_cases)==(88,44,22), "boundary-counts")
    shared_buffer_cases = 0
    for size in (15, 16, 17, 20, 31, 32, 33):
        source_offset = size + 1
        for shift in (-size, -1, 0, 1, size):
            target_offset = source_offset + shift
            buffer = bytearray(range(3 * size + 2))
            before = bytes(buffer)
            source = memoryview(buffer)[source_offset:source_offset + size]
            write_string(buffer, target_offset, source)
            expected = (before[:target_offset] + before[source_offset:source_offset + size]
                        + before[target_offset + size:])
            check(bytes(buffer) == expected, "shared-buffer-copy", length=size, shift=shift)
            shared_buffer_cases += 1
    result = {"success":not failures,"tested_lengths":tested_lengths,
              "shared_buffer_cases":shared_buffer_cases,
              "fixed_copy_lengths":fixed_copy_lengths,"rejected_writes":rejected_writes,
              "boundary_cases":boundary_cases,"exact_capacity_cases":exact_capacity_cases,
              "nonzero_offset_cases":nonzero_offset_cases,"outside_regions_unchanged":outside_unchanged,
              "source_regions_unchanged":source_unchanged,"failure_count":len(failures),"failures":failures}
    out = Path(__file__).resolve().parent / "results/result.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(result) + "\n", encoding="utf-8")
    return 0 if result['success'] else 1


if __name__ == "__main__":
    raise SystemExit(main())
