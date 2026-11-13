---
lanes:
  - {key: all, audits: "every audit: correctness, regressions, the ticket's acceptance criteria and tests"}
models:
  reviewer: {provider: amazon-bedrock, model: global.anthropic.claude-opus-5-5, thinking: low}
  oracle: {provider: amazon-bedrock, model: global.moonshotai.kimi-k3.1, thinking: low}
  arbiter: {provider: amazon-bedrock, model: global.moonshotai.kimi-k3.1, thinking: low}
  developer: {provider: amazon-bedrock, model: global.anthropic.claude-opus-5-5, thinking: low}
merge_method: rebase
batch_max: 2
ci: {checks: [test], workflow: CI}
main_watch: {ticket: true, state: Todo, labels: [ci]}
---
A docstring that says what a function returns is its specification: code that returns
something else is a `bug`, even when no test covers it.
