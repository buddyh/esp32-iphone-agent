# Codex bridge

Use a locally authenticated Codex CLI as the vision backend for ESP32 Agent. The iPhone uses its OpenAI-compatible request format; this bridge translates requests into Codex app-server turns over stdio. This branch also hardens the iOS client to reject streamed errors and repeated empty responses. It does not automate a chat window or attach to an open desktop conversation.

For **a native Codex conversation that directly observes and controls the phone**, use the upstream legacy version's existing [CLI/stdio MCP adapter](https://github.com/onehumanbeing/esp32-iphone-agent/blob/fc8458c39d769b4a48d500fc6d53574bd7b77e9e/docs/remote-agents.md) instead. That route uses UxPlay on the Mac, normal shared Wi-Fi, and different Wi-Fi/TCP board firmware; it does not use the iPhone app. Direct MCP controls need no separate model API key. See the [architecture route comparison](../docs/codex-bridge-architecture.md#choose-the-session-owner-first) before choosing or reflashing. This bridge remains useful when the iPhone app owns the agent loop.

```text
iPhone app → Tailscale HTTPS → loopback bridge → codex app-server → model
                                                        ↓
iPhone app ← completed response ←────────────────────────┘
```

The model still runs through the configured Codex provider. Phone screenshots leave the phone and Mac for inference. The S3 remains responsible for AirPlay capture and USB input; the bridge does not change its firmware or the phone's screen-capture connection.

## Requirements

- Python 3.11 or later. The bridge uses the standard library; no Python packages are required.
- An installed `codex` CLI supporting `app-server`, authenticated locally with **Sign in with ChatGPT**. Check with `codex login status`; use `codex login` if needed.
- Tailscale connected on both the Mac and iPhone, with tailnet HTTPS enabled and access rules permitting the phone to reach the Mac's chosen port.
- A calibrated ESP32 Agent setup and an available image-capable Codex model.
- An iPhone build from this branch, including the client error-handling changes.

