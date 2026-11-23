# AGENTS.md

Always-active conventions and lessons (curated via symphony-learnings).

## 4. Rules that you must obey
- Write all code, comments, documentation, commit messages, log events and user-facing text in
- A ticket that sent you here (an investigation of a failed run, say) is private; describe the  <!-- source: n/a -->

## 5. Commits and pull requests
- One commit has one purpose. Write the subject in the imperative mood, 72 characters or less,
- A pull request links its issue, says how you verified the change, and lists the risks.  <!-- source: n/a -->

## 6. When you are not sure
- Read the code and the documents first. Most answers are there.
- If a requirement is not clear, stop and ask. Do not guess about credentials, data loss or  <!-- source: n/a -->

## Teardown steps in finally must not raise past resource release
- If a best-effort kill or stop step in a finally block raises, the finish and release steps after it are skipped, and a completed attempt becomes a crashed one.
- Pitfall: the runner's finally sends SIGTERM to the engine and then calls kill_agent. If kill_agent raises, _finish and release_sandbox never run. The Job then exits 1 with backoffLimit 0, the daemon requeues the attempt as 'runner died mid-attempt', and that counts against the retry budget even though the work was already done. Next action: when adding or reviewing teardown code, put each cleanup step in its own try/except that logs the error. Always run release and finish. Report teardown failures as a separate status, not as a runner crash.  <!-- source: ORB-661 -->

## Treat a failed /proc/<pid> read as 'process gone', not as an error
- Scanning /proc is a check-then-use race: a process that is live at the State read can become a zombie or exit before the environ/cmdline read.
- Pitfall: code that scans /proc reads /proc/<pid>/status, skips Z/X states, then reads /proc/<pid>/environ. If that second read fails while /proc/<pid> still exists, the code treats it as a hard failure. This was reproduced in a workspace pod: reading a zombie's environ fails with 'No such process', and /proc/<pid> is still there. During teardown processes are exiting all the time, so this branch fires easily even for pids that are unrelated to the target. Next action: in any /proc scanner (stop or kill scripts, pid discovery), treat ENOENT/ESRCH on any per-pid read as 'skip this pid'. Re-read State after a failed read rather than checking only that the directory exists. Never let one unrelated pid's transient read failure decide the scan's exit code. When a stop script fails, report its exit code and stderr instead of dropping them; a generic 'could not be confirmed' message made this hard to diagnose.  <!-- source: ORB-661 -->
