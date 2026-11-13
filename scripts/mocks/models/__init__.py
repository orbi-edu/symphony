"""Model-provider mock: Anthropic Messages and OpenAI Responses, streamed (internals §14).

One server answers both vendors under path prefixes, so a single port (4400) serves a
whole certification: `http://HOST:PORT/anthropic` is an Anthropic base URL and
`http://HOST:PORT/openai/v1` an OpenAI one. Requests are checked against the vendored
specs in `scripts/mocks/specs/` and answered from a deterministic script.
"""
