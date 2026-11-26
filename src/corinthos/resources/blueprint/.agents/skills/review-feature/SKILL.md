---
name: review-feature
description: Independently review the technical software features, described in "task/task_?/task-description.md" and "task/task_?/task-improvement.md", implemented into the corresponding project by another agent.
---

# Review Feature

## Mandatory Rules
1. You do not have any permission to change any of the implementations. You just observe and review the implementations made.
2. You have permission to write your observations and recommendations into "task/task_?/review-report.md".

## Workflow
1. You will receive a number from your caller skill agent. You will operate in "task/task_?/", with "?" being the number you received.
2. If "task/task_?/task-improvement.md" is not empty, consider the checked implementations of "task/task_?/task-improvement.md" as already done and to be used for reference, and only review unchecked implementations. If none remain, report "No pending corrections.".
3. If "task/task_?/task-improvement.md" is empty, read the technical implementation description given in "task/task_?/task-description.md".
4. Another agent will implement the features, and you will only observe, analyze and review their work.
5. Read the "task/task_?/implement-report.md" report first. Look only for concrete flaws, errors and defects with concrete consequences. Consult relevant architecture/context only when needed to resolve a specific uncertainty.
6. Independently assess the evidence, rerun checks only for a specific unresolved concern.
7. Report everything you see as an error into "task/task_?/review-report.md". You are allowed to report "No actionable findings" for a clean report. Put the latest changes and verification results under "## Current status". Preserve previous runs under "## History".
