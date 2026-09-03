# Type Migration Decision Framework

When a patch migrates a type (e.g., raw pointer `T*` to smart pointer `shared_ptr<T>`), the porting complexity increases dramatically because the change cascades through function signatures, call sites, member access patterns, and null checks. This guide helps you decide how to handle type migrations.

## The Core Problem

A type migration like `const SliceTransform*` → `const std::shared_ptr<const SliceTransform>&` affects:

1. **Function signatures**: Every function that takes or returns the type must change
2. **Call sites**: Arguments change from `&obj` or `obj.get()` to `obj`, pointer dereferences may need `.get()`
3. **Member access**: `ptr->method()` stays the same with `shared_ptr`, but `ptr` vs `*ptr` differs
4. **Null checks**: `ptr != nullptr` vs `ptr != nullptr` (same for shared_ptr, but `bool(ptr)` also works)
5. **Ownership semantics**: raw pointer may or may not own; shared_ptr always shares ownership
6. **Copy semantics**: raw pointer is cheap to copy; shared_ptr has atomic refcount overhead

## Three Strategies

### Strategy A: Full Migration

Migrate the entire type system from raw pointer to shared_ptr, matching the patch exactly.

**Pros**:
- Most correct — matches the source version's design
- Easier to port subsequent patches that depend on this type
- Eliminates dangling pointer risks

**Cons**:
- Largest change footprint — potentially 20+ files
- May touch code that the patch doesn't touch, increasing regression risk
- Must update all call sites consistently
- Compilation errors cascade — one missed `.get()` breaks the build

**When to use**:
- Later patches depend on the shared_ptr version
- The target already uses shared_ptr in some places (partial migration exists)
- The type migration is a core part of the optimization, not just cleanup

### Strategy B: Selective Adaptation

Port the non-type-migration parts of the patch, keeping the target's raw pointer style. Only change signatures where the patch's core logic requires it.

**Pros**:
- Smallest change footprint
- Lower regression risk
- Easier to compile and verify
- Preserves the target's existing code style

**Cons**:
- May conflict with later patches that assume shared_ptr
- Mixed raw pointer / shared_ptr usage can be confusing
- Some patch logic may not work correctly with raw pointers

**When to use**:
- Later patches don't depend on the type change
- The type migration is tangential to the patch's core optimization
- The target consistently uses raw pointers throughout

### Strategy C: Skip Type Changes

Mark all type migration hunks as skipped, only port the hunks that don't involve type changes.

**Pros**:
- Most conservative — no risk from type changes
- Fastest to implement
- Easiest to verify

**Cons**:
- May lose important functionality
- Later patches that depend on the type change will also need to be skipped or heavily adapted
- The ported code may not achieve the same optimization

**When to use**:
- The type migration is purely cleanup with no functional impact
- The core optimization can be expressed with raw pointers
- Time constraints require minimal changes

## Decision Flow

```
1. Check: Do later patches depend on the type change?
   ├─ Yes → Prefer Strategy A or B (not C)
   └─ No  → All three strategies are viable

2. Check: Does the target already use shared_ptr for this type anywhere?
   ├─ Yes → Prefer Strategy A (partial migration exists, complete it)
   └─ No  → Strategy B or C may be better (avoid introducing new pattern)

3. Evaluate: Is the type change essential to the optimization?
   ├─ Yes → Strategy A or B with adaptation
   └─ No  → Strategy C is safe

4. Evaluate: Effort vs. benefit
   ├─ Small scope (1-3 files) → Strategy A is fine
   ├─ Medium scope (4-10 files) → Strategy B recommended
   └─ Large scope (10+ files) → Carefully consider Strategy C
```

## Case Study: `const SliceTransform*` → `shared_ptr<const SliceTransform>` (0003_hashsearch)

### Impact Analysis

This type change affects ~20 files in the patch:
- `table/table_builder.h`, `table/table_builder.cc`
- `db/table_cache.h`, `db/table_cache.cc`
- `db/version_set.h`, `db/version_set.cc`
- `table/format.h`, `table/format.cc`
- `table/block_based/block_based_table_reader.h`, `.cc`
- `table/block_based/block_based_table_builder.h`, `.cc`
- `db/forward_iterator.h`, `db/forward_iterator.cc`
- And more...

### Decision for frocksdb 6.20.3

In frocksdb, all `prefix_extractor` usage is raw pointer. No shared_ptr usage exists for this type. The core optimization in 0003_hashsearch is:

1. **MetaBlockIter** — adding a new iterator class (no type dependency)
2. **ParseNextKey refactoring** — template on base class (no type dependency)
3. **BlockPrefixIndex using InternalKeySliceTransform as member** — this is where the type matters
4. **Establishing table_prefix_extractor early in BlockBasedTable::Open** — this is the optimization, and it works regardless of pointer type

**Recommended**: Strategy B — keep raw pointers in the target, port the functional changes (MetaBlockIter, ParseNextKey refactoring, early table_prefix_extractor establishment), adapt the BlockPrefixIndex change to use raw pointer with proper lifetime management.

### Adaptation Pattern for Strategy B

When the patch does this:
```cpp
// Patch (shared_ptr version)
void SetPrefixExtractor(const std::shared_ptr<const SliceTransform>& prefix_extractor) {
  prefix_extractor_ = prefix_extractor;
}
```

Adapt to target's raw pointer style:
```cpp
// Target (raw pointer version)
void SetPrefixExtractor(const SliceTransform* prefix_extractor) {
  prefix_extractor_ = prefix_extractor;
}
```

Key differences to watch for:
- `shared_ptr` copy: `auto p = prefix_extractor;` → raw pointer copy: `auto* p = prefix_extractor;`
- `shared_ptr` null check: `if (prefix_extractor)` → same for raw pointer
- `shared_ptr` member access: `prefix_extractor->Method()` → same for raw pointer
- `shared_ptr` as arg: `void Func(shared_ptr<const T> p)` → `void Func(const T* p)`
- Passing `shared_ptr`: `Func(prefix_extractor_)` → `Func(prefix_extractor_)` (same)
- Getting raw from shared: `prefix_extractor_.get()` → just `prefix_extractor_`

## Lifetime Management with Raw Pointers

When keeping raw pointers, you must ensure the pointer remains valid for its entire usage. The shared_ptr migration was likely done to fix lifetime bugs. If you keep raw pointers:

1. **Document ownership**: Who owns the object? Who is responsible for deletion?
2. **Check lifetimes**: Does the pointed-to object outlive all users?
3. **Watch for dangling**: If the owner is destroyed while users still hold the pointer, that's a bug
4. **Consider const references**: For function parameters that don't store the pointer, `const T&` may be safer than `const T*`

If you find that the type migration was specifically to fix a lifetime bug, strongly consider Strategy A — the fix is more important than the adaptation cost.
