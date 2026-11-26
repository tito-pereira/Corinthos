# Task artifacts

For positive group number N, use only `task/task_N/` and
`task_agents/task_N_agents/`, relative to the project workspace.
Never create root-level numbered task folders.

Each `task/task_N/` contains exactly these five Markdown files:

- implementation-steps.md
- task-description.md
- implement-report.md
- review-report.md
- task-improvement.md

Create `task_agents/task_N_agents/` empty; supporting reports belong there.
The application can create these paths when a group-bound skill is invoked.
Keep durable context outside both numbered folders. Before report reset,
group deletion or final planning cleanup, use the corresponding `corinthos
cleanup` command with a concise summary; consolidation must succeed first.
