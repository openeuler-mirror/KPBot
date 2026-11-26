// Copyright (c) 2026 Tencent Inc.
// SPDX-License-Identifier: Apache-2.0
// READY-list resolver excerpt from the v0.7.1 Scheme 2 reference implementation.
package templatecenter

import (
	"context"
	"errors"
	"strings"
)

func ResolveTemplateReadyReplica(ctx context.Context, templateID, preferredNodeID string) (ReplicaStatus, error) {
	preferredNodeID = strings.TrimSpace(preferredNodeID)
	cacheKey := readyReplicaCacheKey(templateID)
	for {
		if err := ctx.Err(); err != nil {
			return ReplicaStatus{}, err
		}
		v, hit, generation := readyReplicaResultCache.Lookup(cacheKey)
		if hit {
			if replicas, ok := v.([]ReplicaStatus); ok {
				return selectReadyReplica(ctx, replicas, preferredNodeID)
			}
		}
		value, err := resolveResultFetchGroup.Do(resolveFlightKey("bind", cacheKey, generation), func() (interface{}, error) {
			v, hit, current := readyReplicaResultCache.ReadCurrent(cacheKey, generation)
			if !current {
				return nil, errResolveGenerationChanged
			}
			if hit {
				return v, nil
			}
			rows, err := ListReplicas(ctx, templateID)
			if err != nil {
				if !readyReplicaResultCache.Discard(cacheKey, generation) {
					return nil, errResolveGenerationChanged
				}
				// Discard retired this generation. Live followers may start a new read.
				if cause := ctx.Err(); cause != nil && errors.Is(err, cause) {
					return nil, errResolveGenerationChanged
				}
				return nil, err
			}
			ready := make([]ReplicaStatus, 0, len(rows))
			for _, item := range rows {
				replica := replicaModelToStatus(item)
				// Share READY metadata, never this caller's live health decision.
				if strings.TrimSpace(replica.Status) == ReplicaStatusReady {
					ready = append(ready, replica)
				}
			}
			if len(ready) == 0 {
				if !readyReplicaResultCache.Discard(cacheKey, generation) {
					return nil, errResolveGenerationChanged
				}
				return nil, ErrTemplateHasNoReadyReplica
			}
			cached := append([]ReplicaStatus(nil), ready...)
			if !readyReplicaResultCache.Store(cacheKey, generation, cached) {
				return nil, errResolveGenerationChanged
			}
			return cached, nil
		})
		if err := ctx.Err(); err != nil {
			return ReplicaStatus{}, err
		}
		if err == errResolveGenerationChanged {
			continue
		}
		if err != nil {
			return ReplicaStatus{}, err
		}
		// A follower may resume after the completed flight was invalidated.
		if !readyReplicaResultCache.Current(cacheKey, generation) {
			continue
		}
		replicas, _ := value.([]ReplicaStatus)
		return selectReadyReplica(ctx, replicas, preferredNodeID)
	}
}

func selectReadyReplica(ctx context.Context, replicas []ReplicaStatus, preferredNodeID string) (ReplicaStatus, error) {
	for _, replica := range replicas {
		if !isReplicaSchedulableNow(ctx, replica) {
			continue
		}
		if preferredNodeID == "" || strings.TrimSpace(replica.NodeID) == preferredNodeID {
			return replica, nil
		}
	}
	return ReplicaStatus{}, ErrTemplateHasNoReadyReplica
}
