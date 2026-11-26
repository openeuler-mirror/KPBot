// Copyright (c) 2026 Huawei Technologies Co., Ltd.
// DSH integration: the package catalog owns discovery, not domain directory names.
import { fileURLToPath } from 'node:url';
import { discoverSkills, readSkill } from './catalog.mjs';

export const inject = ['skills'];
const repositoryRoot = fileURLToPath(new URL('../../', import.meta.url));

export function apply(ctx) {
  ctx.effect(() => ctx.skills.registerProvider(() => ({
    name: 'kpbot-dsh',
    async list({ signal } = {}) {
      signal?.throwIfAborted();
      const entries = await discoverSkills(repositoryRoot);
      signal?.throwIfAborted();
      return entries.map(entry => ({
        name: entry.name,
        description: entry.description,
        path: entry.path,
        resourceBase: { kind: 'directory', path: entry.resourceDir },
        invocation: entry.invocation ?? { modelInvocable: true, userInvocable: true },
        source: 'bundled',
        provider: 'kpbot-dsh',
        rank: 600,
        locator: entry,
      }));
    },
    async get(candidate, { signal } = {}) {
      signal?.throwIfAborted();
      const skill = await readSkill(candidate.locator, repositoryRoot);
      signal?.throwIfAborted();
      // Never retain a stale catalog selection after a rename.
      if (skill.name !== candidate.name) return undefined;
      return {
        name: skill.name,
        description: skill.description,
        path: candidate.path,
        resourceBase: candidate.resourceBase,
        invocation: skill.invocation,
        source: 'bundled',
        provider: 'kpbot-dsh',
        content: skill.content,
      };
    },
  })));
}
