---
name: clean-reports
description: Reset a group's temporary reports after durable consolidation.
---

# Clean Reports

1. Receive positive group number N. Read the current reports in `task/task_N/`.
2. Consolidate completed and pending work, blockers, verification and resume
   information; preserve unrelated pending decisions.
3. From any directory, run `corinthos cleanup reports --root <project> --group N
   --summary "<concise verified outcome and resume information>"`.
   The command persists continuity before resetting task-description.md,
   implement-report.md, review-report.md and task-improvement.md. It preserves
   implementation-steps.md. If consolidation fails, stop without clearing files.
4. Do not clear the reports through direct filesystem operations.
