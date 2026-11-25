// Copyright (c) 2026 Tencent Inc.
// SPDX-License-Identifier: Apache-2.0
// Extracted from TencentCloud/CubeSandbox v0.7.1 (31d911e430fdf8a8879bd062b8e78066c1a8e89d).
// Surrounding packages are stubbed; the source predicates and singleflight are preserved.

package templatecenter

import "sync"

type templateFetchCall struct {
	done chan struct{}
	val  interface{}
	err  error
}

type templateFetchGroup struct {
	mu    sync.Mutex
	calls map[string]*templateFetchCall
	// Test-only observation point, installed before callers start.
	followerJoined chan<- string
}

func (g *templateFetchGroup) Do(key string, fn func() (interface{}, error)) (interface{}, error) {
	g.mu.Lock()
	if call, ok := g.calls[key]; ok {
		g.mu.Unlock()
		if g.followerJoined != nil {
			g.followerJoined <- key
		}
		<-call.done
		return call.val, call.err
	}
	call := &templateFetchCall{done: make(chan struct{})}
	g.calls[key] = call
	g.mu.Unlock()

	call.val, call.err = fn()
	close(call.done)

	g.mu.Lock()
	delete(g.calls, key)
	g.mu.Unlock()
	return call.val, call.err
}
