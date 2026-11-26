---
name: auto-code
description: Become the supervisor of other agents for the current implementation list in this task.
---

# Auto Code

## Usage Guidelines
1. You do not have any permission to do or change any part of the implementation yourself.
2. You have the power to invoke other skills that manage subagents.

## Workflow
1. You will be assigned a folder in the form of "task/task_?/" with "?" being a number corresponding to a larger list of implementation groups.
2. If "task/task_?/implementation-steps.md" still has unchecked features to implement, use $create-feature and pass it the corresponding number.
3. If the feature has successfully been created into "task/task_?/task-description.md", use $guide-feature and pass it the corresponding number.
4. If guide-feature has successfully reported no errors or failures and only success conditions, use $merge-feature and pass it the corresponding number.
5. If merge-feature returned success conditions and no problems, skip to step 10.
6. Else, gather the problems returned and the corresponding analysis and pass them as input into the skill $create-improvement, use it, and pass it the corresponding number.
7. After the improvement has successfully been created and converted into tasks, use $guide-improvement, and pass it the corresponding number.
8. If guide-improvement has successfully reported no errors or failures and only success conditions, use $merge-feature, and pass it the corresponding number.
9. Return to step 5.
10. Return to step 1 and continue on this implementation loop until every feature in "task/task_?/implementation-steps.md" is checked as implemented successfully.
11. Report back to the User or caller skill with the success conditions.
