# Kitex demo environment. Source this file; no fixed toolchain path is required.
#
# Toolchain resolution (first match wins):
#   GO_BIN   directory containing the `go` binary
#   GOROOT   a Go installation root (uses $GOROOT/bin)
#   PATH     an existing `go` on PATH
#
# Usage:
#   GO_BIN=/path/to/go/bin . assets/env.sh
#   GOROOT=/path/to/go     . assets/env.sh
#   . assets/env.sh        # when go is already on PATH

if [ -n "${GO_BIN:-}" ]; then
  export PATH="$GO_BIN:$PATH"
elif [ -n "${GOROOT:-}" ]; then
  export PATH="$GOROOT/bin:$PATH"
elif ! command -v go >/dev/null 2>&1; then
  echo "env.sh: go not found; set GO_BIN or GOROOT, or put go on PATH" >&2
  return 1 2>/dev/null || exit 1
fi

export GOPATH=${GOPATH:-$HOME/go}
export GOMODCACHE=${GOMODCACHE:-$GOPATH/pkg/mod}
export GOCACHE=${GOCACHE:-$HOME/.cache/go-build}
export GOPROXY=${GOPROXY:-https://goproxy.cn,direct}
export GOSUMDB=${GOSUMDB:-sum.golang.google.cn}
export GOFLAGS=${GOFLAGS:--mod=mod}
export GOTOOLCHAIN=${GOTOOLCHAIN:-local}
