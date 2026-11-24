"""Exercise check_tokenizer's CLI with a local fixture and a vocabulary stub."""
import argparse
import contextlib
import io
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "scripts"))
import check_tokenizer


class FixtureTokenizer:
    @classmethod
    def from_pretrained(cls, directory, *, trust_remote_code):
        if trust_remote_code:
            raise ValueError("fixture should not load remote code")
        return cls(Path(directory) / "vocab.txt")

    def __init__(self, filename):
        self.tokens = filename.read_text().splitlines()

    def __call__(self, text, add_special_tokens=False):
        if add_special_tokens:
            raise ValueError("fixture only covers chat without added tokens")
        return {"input_ids": [self.tokens.index(word) for word in text.split()]}

    def decode(self, ids):
        return " ".join(self.tokens[i] for i in ids)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real-tokenizer", action="store_true",
                        help="use the installed transformers tokenizer instead of the vocabulary stub")
    args = parser.parse_args()
    fixture = HERE / "fixtures"
    command = ["check_tokenizer.py", "--corpus", str(fixture / "gold.jsonl"),
               "--tokenizer", str(fixture / "tokenizer"), "--show", "0"]
    dependency = SimpleNamespace(AutoTokenizer=FixtureTokenizer)
    load = (contextlib.nullcontext() if args.real_tokenizer else
            patch.dict(sys.modules, {"transformers": dependency}))
    output = io.StringIO()
    with patch.object(sys, "argv", command), load, contextlib.redirect_stdout(output):
        code = check_tokenizer.main()
    summary = output.getvalue()
    if code != 1 or "一致 1，不一致 1" not in summary:
        raise SystemExit(f"expected exactly one equal-length token ID mismatch: {summary}")
    if "alpha beta" in summary or "beta alpha" in summary:
        raise SystemExit("comparison printed request text despite --show 0")
    print(summary, end="")


if __name__ == "__main__":
    main()
