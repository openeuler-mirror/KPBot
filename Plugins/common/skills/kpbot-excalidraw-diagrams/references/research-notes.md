# Excalidraw skill research — 2026-10-03

Scope: public repository documentation and targeted source inspection. This is not a full repository security audit, and none of these third-party skills was installed or copied into this skill.

| Candidate | Useful capabilities | Gap for this request |
| --- | --- | --- |
| [coleam00/excalidraw-diagram-skill](https://github.com/coleam00/excalidraw-diagram-skill) | Visual design guidance and render/inspect iteration | Inspected workflow relies on visual correction; a deterministic arrow/obstacle acceptance gate was not established |
| [AlanYu04/excalidraw-generator-skill](https://github.com/AlanYu04/excalidraw-generator-skill) | Programmatic generation, CJK sizing, verification helpers | In inspected `core/engine.py`, `check_overlaps` skips arrows/lines and contained pairs; binding checks do not establish full arrow-path clearance |
| [yctimlin/mcp_excalidraw](https://github.com/yctimlin/mcp_excalidraw) | Live-canvas MCP/CLI, Mermaid conversion, screenshot-based refinement | Requires a running canvas service and browser for the visual workflow; strict collision prevention was not established from the inspected skill |
| [duhman/excalidraw-agent](https://github.com/duhman/excalidraw-agent) | Scene conversion/linting references and integration workflow | Imported diagrams may still need manual arrangement |

Targeted snapshots: coleam00 `8646fcc9f74f38539c6cdb4c969723336a96ddcd`; AlanYu04 `6bbe9eecc4a022d0d869a00205064aa0ecb1bad3`; yctimlin `46e1146a6b031cb2891ea9162b7570af40680dbf`; duhman `a025f1f158608a10be4467832e13d6179bfc323f`. Findings apply to those inspected versions, not all possible versions or features.

There are useful existing skills. The reason for this local implementation is the combination of editable output, local prose/Markdown/Mermaid workflow, explicit group membership, and an enforceable arrow-path collision gate. This is independently written implementation code using published libraries, not a fork of a candidate skill.

Primary format and library references:

- [Excalidraw element skeleton API](https://docs.excalidraw.com/docs/@excalidraw/excalidraw/api/excalidraw-element-skeleton)
- [Official Mermaid-to-Excalidraw project](https://github.com/excalidraw/mermaid-to-excalidraw)
- [Mermaid parser API](https://mermaid.js.org/config/setup/mermaid/interfaces/Mermaid.html)
- [ELK JavaScript layout engine](https://github.com/kieler/elkjs)

Runtime dependencies are pinned and retained with their upstream package licenses. Package installation needs registry access; generation and preview rendering do not send diagram contents to external services. No credentials, account permissions or browser sessions are requested by the engine.
