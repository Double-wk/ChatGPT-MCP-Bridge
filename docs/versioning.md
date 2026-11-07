# Version policy

- Current release line: v3 / 3.0.0
- Branch: main; do not create version branches.
- Release tag: v3.0.0, pointing to the consolidated v3 release commit.
- Fixed v3 start time (Asia/Shanghai): 2026-11-07T20:45:54+08:00 (author and committer timestamps).
- Preserve the original v3 tagger timestamp: 2026-11-07T20:45:55+08:00.

Per the user, include subsequent v3 fixes in the same consolidated release commit and update v3.0.0 accordingly; do not publish separate fix commits. This includes the CI runner update from retired macOS 14 to macOS 15.

The user explicitly started v3 on 2026-11-07. The v3 release consolidates macOS support, persistent SSH sessions, custom SSH ports, password authentication, Python 3.11 loopback compatibility, and the macOS Conda / Tailscale Funnel / GitHub OAuth deployment.

Public release tags are v1.0.0, v2.0.0, and v3.0.0. Functional changes are included in their release commit. The v3 commit follows the original v2.0.0 baseline on main.

## Fixed v2 baseline

- Baseline tag: v2.0.0
- Baseline commit: fe1801996e302e7797cea4a32d6cf5e26feed5ce
- Baseline commit time (Asia/Shanghai): 2026-10-26T14:42:43+08:00

Keep v1.0.0 and v2.0.0 and their commit timestamps unchanged. Do not amend the v2 baseline or move its tag to incorporate later work.
