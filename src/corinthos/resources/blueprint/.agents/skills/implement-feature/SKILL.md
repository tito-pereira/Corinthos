---
name: implement-feature
description: Implement the technical software features, described in "task/task_?/task-description.md" and "task/task_?/task-improvement.md", into the corresponding project.
---

# Implement Feature

## Workflow
1. You will receive a number from your caller skill agent. You will operate in "task/task_?/", with "?" being the number you received.
2. If "task/task_?/task-improvement.md" is not empty, consider the checked implementations of "task/task_?/task-improvement.md" as already done and to be used for reference, and implement only unchecked corrections. If none remain, report "No pending corrections.".
3. If "task/task_?/task-improvement.md" is empty, read the technical implementation description given in "task/task_?/task-description.md".
4. Pre-organize the technical implementation with the most efficient plan.
5. Implement it.
6. Run the smallest relevant checks. Repeat only when subsequent changes invalidate their results.
7. Report everything you do into "task/task_?/implement-report.md". Put the latest changes and verification results under "## Current status". Preserve previous runs under "## History".