ChatGPT authentication uses subscription access; model availability and usage limits depend on the account and workspace. A model appearing in a catalog is not proof that inference is permitted. A completed text and image request must verify the chosen account/model combination. See [Codex authentication](https://learn.chatgpt.com/docs/auth) and [model access verification](https://developers.openai.com/siwc/token-sharing-open-source/codex-app-server). Do not copy OAuth tokens into this app or publish credentials.

The child process strips API-key/access-token environment overrides and requires a ChatGPT account. `OPENAI_BASE_URL` and custom `openai` provider configuration are rejected rather than used as alternate inference routes. These checks do not read or export saved login tokens.

## Start privately through Tailscale

Run from the repository root. First inspect existing routes:

```sh
tailscale serve status --json
```

Choose an unused HTTPS port. These examples use `8449`; preserve existing routes and do not run `tailscale serve reset`.

Start the bridge, replacing `YOUR-TAILSCALE-LOGIN` with the exact login attached to the iPhone's Tailscale identity:

```sh
python3 -m codex_bridge --port 8766 --tailscale-login YOUR-TAILSCALE-LOGIN
```

In another terminal, expose only this loopback service within the tailnet:

```sh
tailscale serve --bg --https=8449 http://127.0.0.1:8766
tailscale serve status --json
```

Use **Serve**, not Funnel. [Tailscale Serve](https://tailscale.com/docs/features/tailscale-serve) proxies private tailnet traffic, adds the user's identity header, and removes a client's attempted identity header before forwarding. The bridge requires the configured `Tailscale-User-Login`. Tagged devices do not supply that user header. The listener stays on `127.0.0.1`; processes already running on the Mac remain inside this trust boundary.

The bridge refuses to start without an explicit authentication mode. An alternative is `--token-file /absolute/path/to/private-token-file`, with a private file containing a high-entropy bridge token of at least 32 ASCII characters. In that mode, enter the same token into the iPhone's API-key field; it authenticates to this bridge, not OpenAI. Keep the file outside the repository with owner-only permissions, and send it only over HTTPS. If both authentication options are configured, either one authorizes a request.

## Configure the iPhone

Keep the S3 setup connected, and enable Tailscale on the iPhone. In ESP32 Agent → Settings → Model, enter:

| Field | Value |
|---|---|
| Base URL | `https://YOUR-MAC.YOUR-TAILNET.ts.net:8449/v1` |
| API key | Blank when using Tailscale identity; bridge token when using `--token-file` |
| Model | Refresh Model List and select an image-capable returned model |
| Planner / Actor | Same as Model initially |
| Token parameter | Automatic |
| Extra body JSON | `{}` |

Use the exact hostname reported by `tailscale serve status`. Tap **Test Connection**. Then run a short, supervised task such as opening Settings and identifying the current page. Model discovery, text inference, image inference, and physical phone control are separate checks; a green model list alone does not verify the agent.

With the existing hotspot setup, AirPlay uses the S3's Wi-Fi while model requests reach Tailscale over cellular. Confirm the phone has cellular service and that Tailscale stays connected in that state. A Mac on the same home Wi-Fi is not enough when the phone has joined the board's separate network.

## Supported API surface

This is a narrow adapter for [the current iOS client](../ios/IPhoneAgent/Services/LLMClient.swift), not a general OpenAI API proxy.

| Route | Behavior |
|---|---|
| `GET /v1/models` | Lists Codex catalog entries that explicitly advertise image input, in OpenAI list format. |
| `POST /v1/chat/completions` | Accepts system/user text and inline base64 JPEG, PNG, or WebP image parts; returns assistant text. |

The client supplies `model`, `messages`, `stream`, `temperature`, and `max_tokens` or `max_completion_tokens`. Sampling and output-budget fields are compatibility inputs; they do not imply equivalent controls in Codex. Unsupported payload forms are rejected rather than fetched or executed. Remote image URLs, tool calling, audio, and other OpenAI endpoints are outside this adapter.

Limits are 6 MiB per JSON request, twelve images within at most thirteen user content parts, 4 MiB per decoded image, and 32,768 combined prompt characters. The total JSON cap still applies when several images are present. Only one inference runs at a time; concurrent work receives HTTP `429`. The default Codex timeout is 75 seconds (`--timeout` overrides it); the current iPhone client has a 90-second resource timeout. Timeouts and client cancellation terminate the owned app-server process. Each request uses an ephemeral thread. Known tool features and configured MCP servers are disabled; unexpected tool or approval requests abort inference. This is a restriction for the tested CLI protocol, not a guarantee about future tool features. `/` and `/health` provide unauthenticated liveness only; the model routes require authentication.

Streaming uses SSE heartbeats while waiting, then the completed text and `[DONE]`. It intentionally does not forward partial action JSON: an interrupted or failed Codex turn must not become an executable phone action.

## Verification and operations

Run the local, mocked tests from the repository root:

```sh
python3 -m unittest discover -s tests -p 'test_bridge*.py' -v
```

Before describing a setup as working, record these checks separately:

1. Authentication rejects an untrusted request and accepts the intended Tailscale identity or token.
2. `/v1/models` returns a catalog from the installed Codex CLI.
3. A text request completes through the chosen model.
4. A synthetic, non-sensitive image request completes and describes that image correctly.
5. iPhone **Test Connection** passes over Tailscale while joined to the S3 hotspot.
6. A supervised phone task completes, and stopping it prevents further actions.

The tests use fake app-server responses and do not establish subscription access, mobile routing, or physical-device acceptance. Separately, an initial backend smoke test completed real synthetic-image inference through a local ChatGPT login and that account's default model. HTTP routing and physical iPhone acceptance still require their own checks; that backend result does not establish every account/model combination. Do not include private phone captures, hostnames, account identities, or tokens in public evidence.

The bridge does not log request bodies or screenshots. Ephemeral Codex threads reduce local conversation persistence; this is not a promise of zero provider-side retention. The iOS app separately keeps its existing run/debug records. Review those before sharing a run.

Keep the bridge terminal open for this initial setup. Stop it with Ctrl-C. Remove only this Serve listener when finished:

```sh
tailscale serve --https=8449 off
```

Troubleshooting:

- **403/401:** check the configured authentication mode, exact Tailscale login, and whether the phone is connected to Tailscale. Do not disable authentication to work around it.
- **429:** an inference is already active; wait for it or cancel the run before retrying.
- **Models load but inference fails:** check Codex login, model access, limits, and model image support. Do not silently switch to an API-key provider.
- **Works on Mac but not iPhone:** check the Serve URL, tailnet access rules, cellular data, and Tailscale while the phone is on the board's hotspot.
- **Stream stops or times out:** inspect the returned error and retry a small supervised request. Never reuse incomplete output as an action.

See [architecture and network alternatives](../docs/codex-bridge-architecture.md) for the route toward normal Wi-Fi.
