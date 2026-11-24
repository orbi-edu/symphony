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

## Tightening an API contract in one test double leaves the other doubles of that API unchecked
- If a repo has more than one fake for the same external API, a validation added to one of them only covers the tests that use that one.
- Pitfall: we added a rule to the shared Linear mock: `Store.insert` rejects any client `id` that is not a v4 UUID. Tests built on a different hand-rolled double (`FakeLinear` in `tests/test_linear_tools.py`) never ran that rule, so they kept passing even before the fix. That left gaps in coverage nobody could see. Next action: before you change behavior that an external API enforces, find every test double for that API. Search tests/ for the API's mutation names, for example `issueCreate` and `issueRelationCreate`. Add the same check to each double. Then confirm that the tests behind each double fail before the fix and pass after it.  <!-- source: ORB-668 -->
