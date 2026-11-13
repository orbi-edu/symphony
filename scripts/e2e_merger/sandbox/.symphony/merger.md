---
lanes:
  - key: correctness
    audits: "correctness: bugs, regressions and broken callers in the changed code"
  - key: spec-tests
    audits: "whether the PR does what its title and body say, and whether its tests check it"
ci:
  checks: [test]
  workflow: CI
merge_method: rebase
batch_max: 3
fix_slots: 2
---
`calc` is a tiny arithmetic library. Every public function has a docstring that says what it
returns: a function that returns something other than its docstring says is a `bug`. A missing
test for a working function is a nit.
