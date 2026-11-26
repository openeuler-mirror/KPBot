"""Candidate-hash and evidence-verdict models; does not implement or run LZ4."""
import json
from pathlib import Path


def candidate_index(buffer, offset, hash_bytes=5, load_bytes=8, aarch64=True, by_u16=True):
    if hash_bytes not in (4, 5) or load_bytes < hash_bytes:
        raise ValueError("invalid model widths")
    if offset < 0 or offset > len(buffer) or load_bytes > len(buffer) - offset or not aarch64 or not by_u16:
        return "generic", None
    loaded = bytes(buffer[offset:offset + load_bytes])
    # Deliberately a fixture hash, not LZ4's hash formula or table layout.
    return "candidate", int.from_bytes(loaded[:hash_bytes], "little") % 65521


def verified_match(left, right, minimum=4):
    return len(left) >= minimum and len(right) >= minimum and left[:minimum] == right[:minimum]


PAIRS = {("original", "original"), ("original", "candidate"),
         ("candidate", "original"), ("candidate", "candidate")}


def compatibility_verdict(records, expected):
    # Records must come from real codecs for a target validation. Fixtures below
    # only verify that missing pairs or bad decode results cannot be called pass.
    return len(records) == 4 and {tuple(row["pair"]) for row in records} == PAIRS and all(
        row["status"] == "ok" and row["decoded_length"] == len(expected)
        and row["decoded"] == expected for row in records)


def main():
    left, right = b"ABCDX123", b"ABCDY123"
    _, old_left = candidate_index(left, 0, hash_bytes=4)
    _, old_right = candidate_index(right, 0, hash_bytes=4)
    route, new_left = candidate_index(left, 0)
    _, new_right = candidate_index(right, 0)
    payload = b"raw serialized bytes"
    records = [{"pair": pair, "status": "ok", "decoded_length": len(payload), "decoded": payload}
               for pair in sorted(PAIRS)]
    corrupted = [dict(row) for row in records]
    corrupted[1]["decoded"] = b"bad"
    short_decode = [dict(row) for row in records]
    short_decode[2]["decoded_length"] -= 1
    failed = [dict(row, status="sanitizer_failure") for row in records]
    checks = {
        "hash_candidate_route_positive": route == "candidate" and old_left == old_right and new_left != new_right,
        "actual_load_width_guard": candidate_index(b"ABCDX", 0)[0] == "generic"
            and candidate_index(b"!" + left, 1)[0] == "candidate" and candidate_index(left, 1)[0] == "generic",
        "architecture_and_table_fallback": candidate_index(left, 0, aarch64=False)[0] == "generic"
            and candidate_index(left, 0, by_u16=False)[0] == "generic",
        "candidate_still_requires_match": verified_match(left, right) and not verified_match(left, b"WXYZ1234")
            and not verified_match(b"ABC", left),
        "compatibility_matrix_complete": compatibility_verdict(records, payload) and not compatibility_verdict(records[:3], payload),
        "decode_bytes_and_length_checked": not compatibility_verdict(corrupted, payload) and not compatibility_verdict(short_decode, payload),
        "shared_failure_is_not_pass": not compatibility_verdict(failed, payload),
    }
    result = {"success": all(checks.values()), "real_lz4_executed": False, **checks}
    out = Path(__file__).resolve().parent / "results/result.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
