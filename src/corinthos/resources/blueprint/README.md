# Project Workspace

Other agents may only read this README; they must not modify it.

- The main project repository belongs in `repo/`.
- `context/` should contain only `PROJECT.md`, `DECISIONS.md`, `ARCHITECTURE.md` and `CURRENT_STATE.md`. Write and read any extra agent context files in `context_agents/`.
- `task/` should contain only `README.md`, `implementation-groups.md` and skill-created `task/task_N/` folders, where N is a positive group number.
- Each numbered `task/task_N/` folder should contain only `implementation-steps.md`, `task-description.md`, `implement-report.md`, `review-report.md` and `task-improvement.md`. Write and read any extra agent task context and reports in `task_agents/task_N_agents/`.

## Context

- product purpose belongs in `PROJECT.md`;
- architecture belongs in `ARCHITECTURE.md`;
- implementation status belongs in `CURRENT_STATE.md`;
- binding decisions belong in `DECISIONS.md`.

### `PROJECT.md`

Defines what the project is. Use it for:
- product vision;
- problem being solved;
- product model;
- scope;
- major concepts;
- users;
- product principles.
It must not become an implementation log or roadmap.

### `ARCHITECTURE.md`

Defines how the project is currently structured. Use it for:
- architectural boundaries;
- components and responsibilities;
- data and control flows;
- persistence boundaries;
- provider architecture;
- platform boundaries;
- invariants.
Planned architecture must never be presented here as implemented architecture.

### `CURRENT_STATE.md`

Defines what is actually implemented now. Use it for:
- currently available capabilities;
- current version/state;
- verified limitations;
- incomplete capabilities;
- relevant operational constraints.
Planned functionality must never be presented here as implemented functionality.

### `DECISIONS.md`

Contains product and architectural decisions planned to implement.
Only write to or read from it with specific User permission or skills.
