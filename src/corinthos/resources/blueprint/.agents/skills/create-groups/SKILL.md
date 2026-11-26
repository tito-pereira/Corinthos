---
name: create-groups
description: Translate a guide of planned new features into an actual list of technical and independent implementation groups.
---

# Create Groups

## Workflow
1. Study, learn and analyze the current project's planned new features in "context/DECISIONS.md".
2. Study, learn and analyze the current project's architecture using $analyze-architecture.
3. Divide the desired new features into separate and non-dependent implementation groups. Each group will be implemented in parallel, so it's extremely important to guarantee that, when creating the groups, their implementation does not interfere with other groups' implementations.
4. Write down your list of groups in "implementation-groups.md" in the "task" folder.
5. For each group, put an "Implemented / Suspended / In Progress / Not Implemented" option for future reference to guide what's already implemented or not.
6. Report back to the User or the caller skill the number of groups and a brief description on all of them.
