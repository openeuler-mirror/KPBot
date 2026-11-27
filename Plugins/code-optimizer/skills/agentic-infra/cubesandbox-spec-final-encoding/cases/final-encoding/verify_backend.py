"""Compare the fixture's backend decision with a CubeSandbox source checkout."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from run import normalized_backend, record_cases


GO_TEST = """package constants

import (
    "encoding/json"
    "os"
    "testing"
)

func TestSpecBackendDecision(t *testing.T) {
    data, err := os.ReadFile("decisions.json")
    if err != nil { t.Fatal(err) }
    var rows []struct { Raw, Expected string }
    if err := json.Unmarshal(data, &rows); err != nil { t.Fatal(err) }
    for _, row := range rows {
        backend := ""
        if normalized, ok, err := OptionalSnapshotBackend(row.Raw); err == nil && ok {
            backend = normalized
        }
        if backend != row.Expected {
            t.Errorf("raw %q: CubeMaster backend %q, fixture %q", row.Raw, backend, row.Expected)
        }
    }
}
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path, help="CubeSandbox source root")
    args = parser.parse_args()
    source = args.checkout / "CubeMaster/pkg/base/constants/snapshot_backend.go"
    rows = [{"Raw": request.get("backend", ""), "Expected": normalized_backend(request)}
            for request, _, _, _ in record_cases()]
    with tempfile.TemporaryDirectory() as temporary:
        project = Path(temporary)
        shutil.copyfile(source, project / source.name)
        (project / "decisions.json").write_text(json.dumps(rows), encoding="utf-8")
        (project / "backend_test.go").write_text(GO_TEST, encoding="utf-8")
        subprocess.run(["go", "test", "-count=1", source.name, "backend_test.go"], cwd=project,
                       env={**os.environ, "GO111MODULE": "off", "GOWORK": "off",
                            "GOTOOLCHAIN": "local", "GOPROXY": "off"}, check=True)
    print(f"Compared {len(rows)} backend decisions with {source.name}")


if __name__ == "__main__":
    main()
