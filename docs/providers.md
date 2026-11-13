# Models and providers

Symphony supports provider API keys, unauthenticated custom endpoints (`auth: none`), and host cloud credentials (for example Amazon Bedrock through IAM). It has no provider OAuth or subscription login. This document explains where the credentials live, how an admin chooses a model for each agent role, and how a credential reaches an engine.

## Contents

- [Summary](#summary)
- [Built-in provider keys](#built-in-provider-keys)
- [Custom providers](#custom-providers)
- [Host fallback](#host-fallback)
- [Amazon Bedrock](#amazon-bedrock)
- [The model catalog](#the-model-catalog)
- [Models for each agent role](#models-for-each-agent-role)
- [How a key reaches the engine](#how-a-key-reaches-the-engine)
- [Local mode](#local-mode)

## Summary

| Source | Where it lives | Who writes it | Wins over |
|---|---|---|---|
| Built-in provider key | Secret `symphony-providers` | an admin, from Tern or the CLI | the host fallback, for the same provider |
| Custom provider | Secret `symphony-providers` | an admin, through the API | — (it is the only source for its ID) |
| Host fallback | Secret `model-provider` (or the host environment) | the operator of the cluster | — |

The runner, the reflector and the merger read `symphony-providers` at the start of each agent run. A key saved from Tern applies to the next attempt with no restart.

The API never returns a stored key. The list of providers shows only `credentialConfigured: true` or `false`.

## Built-in provider keys

A built-in provider is a provider that the pinned `omp` already knows, for example `anthropic`, `openai` or `amazon-bedrock`. A key replaces only the credential. The models, the endpoint and the protocol stay the ones of `omp`.

To store a key in Tern:

1. Open the command **Symphony: Admin** (`plugin.symphony.admin`).
2. Select the **Providers** tab.
3. Select the provider. For a provider that is not in the list, select **Other omp provider…**.
4. Select **Set key** (or **Replace key**) and paste the key.

To store a key with the CLI:

```sh
symphony admin provider key anthropic          # hidden prompt
printf '%s' "$KEY" | symphony admin provider key anthropic   # from stdin
symphony admin provider list
```

The CLI reads the key from standard input or a hidden prompt, never from a command-line argument.

Rules:

- The token role must be `admin`.
- The provider ID must be in the catalog with the credential kind `apiKey`. Otherwise the API answers `422 unknown_provider`.
- The stored record is `{builtin: true, apiKey}`.

## Custom providers

A custom provider is an endpoint of your own that speaks a known protocol. Define it with `PUT /v1/admin/providers/{id}` (token role `admin`, `If-Match` with the current revision):

```json
{
  "name": "Internal gateway",
  "api": "openai-completions",
  "baseUrl": "https://llm.example.com/v1",
  "auth": "apiKey",
  "apiKey": "<key>",
  "models": [
    {"id": "example-model", "name": "Example model", "contextWindow": 128000, "maxTokens": 8192}
  ]
}
```

| Field | Rule |
|---|---|
| `name` | 1 to 100 characters. |
| `api` | `openai-completions`, `openai-responses`, `anthropic-messages` or `google-generative-ai`. |
| `baseUrl` | An absolute URL with no credentials, query or fragment. |
| `auth` | `apiKey` or `none`. |
| `apiKey` | Write-only. Omit it to keep the stored key. With `auth: apiKey` and no stored key, the API answers `provider_key_required`. |
| `{id}` (in the path) | Lowercase letters, digits and `-`, starting with a letter, at most 48 characters. The Secret holds at most 100 providers. |
| `models` | 1 to 50 models. Each has an `id` (no spaces) and optional `name`, `contextWindow`, `maxTokens` (not more than `contextWindow`), `reasoning`, `input` (`text`, `image`) and `cost` (`input`, `output`, `cacheRead`, `cacheWrite` per million tokens). |

A custom provider carries only its own key. The host fallback does not apply to it.

## Host fallback

The Secret `model-provider` holds host-level model credentials as environment variables. Each engine gets them in addition to a built-in provider key, and with no stored item for the provider:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: model-provider
stringData:
  AWS_REGION: us-east-1
  AWS_BEARER_TOKEN_BEDROCK: <Bedrock API key>
  # ANTHROPIC_API_KEY: <key>
  # OPENAI_API_KEY: <key>
```

- The runner and the reflector read this Secret through the Kubernetes API at each attempt.
- The merger gets it with `envFrom` (optional).
- When `symphony-providers` has a key for the same provider, that key wins.
- The runner removes `LINEAR_API_KEY` and the `GITHUB_APP_*` variables from this environment, even if someone puts them in the Secret.

## Amazon Bedrock

Bedrock is the provider of the built-in team configuration.

| Consumer | Credential |
|---|---|
| Workspaces (implementer) | `AWS_REGION` and `AWS_BEARER_TOKEN_BEDROCK` (a Bedrock API key), or the access keys of an IAM user that can only call Bedrock. The NetworkPolicy blocks the instance metadata endpoint, so a workspace cannot use an instance role. |
| Merger | The same Secret, or a cloud role. On EKS, annotate the `symphony-merger` ServiceAccount for IRSA and remove `AWS_BEARER_TOKEN_BEDROCK`. On EC2 with an instance role, set the IMDSv2 hop limit to 2. |

The minimum IAM policy allows `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream` on the models and inference profiles that you use. Model IDs that start with `global.` are global inference profiles.

## The model catalog

`GET /v1/admin/catalog` (token role `viewer`) lists the providers and models of the pinned `omp`. Tern uses it in Admin › Models and Admin › Providers.

- The source is the `@oh-my-pi/pi-catalog` package of the pinned version, installed at `/opt/symphony/omp-catalog/models.json`. `SYMPHONY_OMP_CATALOG` changes the path.
- Each provider has a credential kind from the compiled rules of `omp`:

| Kind | Meaning | In the catalog |
|---|---|---|
| `apiKey` | An admin can store a key. | yes |
| `host` | The credential comes from the host (for example cloud default credentials). | yes |
| `oauth` | The provider works only with a login: an OAuth token, a `/login` flow, or a session that a provider hook renews. | no |

Symphony does not offer login-only providers, for example `openai-codex`, `github-copilot` and `cursor`.

## Models for each agent role

The team configuration gives each of the ten agent roles a `provider`, a `model` and a `thinking` level. [configuration.md](configuration.md#team-configuration) lists the defaults and the publication rules.

To change a model in Tern:

1. Open **Symphony: Admin** and select the **Models** tab.
2. Select a role, then a provider, a model and a thinking level.
3. Select **Publish N changes** and give a reason.
4. Look at the rollout list. Each consumer shows the generation that it applied.

To change a model with the CLI:

```sh
symphony admin config set implementer=anthropic/<model-id>:high --reason "Move the implementer to Anthropic"
```

> **Note:** The `oracle` and the `arbiter` must use a different provider or model than the `reviewer`. A finding blocks a pull request only when two independent models agree.

Each consumer reads the configuration at the start of each unit of work. The precedence is in [concepts.md](concepts.md#precedence). The model must also have a credential: a stored key, a custom provider, or the host fallback. Otherwise the engine rejects the first prompt and the attempt fails with a message such as `No API key found for <model>`.

## How a key reaches the engine

For each attempt, the runner (on the trusted side) does these steps:

1. It deletes the previous private engine directory in the workspace.
2. It reads `symphony-providers` and finds the item for the role's provider.
3. It builds the environment:
   - Built-in key: the host fallback plus `SYMPHONY_PROVIDER_API_KEY=<key>`.
   - Custom provider: only `SYMPHONY_PROVIDER_API_KEY=<key>`.
   - No item: only the host fallback.
4. It writes `/run/symphony/engine/models.yml` and `/run/symphony/engine.env` with mode `0600`. `models.yml` names the variable `SYMPHONY_PROVIDER_API_KEY`, not the key.
5. It starts the engine with `PI_CODING_AGENT_DIR=/run/symphony/engine`.

The key is never in a command line, a log or Redis. The merger does the same in a private directory (mode `0700`) for each agent run. If the merger cannot read the Secret, the agent run fails before the engine starts.

## Local mode

In local mode, the engines use the model credentials of the host environment (for example the AWS variables, or the instance role of the machine). `/v1/admin/providers` answers 503. A merger that runs outside Kubernetes (no `KUBERNETES_SERVICE_HOST`) also uses the host environment and does not read `symphony-providers`.
