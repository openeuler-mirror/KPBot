# Patch Anatomy: Reading and Decomposing Complex Patches

## Unified Diff Format

A patch file in unified diff format consists of:

```
diff --git a/old_file b/new_file
index abc1234..def5678 100644
--- a/old_file          ← source file path
+++ b/new_file          ← target file path
@@ -X,Y +A,B @@ optional heading
 context line           ← unchanged (for reference only)
-removed line           ← existed in source, removed by patch
+added line             ← new line added by patch
 context line           ← unchanged
```

### Header Lines

- `diff --git a/X b/Y` — identifies the file being modified. If X ≠ Y, the file was renamed.
- `index abc..def 100644` — git blob hashes and mode. Not useful for porting.
- `--- a/X` / `+++ b/Y` — old and new file paths. Use these to identify which file to modify.

### Hunk Header

`@@ -X,Y +A,B @@ heading`

- `X`: starting line in the source file
- `Y`: number of lines in the source hunk
- `A`: starting line in the target file (after patch)
- `B`: number of lines in the target hunk (after patch)
- `heading`: often the function name where the change occurs — useful for locating the change in the target

**Important**: The line numbers (X, A) are for the SOURCE version, not the target version you're porting to. They help you understand the patch but NOT where to apply it.

### Context Lines

Lines starting with a space are unchanged context. They show what surrounds the change. Use them to locate the equivalent position in the target, but DO NOT assume the target has identical context.

### New File Indicators

```
--- /dev/null
+++ b/new_file.cpp
```

This means the patch creates an entirely new file. Port the file as-is, adjusting only for type/API differences with the target version.

## Decomposition Strategy

For complex patches (100+ lines, multiple files), decompose before porting.

### Step 1: File Inventory

List every file the patch touches:

```
0003_hashsearch.patch touches:
  table/block_based/block.h          (+45 -12)
  table/block_based/block.cc         (+120 -30)
  table/block_based/block_based_table_reader.h  (+8 -8)
  table/block_based/block_based_table_reader.cc  (+60 -40)
  db/table_cache.h                   (+4 -4)
  db/table_cache.cc                  (+8 -8)
  ...etc
```

The `(+N -M)` counts tell you the rough complexity per file.

### Step 2: Group by Intent

Group hunks by what they're trying to accomplish, not by file:

```
Group A: Type migration (prefix_extractor T* → shared_ptr<T>)
  - table/table_builder.h:1-4
  - db/table_cache.h:1-8
  - db/version_set.cc:3-5
  ... (spans ~20 files)

Group B: MetaBlockIter addition
  - table/block_based/block.h:1-30
  - table/block_based/block.cc:1-50

Group C: ParseNextKey template refactoring
  - table/block_based/block.h:5-15
  - table/block_based/block.cc:51-80

Group D: BlockPrefixIndex member change
  - table/block_based/block_based_table_reader.h:1-4
  - table/block_based/block_based_table_reader.cc:1-20
```

### Step 3: Identify Dependencies

Which groups depend on which?

```
Group B (MetaBlockIter) — independent, can port first
Group C (ParseNextKey) — independent, can port first
Group A (type migration) — independent of B and C, but affects D
Group D (BlockPrefixIndex) — depends on A for type, depends on C for iterator
```

### Step 4: Port in Dependency Order

```
1. Port Group B (MetaBlockIter) — no dependencies
2. Port Group C (ParseNextKey) — no dependencies
3. Decide on Group A strategy (see type-migration.md)
4. Port Group D — depends on decisions from step 3
```

## Reading Between the Lines

### Inferring Intent from Code Changes

Sometimes a hunk's intent isn't obvious from the diff alone. Look for:

1. **Commit message or patch name**: `0003_hashsearch` suggests hash-based search optimization
2. **Variable names**: `prefix_extractor` changes suggest prefix extraction logic
3. **New includes**: `#include <arm_sve.h>` clearly means ARM SVE support
4. **Build system changes**: new source files, compiler flags → new feature/optimization
5. **Test changes**: they document expected behavior

### Identifying What's Essential vs. Incidental

A large patch often contains:
- **Essential changes**: the core optimization or feature (must be ported)
- **Incidental changes**: cleanup, refactoring, style fixes (nice to have but not required)
- **Infrastructure changes**: needed for the essential changes but not the goal itself

Example from 0005_bf.patch:
- Essential: SVE2 bloom filter path in `bloom_impl.h`
- Incidental: typo fix in `block.cc` comment ("skiped" → "skipped")
- Infrastructure: SVE2 detection in `build_detect_platform`
- Build-specific: `build.sh` changes (skip)

### Dealing with Inter-Patch Dependencies

Patches are numbered for a reason. Check:
1. Does patch N modify code that patch N-1 added? → N depends on N-1
2. Does patch N use a type/field/function introduced in patch N-1? → N depends on N-1
3. Are they independent? → can be ported in any order (but still port sequentially for clean diffs)

Example dependency chain:
- 0001_autumn: adds `autumn_c` field
- 0002_prefetch: independent of autumn_c (prefetch changes only)
- 0003_hashsearch: may reference `prefix_extractor` patterns established earlier
- 0004_crc32c: independent (CRC implementation)
- 0005_bf: independent bloom filter optimization

When you find a dependency that crosses patches, you must port the prerequisite first.
