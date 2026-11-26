# Assessment Guide: Classifying Patch Hunks

This guide provides detailed criteria and real examples for each assessment category used in Step 3 (ASSESS) of the porting workflow.

## DIRECT_PORT

**When**: The target version's code is structurally similar to the source. Function names, class structures, and surrounding context are recognizable — only line numbers differ.

**What to do**: Adjust line numbers and apply the change directly using the Edit tool on the target file.

### Example: PREFETCH locality change (0002_prefetch.patch)

The patch changes `PREFETCH(addr, 0, 1)` to `PREFETCH(addr, 0, 3)` in `memtable/inlineskiplist.h`. In frocksdb 6.20.3, the same file has the same PREFETCH calls with locality 1. The function names and logic are identical — only line numbers shifted.

**Assessment**: DIRECT_PORT — change the locality parameter from 1 to 3 on the matching PREFETCH calls.

### Example: Adding a field next to an existing field (0001_autumn.patch)

Adding `double autumn_c = 1.0;` next to `max_bytes_for_level_multiplier` in `advanced_options.h`. The target has the same struct with the same neighboring field.

**Assessment**: DIRECT_PORT — insert the new field declaration at the appropriate location.

---

## ADAPTED_PORT

**When**: The intent is the same, but the target's code structure differs — different loop patterns, different variable names, different function signatures, or the change needs to be expressed differently to fit the target's architecture.

**What to do**: Re-implement the intent in the target's style. Do NOT copy-paste from the patch. Read the target's version of the function, understand what it does, then modify it to achieve the patch's intent.

### Example: CalculateBaseBytes modification (0001_autumn.patch)

**Source version** (rocksdb v6.26.1):
```cpp
for (int i = 1; i < num_levels_in_use_; ++i) {
  level_max_bytes_[i] = level_max_bytes_[i-1] * multiplier * autumn_c;
}
```

**Target version** (frocksdb 6.20.3):
```cpp
for (int i = 1; i < options_.num_levels; ++i) {
  level_max_bytes_[i] = level_max_bytes_[i-1] * multiplier;
}
```

Differences:
1. Loop bound: `num_levels_in_use_` vs `options_.num_levels`
2. No `autumn_c` multiplication in target
3. Target doesn't have `num_levels_in_use_` field at all

**Assessment**: ADAPTED_PORT + MISSING_INFRA
- First: add `num_levels_in_use_` field to VersionStorageInfo (MISSING_INFRA)
- Then: modify the loop to multiply by `autumn_c` using the target's loop structure (ADAPTED_PORT)

### Example: BlockBasedTable::Open prefix_extractor change (0003_hashsearch.patch)

The patch changes `prefix_extractor` from raw pointer to `shared_ptr` in many function signatures. In frocksdb, all these functions use raw pointers.

**Assessment**: ADAPTED_PORT (type migration) — see `type-migration.md` for the decision framework.

---

## MISSING_INFRA

**When**: The target version lacks a field, type, function, or class that the patch assumes exists. The change cannot be ported until this prerequisite is added.

**What to do**: Add the minimum necessary infrastructure first, then port the dependent change. Record both steps.

### Example: `num_levels_in_use_` field (0001_autumn.patch)

The patch uses `num_levels_in_use_` in `version_set.h` and `version_set.cc`, but frocksdb 6.20.3 doesn't have this field in `VersionStorageInfo`.

**Action**:
1. Add `int num_levels_in_use_ = -1;` to `VersionStorageInfo` in `version_set.h`
2. Initialize it in the constructor
3. Set it in `VersionStorageInfo::SetFinalized()`
4. Now the `CalculateBaseBytes` modification can be ported

### Example: `MetaBlockIter` class (0003_hashsearch.patch)

The patch adds a `MetaBlockIter` class to `block.h`. The target doesn't have this class.

**Action**: Add the `MetaBlockIter` class definition to the target's `block.h`, adapting it to the target's iterator base class structure.

---

## SKIP_NO_FILE

**When**: The file the patch modifies doesn't exist in the target version, and no equivalent file can be found.

**What to do**: Skip the hunk. Record the file name and why it was skipped. If the change is critical (not just build/tooling), investigate further.

### Example: framework/fork-specific build scripts

All patches that modify a framework-specific build script (e.g. upstream's `build.sh`) should be skipped when the target fork has no such file — the fork has its own local build script (e.g. `local_build.sh`) instead.

**Action**: SKIP_NO_FILE for these hunks. Always record them.

### Example: Test files that don't exist

If the patch modifies `test_options_settable.cc` but the target doesn't have this test file, skip it.

**Action**: SKIP_NO_FILE — but check if there's an equivalent test file first.

---

## SKIP_FORK_CONFLICT

**When**: The target fork has its own different implementation of the same logic the patch modifies. Applying the patch would overwrite fork-specific behavior.

**What to do**: Compare both implementations. Decide whether to:
- **Override**: The patch's change is more correct/performant, replace the fork's version
- **Merge**: Combine the best of both approaches
- **Keep fork's version**: The fork's implementation is more appropriate for its use case

Document the decision and reasoning.

### Example: CalculateBaseBytes with fork-specific logic

If frocksdb's `CalculateBaseBytes` has its own custom scaling logic that differs from upstream rocksdb, and the patch modifies the same function, this is a fork conflict.

**Decision factors**:
- Is the patch's change orthogonal to the fork's change? (Can both coexist?)
- Is the patch's change the core optimization being ported? (Must be included)
- Can the fork's change be preserved alongside? (Merge if possible)
