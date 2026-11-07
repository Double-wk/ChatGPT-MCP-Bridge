# Repository instructions

## Version policy

The user explicitly started v3 on 2026-11-07. Implement and commit subsequent changes on main. Do not create version branches.
The v3 release consolidates the post-v2 functional changes into one release commit, tagged v3.0.0.
Per the user, fold subsequent v3 fixes into that same release commit rather than publishing separate fix commits. Keep both its author and committer timestamps at 2026-11-07T20:45:54+08:00; update v3.0.0 to the consolidated commit while preserving its original tagger timestamp.
Keep the existing v1.0.0 and v2.0.0 tags and their commit timestamps fixed.
Do not amend the v2 baseline or move its tag to incorporate later work.
See docs/versioning.md for the baseline time and current release policy.
