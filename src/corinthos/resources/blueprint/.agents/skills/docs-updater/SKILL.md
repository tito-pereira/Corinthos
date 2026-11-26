---
name: docs-updater
description: Update the necessary project's documentation to take into account the recently made changes.
---

# Docs Updater

## Usage Guidelines
1. You do not have any permission to do or change any part of the implementation yourself.
2. You may update Markdown documentation and invoke the required continuity-preserving cleanup command; do not change product implementation.

## Workflow
1. Read "task/implementation-groups.md", where you will find all the implementations that have been made after the current documentation was made.
2. Use $analyze-architecture to understand the current project, what and where exactly do the recent changes affect documentation.
3. Do the appropriate documentation changes into "context/CURRENT_STATE.md", "context/ARCHITECTURE.md" and "context/PROJECT.md" based on the analysis made. Delete or replace old documentation if they no longer represent the current project.
4. If there is a github repo connected to the project, do the appropriate changes into that github repo README.md based on the analysis and implementations made.
5. Preserve completed/pending work, blockers, verification, resume information and unrelated pending decisions, then run `corinthos cleanup planning --root <project> --summary "<concise outcome and resume information>"`. The command consolidates before resetting planning files and clearing task_agents. Stop on failure; do not clear them directly.
6. Do the appropriate documentation changes into the "context_agents/" folder. Delete or replace old documentation if they no longer represent the current project. Preserve `context_agents/corinthos/`, which contains durable application continuity.
7. Report back to the User or caller skill.
