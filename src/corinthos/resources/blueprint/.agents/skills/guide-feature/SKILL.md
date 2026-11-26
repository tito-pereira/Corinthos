---
name: guide-feature
description: Coordinate subagents to implement the technical software features, described in "task/task_?/task-description.md", into the corresponding project.
---

# Guide Feature

## Mandatory Rules
1. You do not have any permission to do or change any part of the implementation yourself.
2. You coordinate a subagent that implements it, a subagent that reviews it, and analyze the two reports that both of them give.

## Workflow
1. You will receive a number from your caller skill agent. You will operate in "task/task_?/", with "?" being the number you received.
2. Create a subagent to use $implement-feature to implement the feature described in "task/task_?/task-description.md", into the corresponding project, and pass it the received number.
3. Wait for the implementer subagent to finish and write its report in "task/task_?/implement-report.md".
4. Create a subagent to use $review-feature to review the feature described in "task/task_?/task-description.md", in the corresponding project, and pass it the received number.
5. Wait for the reviewer subagent to finish and write its report in "task/task_?/review-report.md".
6. Read the report file from the implement subagent in "task/task_?/implement-report.md".
7. Read the report file from the review subagent in "task/task_?/review-report.md".
8. Analyze the two reports against each other to look only for concrete flaws, errors and defects with concrete consequences.
9. Make your own report with your conclusion on whether the implementation is correctly applied and ready to merge with no actionable findings or needs modifications and requires changes.
10. Present your report to the User when it's done.
