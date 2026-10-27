# Thesis-Refiner Bridge

This generic skill does not replace the thesis-refiner workflow.

Use the local thesis-refiner skill when the user mentions any of these:

- `/论文精炼助手`
- `论文精炼助手`
- `论文精选助手`
- `thesis-refiner`
- NotebookLM/NLM validation for thesis chapters
- Chinese dissertation refinement

## Relationship

`multi-agent-collaboration` provides generic coordination primitives:

- pane discovery
- task packages
- callback-first supervision
- consensus and audit standards

The thesis-refiner skill owns domain-specific rules:

- A5/PDF source gates
- NLM/NotebookLM validation gates
- thesis chapter review criteria
- thesis-specific prompt packs and evidence format

## How To Combine

1. Load the thesis-refiner skill first for domain rules.
2. Use this skill only for tmux/smux collaboration mechanics.
3. Do not weaken thesis-refiner gates for speed.
4. When writing a task pack for thesis work, include both the thesis-specific evidence requirements and this skill's callback format.

## Borrowing Boundary

The thesis-refiner workflow may reuse these generic mechanics:

- identity preflight and pane role naming;
- `multi-agent-role-map.json` for session recovery;
- `setup-check -> preflight -> bridge-test` before dispatch;
- callback-first supervision and low-frequency watchdogs;
- read-act-read bridge discipline and message ledger ideas;
- snapshot/restore as a way to recover pane/session metadata.

It must not import these generic experiments into real NLM work:

- ConPTY/wmux as a default runtime;
- AO/OpenRig-style platform orchestration as a replacement for thesis checkpoints;
- Thrum/message-bus as a replacement for required `tmux-bridge` Push;
- dashboards or daemons as proof that A5/PDF-only/NLM gates were satisfied.

In short: use `multi-agent-collaboration` to make pane identity and communication harder to get wrong; keep thesis-refiner's red lines, NLM evidence, `agent-state`, `executor_logs`, and `approved-plan` as the authority.

## Skill-Set Design Note

If this workflow is published as a reusable skill set, keep the generic multi-agent skill and thesis-refiner adapter as separate modules under one collection. Their shared dependency is the WSL/tmux environment doctor, not the research-domain logic.
