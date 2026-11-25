// SPDX-License-Identifier: Apache-2.0
package templatecenter

import (
	"context"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"
)

const ReplicaStatusReady = "READY"

var ErrTemplateHasNoReadyReplica = errors.New("no ready replica")

type ReplicaStatus struct{ NodeID, Status string }
type fakeDB struct {
	mu        sync.Mutex
	rows      map[string][]ReplicaStatus
	listReads map[string]int
	health    map[string]bool
	failure   error
	blocked   <-chan struct{}
	entered   chan struct{}
}

var store struct{ db *fakeDB }

func replicaModelToStatus(row ReplicaStatus) ReplicaStatus { return row }
func isReplicaSchedulableNow(ctx context.Context, row ReplicaStatus) bool {
	return ctx.Err() == nil && strings.TrimSpace(row.Status) == ReplicaStatusReady && store.db.health[row.NodeID]
}
func ListReplicas(ctx context.Context, templateID string) ([]ReplicaStatus, error) {
	db := store.db
	db.mu.Lock()
	db.listReads[templateID]++
	rows := append([]ReplicaStatus(nil), db.rows[templateID]...)
	err, blocked, entered := db.failure, db.blocked, db.entered
	db.mu.Unlock()
	if entered != nil {
		select {
		case entered <- struct{}{}:
		default:
		}
	}
	if blocked != nil {
		select {
		case <-blocked:
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	}
	return rows, err
}

func setup() *fakeDB {
	db := &fakeDB{rows: map[string][]ReplicaStatus{"tpl": {{"a", "READY"}, {"b", "READY"}}},
		listReads: make(map[string]int), health: map[string]bool{"a": true, "b": true}}
	store.db = db
	readyReplicaResultCache = newResolveTTLCache(resolveResultCacheTTL)
	replicaSchedulableResultCache = newResolveTTLCache(resolveResultCacheTTL)
	resolveResultFetchGroup = &templateFetchGroup{calls: make(map[string]*templateFetchCall)}
	return db
}

func receive[T any](t *testing.T, channel <-chan T) T {
	t.Helper()
	select {
	case value := <-channel:
		return value
	case <-time.After(2 * time.Second):
		t.Fatal("query did not finish after the expected barrier")
		var zero T
		return zero
	}
}

type resolutionResult struct {
	replica ReplicaStatus
	err     error
}

func TestReadyFlightBeforeCacheFill(t *testing.T) {
	db := setup()
	db.rows["other"] = []ReplicaStatus{{"c", "READY"}}
	db.health["c"] = true
	joined := make(chan string, 3)
	resolveResultFetchGroup.followerJoined = joined

	release := make(chan struct{})
	db.blocked, db.entered = release, make(chan struct{}, 1)
	var workers sync.WaitGroup
	var once sync.Once
	resume := func() { once.Do(func() { close(release) }) }
	defer func() { resume(); workers.Wait() }()
	resolve := func(template, preferred string) <-chan resolutionResult {
		done := make(chan resolutionResult, 1)
		workers.Add(1)
		go func() {
			defer workers.Done()
			row, err := ResolveTemplateReadyReplica(context.Background(), template, preferred)
			done <- resolutionResult{row, err}
		}()
		return done
	}
	check := func(done <-chan resolutionResult, want string) {
		t.Helper()
		got := receive(t, done)
		if got.err != nil || got.replica.NodeID != want {
			t.Fatalf("wanted node %q, got %+v", want, got)
		}
	}

	first := resolve("tpl", "a")
	receive(t, db.entered)
	second := resolve("tpl", "b")
	receive(t, joined) // Only this follower exists and the first DB read is still blocked.
	db.mu.Lock()
	db.blocked = nil // Subsequent reads can finish while the first remains blocked.
	db.mu.Unlock()
	check(resolve("other", "c"), "c")
	resume()
	check(first, "a")
	check(second, "b")
	if db.listReads["tpl"] != 1 || db.listReads["other"] != 1 {
		t.Fatalf("wanted one read per template, got %v", db.listReads)
	}
}

func TestReadyInvalidationSeparatesFlightsAndRejectsOldRefill(t *testing.T) {
	db := setup()
	db.rows["tpl"] = []ReplicaStatus{{"a", "READY"}}
	release := make(chan struct{})
	var once sync.Once
	resume := func() { once.Do(func() { close(release) }) }
	db.blocked, db.entered = release, make(chan struct{}, 1)
	var workers sync.WaitGroup
	defer func() { resume(); workers.Wait() }()
	oldResult := make(chan resolutionResult, 1)
	workers.Add(1)
	go func() {
		defer workers.Done()
		replica, err := ResolveTemplateReadyReplica(context.Background(), "tpl", "")
		oldResult <- resolutionResult{replica, err}
	}()
	receive(t, db.entered)
	db.mu.Lock()
	db.rows["tpl"] = []ReplicaStatus{{"b", "READY"}}
	db.blocked, db.entered = nil, nil
	db.mu.Unlock()
	invalidateResolveResultCache("tpl")
	newResult := make(chan resolutionResult, 1)
	workers.Add(1)
	go func() {
		defer workers.Done()
		replica, err := ResolveTemplateReadyReplica(context.Background(), "tpl", "")
		newResult <- resolutionResult{replica, err}
	}()
	// A post-invalidation request must not join the still-blocked old flight.
	newer := receive(t, newResult)
	if newer.err != nil || newer.replica.NodeID != "b" {
		t.Fatalf("new generation: %v", newer)
	}
	resume()
	older := receive(t, oldResult)
	if older.err != nil || older.replica.NodeID != "b" {
		t.Fatalf("old reader reused obsolete metadata: %v", older)
	}
	if _, err := ResolveTemplateReadyReplica(context.Background(), "tpl", "b"); err != nil {
		t.Fatal(err)
	}
	if db.listReads["tpl"] != 2 {
		t.Fatalf("expected one read per generation, got %v", db.listReads)
	}
}
