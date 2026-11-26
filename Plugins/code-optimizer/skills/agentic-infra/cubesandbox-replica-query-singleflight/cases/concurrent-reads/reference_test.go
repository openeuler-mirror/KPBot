// SPDX-License-Identifier: Apache-2.0
package templatecenter

import (
	"crypto/sha256"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// Pin the reviewed patch and both derived source files together. The runner
// also supports an exported skill with an explicitly supplied reference patch.
func TestReferenceConsistency(t *testing.T) {
	verify := func(path, want string) {
		t.Helper()
		data, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		normalized := strings.ReplaceAll(string(data), "\r\n", "\n")
		if got := fmt.Sprintf("%x", sha256.Sum256([]byte(normalized))); got != want {
			t.Fatalf("%s changed: regenerate and review the fixture against the reference patch", path)
		}
	}
	verify("cache_test.go", "bca9ae5ce82d486ff21c45d08559a70ca97cbe5e4800479e8b12f71b43ca75e9")
	verify("resolver_test.go", "765e2ad04c20e31258029476c6fcd52a920e8a08a4b514ebf142b3570f058918")
	const patchHash = "b68e9e153eba9d2c016ed727ee70012d5f4ec4a6c17000d48da6b927c8c109c2"
	// Resolve from the case directory in a KPBot checkout.
	domain := filepath.Join("..", "..", "..")
	if _, err := os.Stat(filepath.Join(domain, "DOMAIN.md")); err == nil {
		verify(filepath.Join(domain, "cubesandbox-create-optimize", "references", "scheme2-rsi-cache.patch"), patchHash)
		verify(filepath.Join(domain, "..", "cubesandbox-master-create-path", "references", "scheme2-rsi-cache.patch"), patchHash)
	} else if !os.IsNotExist(err) {
		t.Fatal(err)
	}
	if path := os.Getenv("SCHEME2_REFERENCE_PATCH"); path != "" {
		verify(path, patchHash)
	}
}
