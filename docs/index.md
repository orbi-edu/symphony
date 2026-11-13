# Symphony documentation

Use this map to find the document for your task.

## Start here

| Document | Read it to |
|---|---|
| [README](../README.md) | Learn what Symphony is and start it quickly. |
| [Explainer video](media/symphony-explainer.mp4) ([captions](media/symphony-explainer.vtt)) | Watch a 3-minute overview of Symphony. |
| [videos.md](videos.md) | Watch the twelve videos: why Symphony, setup, a ticket's life, the merger, Tern, integrations, the assistant, MCP, Seiji, security and testing. |
| [concepts.md](concepts.md) | Understand tickets, attempts, sessions, agent roles, holds, investigations, multi-repository tickets and learnings. |
| [glossary.md](glossary.md) | Find the meaning of a term. |

## Install and operate

| Document | Read it to |
|---|---|
| [install.md](install.md) | Install on Kubernetes, k3s, kind or in local mode. Connect GitHub, the tracker (Linear or a GitHub Projects board), Slack, Discord and Tern. |
| [examples/](examples/) | Copy a complete `WORKFLOW.md`: [GitHub Projects tracker](examples/WORKFLOW.github.md), [Node.js](examples/WORKFLOW.node.md), [.NET](examples/WORKFLOW.dotnet.md). |
| [configuration.md](configuration.md) | Look up every `WORKFLOW.md` key, environment variable and Secret, and the team configuration. |
| [providers.md](providers.md) | Store model API keys, define custom providers and choose a model for each agent role. |
| [security.md](security.md) | Understand the trust boundaries, the credentials, the token roles and the network policy. |
| [observability.md](observability.md) | Scrape the Prometheus metrics, and alert on a down daemon, a silent merger, failing attempts, rate limits and incomplete setup. |
| [troubleshooting.md](troubleshooting.md) | Find the cause and the fix of a problem. |

## Use

| Document | Read it to |
|---|---|
| [tern.md](tern.md) | Use the Tern plugin, the live chat and the admin views. |
| [cli.md](cli.md) | Use the `symphony` command. |
| [merger.md](merger.md) | Understand the AI review and the merge train, and write a `.symphony/merger.md` policy. |
| [mcp.md](mcp.md) | Give agents extra tools with MCP servers, per agent role. |
| [seiji.md](seiji.md) | Let agents test apps on iOS simulators with Seiji, with evidence on the ticket. |
| [assistant.md](assistant.md) | Ask, research and plan tickets with the Slack and Discord assistant. |

## Reference and development

| Document | Read it to |
|---|---|
| [architecture.md](architecture.md) | See the components, the data stores, the deployment and the sequence diagrams. |
| [api.md](api.md) | Call the HTTP API, the event stream, MCP and the webhooks. |
| [internals.md](internals.md) | Read the contracts between the components: Redis keys, intents, host tools, fencing. |
| [testing.md](testing.md) | Run the tests, the vendor mocks and the end-to-end suites. |
| [features.md](features.md) | See the list of features and how each one is verified. |
| [certification.md](certification.md) | See the results of the end-to-end certification runs. |

## Diagrams

| Diagram | Document |
|---|---|
| System context | [architecture.md](architecture.md#system-context) |
| Deployment on Kubernetes | [architecture.md](architecture.md#deployment-on-kubernetes) |
| Local mode | [architecture.md](architecture.md#local-mode) |
| Ticket lifecycle | [concepts.md](concepts.md#ticket-lifecycle), [architecture.md](architecture.md#ticket-lifecycle) |
| Internal status of a ticket | [concepts.md](concepts.md#internal-status) |
| Attempt sequence | [architecture.md](architecture.md#attempt-sequence) |
| Merger review flow | [architecture.md](architecture.md#merger-review-flow) |
| Merge train flow | [architecture.md](architecture.md#merge-train-flow) |
| Credential flow | [architecture.md](architecture.md#credential-flow) |
| Live chat and steer | [architecture.md](architecture.md#live-chat-and-steer) |
| MCP proxy and assistant | [architecture.md](architecture.md#mcp-proxy-and-assistant) |
| MCP request flow | [mcp.md](mcp.md#how-a-request-flows) |
| Proposal and confirmation | [assistant.md](assistant.md#proposals-and-confirmation) |
| Trust boundaries | [security.md](security.md#trust-boundaries) |
