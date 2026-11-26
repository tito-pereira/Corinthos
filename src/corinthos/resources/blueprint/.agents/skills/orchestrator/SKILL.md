---
name: orchestrator
description: Become the main software orchestrator for the current implementation groups in this project.
---

# Orchestrator

## Usage Guidelines
1. You do not have any permission to do or change any part of the implementation yourself.
2. You have the power to invoke other skills that manage subagents.

## Workflow
1. Check for the next "Suspended" or "Not Implemented" group of implementations in "task/implementation-groups.md".
2. If everything is already "Implemented" and every subagent has reported back, skip to step 11.
3. Otherwise, change that group to "In Progress" and retrieve its number.
4. Read "task/README.md". If "task/task_?/" and "task_agents/task_?_agents/" for the corresponding number already exist, skip to Step 7.
5. Else, using the retrieved number as the "?" variable, create "task/task_?/" and "task_agents/task_?_agents/" according to "task/README.md".
6. Call $create-steps-list with the corresponding number and wait for that skill to report back.
7. Create a subagent and make it use $auto-code for the "task/task_?/" folder for the corresponding group number.
8. If any "auto-code" called agent reports back with success, retrieve back the corresponding group number and: in "task/implementation-groups.md", change that group to "Implemented"; consolidate the completed/pending work, blockers, verification and resume information, then run `corinthos cleanup group --root <project> --group N --summary "<concise outcome and resume information>"`, where N is that group. The command removes only its canonical numbered task and agent folders after continuity is saved. Stop on failure; do not delete them directly.
9. If any "auto-code" called agent reports back with errors or interruptions, change that group to "Suspended".
10. Return to Step 1.
11. Use the $code-updater skill.
12. Use the $docs-updater skill.
13. Report the success back to the User or the caller skill with the final report on the implementation of every group and testing.
