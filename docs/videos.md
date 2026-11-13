# Videos

Each video has English captions. Each one is built from the sources in `docs/media/<name>/` with that folder's `render.sh`. The videos use the screenshots in [docs/tern/](tern/) and the figures in [certification.md](certification.md) and [mocked-certification.md](mocked-certification.md).

| Video | Length | Watch it to | Read next |
|---|---|---|---|
| [Why Symphony](media/symphony-why.mp4) ([captions](media/symphony-why.vtt)) | 2:24 | Decide if Symphony fits your team: what it gives, why you can trust it, and when not to use it. | [README](../README.md) |
| [Explainer](media/symphony-explainer.mp4) ([captions](media/symphony-explainer.vtt)) | 3:20 | Get an overview of Symphony: the architecture, a ticket's life, people in the loop, the merger, credentials and proof. | [README](../README.md) |
| [Install and set up](media/symphony-setup.mp4) ([captions](media/symphony-setup.vtt)) | 3:08 | Install on Kubernetes or in local mode, then run `symphony setup` or open Setup in Tern. | [install.md](install.md) |
| [One ticket, end to end](media/symphony-lifecycle.mp4) ([captions](media/symphony-lifecycle.vtt)) | 4:57 | Follow a ticket from dispatch to Done: the workspace, attempts, rework, retries, holds, investigations, sub-issues and learnings. | [concepts.md](concepts.md) |
| [The merger](media/symphony-merger.mp4) ([captions](media/symphony-merger.vtt)) | 5:23 | See how the merger watches, reviews, comments on and merges pull requests. | [merger.md](merger.md) |
| [Operating from Tern](media/symphony-tern.mp4) ([captions](media/symphony-tern.vtt)) | 4:28 | Use the dashboard, live chat, steer and take over, tickets, the merger view and Admin. | [tern.md](tern.md) |
| [Trackers and notifications](media/symphony-integrations.mp4) ([captions](media/symphony-integrations.vtt)) | 3:48 | Compare Linear and GitHub Projects as the tracker, and Slack and Discord for notices. | [configuration.md](configuration.md) |
| [The assistant](media/symphony-assistant.mp4) ([captions](media/symphony-assistant.vtt)) | 3:23 | Ask, research and plan in Slack or Discord, and confirm proposed tickets. | [assistant.md](assistant.md) |
| [MCP servers](media/symphony-mcp.mp4) ([captions](media/symphony-mcp.vtt)) | 2:55 | Give a role more tools while the secret headers stay in the gateway. | [mcp.md](mcp.md) |
| [Symphony and Seiji](media/symphony-seiji.mp4) ([captions](media/symphony-seiji.vtt)) | 3:29 | Give agents an iOS simulator from Seiji: the preset, the token swap, evidence, cleanup and watching a device. | [seiji.md](seiji.md) |
| [Credentials and isolation](media/symphony-security.mp4) ([captions](media/symphony-security.vtt)) | 3:57 | Understand the trust zones, the network fence, the tokens and the audit. | [security.md](security.md) |
| [How Symphony is proven](media/symphony-testing.mp4) ([captions](media/symphony-testing.vtt)) | 3:14 | See the vendor mocks, the mocked end-to-end runs and the certifications, with their limits. | [testing.md](testing.md) |

## Rebuild a video

The build needs `uv`, `curl`, `ffmpeg` and Node.js. The first run downloads the narration model and checks its sha256.

```bash
docs/media/<name>/render.sh
```
