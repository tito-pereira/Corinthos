---
name: merge-feature
description: Merge the technical software features, described in "task/task_?/task-description.md" and implemented into the corresponding project, into the corresponding GitHub repository main branch, and do the post-merge CI.
---

# Merge Feature

## Mandatory Rules
1. You do not have any permission to change any of the implementations.
2. You have permission to merge into the project's main GitHub branch.

## Workflow
1. You will receive a number from your caller skill agent. You will operate in "task/task_?/", with "?" being the number you received.
2. If the recently finished technical software features were implemented on another branch rather than the "main", merge that branch into the main.
3. Execute the post-merge CI.
4. If any error occurs or you identify any error, either in the merge or post-merge CI, suspend the rest of this skill, make a report about it and present it to the User.
5. Else, if everything works, check the recently implemented features in "task/task_?/task-description.md" as already completed inside "task/task_?/implementation-steps.md".
6. Use $clean-reports and pass it the number you received, to reset the task, improvement, implement and review report files.
7. Report the success conditions back to the User.

Before the clean-reports handoff, include verified outcomes, pending work and resume information in its consolidation summary. Never reset temporary artifacts directly.
