# Repository Guidelines

## Project Structure & Module Organization

This repository is a server application optimization skill framework. The canonical implementation lives in `skills/server-application-optimization/`.

- `skills/server-application-optimization/SKILL.md`: main orchestration skill.
- `skills/server-application-optimization/subskills/`: focused optimization workflows such as CPU affinity, compiler tuning, network, and database analysis.
- `skills/server-application-optimization/references/`: supporting contracts, checklists, examples, schemas, and workflow notes.
- `skills/server-application-optimization/scripts/`: executable helpers for backups, placeholder benchmarks, bottleneck checks, and report summaries.
- `docs/`: requirements, design, usage guide, architecture, and report template.
- `ref-skills/`: imported or reference skills used as integration examples.
- `.claude/`, `.opencode/`, `.agents/`: compatibility entry points. Keep them aligned with the canonical skill when changing public skill behavior.

## Build, Test, and Development Commands

There is no package build step. Validate changes with targeted script runs and documentation review:

```bash
skills/server-application-optimization/scripts/backup_environment.sh ./output/env
skills/server-application-optimization/scripts/init_report.sh ./output/report demo-scenario
skills/server-application-optimization/scripts/run_placeholder_benchmark.sh demo-scenario
python3 skills/server-application-optimization/scripts/summarize_improvement.py \
  --baseline baseline.json --candidate tuned.json --round-name round-2
```

Use `shellcheck skills/server-application-optimization/scripts/*.sh` when available before changing shell scripts.

## Coding Style & Naming Conventions

Shell scripts use Bash with `#!/usr/bin/env bash` and `set -euo pipefail`. Prefer clear function names, quoted variables, and explicit output paths. Python scripts should be small, standard-library only unless a dependency is justified, and formatted with 4-space indentation. Markdown should use concise headings, relative links, and command examples that can be run from the repository root.

Name new subskills with lowercase kebab-case, for example `cpu-affinity-optimization`. Keep generated output under ignored local paths such as `output/`.

## Testing Guidelines

No formal test suite is defined yet. For script changes, run the modified script with safe sample inputs and verify generated files or JSON output. For workflow or documentation changes, check that referenced files, commands, and platform entry points still exist.

## Commit & Pull Request Guidelines

Recent history uses short imperative messages, often with prefixes such as `fix:` and `docs:`. Follow that style, for example `docs: update report workflow` or `fix: handle missing network tool`.

Do not force push to `main`. Put changes on a feature branch and merge through a Gitee Merge Request. PR/MR descriptions should summarize the changed skill behavior, list validation commands run, and mention any compatibility updates in `.claude/`, `.opencode/`, or `.agents/`.
