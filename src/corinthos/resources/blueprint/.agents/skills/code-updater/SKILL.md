---
name: code-updater
description: Clean up old code and old testing in the project's repo. Test that behaviour remains.
---

# Code Updater

## Worfklow
1. Use the $analyze-architecture skill to get context into the project.
2. Read "task/implementation-groups.md" to get context into the most recent changes into the project.
3. Create a "code-updates.md" file. This file will be used for context of every action you take, in case there's a need for a rollback.
4. Analyze the project's repo. Find old code, files, folders, and testing cases, when you understand that they are no longer relevant or part of the current project's architecture and can be deleted without creating errors.
5. If you don't see anything to clean up, or it was already flagged in "code-updates.md", skip to Step 9.
6. Write into "code-updates.md" what you intend to delete or clean up. Then proceed with said deletion or clean up.
7. Test specifically for that change. If the testing fails, rollback the changes, but maintain the context in "code-updates.md" to avoid duplicate flagging of old code.
8. Return to Step 4.
9. If any changes were made, run the global CI test suite.
10. If no changes were made, skip to Step 13.
11. In case of any fails in testing, rollback only the minimum necessary changes in "code-updates.md" for the global test suite to pass.
12. Only proceed once the global test suite passes.
13. Delete the "code-updates.md" file.
14. Return back to the User or the caller skill with a report on any changes made.
