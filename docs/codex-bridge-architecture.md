# Codex bridge architecture and network options

## Choose the session owner first

For **a native Codex chat that sees the phone and chooses each action**, the recommended route is the upstream legacy version's existing CLI/stdio MCP adapter. The external chat calls screenshot and input tools directly, so its visible conversation owns the observe/act loop. A separate app-server process behind an OpenAI-compatible endpoint instead serves the iPhone app's agent loop. Making that process use a saved thread would not, by itself, attach the loop to a live desktop conversation.

This distinction changes the network choice too: the legacy route already puts the phone and board on normal home Wi-Fi and captures AirPlay on the Mac. It avoids the S3 hotspot and cellular requirement without a new screen-capture implementation.

| Route | Who owns decisions | Capture and control | Compatibility |
|---|---|---|---|
| **Existing legacy MCP — recommended for native Codex control** | The Codex chat calling MCP tools | AirPlay to UxPlay on Mac; Python server; Wi-Fi/TCP to USB HID board | Different firmware; no iPhone app. |
| **This app-server bridge — optional headless backend** | The iPhone app's planner/actor loop | Current S3 AirPlay → BLE capture and BLE → USB input | Current app/firmware; Tailscale model endpoint. |
| **Current app with shared-LAN STA mode — future network increment** | The iPhone app's planner/actor loop | S3 AirPlay on shared LAN, with existing BLE capture/control | Firmware support exists; app setup/recovery remains. |

### Existing legacy CLI and MCP support

Source review verified this support at upstream legacy commit `fc8458c39d769b4a48d500fc6d53574bd7b77e9e`, newer than the `v0.1.0` tag. Use the [pinned remote-agent instructions](https://github.com/onehumanbeing/esp32-iphone-agent/blob/fc8458c39d769b4a48d500fc6d53574bd7b77e9e/docs/remote-agents.md) and [MCP implementation](https://github.com/onehumanbeing/esp32-iphone-agent/blob/fc8458c39d769b4a48d500fc6d53574bd7b77e9e/server/hidagent/mcp.py), rather than assuming the older tag contains these tools.

```text
iPhone ─AirPlay on shared Wi-Fi→ UxPlay on Mac → Python frames
                                                        ↓
native Codex chat ← iphone_screenshot (image) ← stdio MCP adapter
native Codex chat ─tap/swipe/type/etc→ adapter → Python HTTP server
                                                  ⇅ TCP :7300
                                      S3 on shared Wi-Fi → USB HID → iPhone
```

`iphone_status`, `iphone_screenshot`, `iphone_tap`, `iphone_swipe`, `iphone_scroll`, `iphone_type`, `iphone_key`, `iphone_home`, `iphone_back`, and `iphone_calibrate` expose direct controls. They need no separately configured vision-model credentials because the external chat supplies the decisions. `delegate_task` is different: it starts the legacy server's configured vision agent and requires that agent's model configuration. It would move decisions back into that server loop.

The board must run the legacy Wi-Fi/TCP firmware. The currently calibrated BLE/iPhone-app firmware is incompatible with these server tools, so switching requires a deliberate reflash and recalibration. UxPlay and the FastAPI/MCP Python environment also need installation and runtime verification; source inspection is not proof of a working legacy deployment.

The pinned upstream instructions explicitly state that the HTTP server has **no authentication** and there are **no controller ownership leases**. A private authenticated access design and one-controller discipline are required before packaging remote use for publication. Do not expose the unauthenticated HTTP or board TCP listener publicly. Start with status and a screenshot, observe after every action, and verify the visible effect: an acknowledgement is not proof the UI changed, and the latest frame may be stale. Do not mix direct MCP actions with `delegate_task`.

## Optional headless app-server bridge

The iPhone app already creates a self-contained vision request for each decision: system text, user text, and JPEG frames. It understands OpenAI-style model lists and either plain JSON or SSE chat responses. [LLMClient.swift](../ios/IPhoneAgent/Services/LLMClient.swift) is the compatibility contract.

The bridge translates that request format into the installed Codex CLI's app-server protocol. The iOS client is also hardened to throw on SSE error objects and a repeated empty response, so model failure cannot silently become an empty action:

```text
Screen Mirroring ─Wi-Fi→ S3 ─BLE H.264→ iPhone app
iPhone app ─HTTPS/Tailscale→ Mac loopback HTTP adapter
                            └─stdio→ Codex app-server ─provider→ model
iPhone app ← completed assistant text ←─────────────────────┘
iPhone app ─BLE commands→ S3 ─USB HID→ iPhone
```

Codex app-server is a programmatic interface distinct from a desktop chat. The lifecycle is `initialize`, `initialized`, model discovery, an ephemeral `thread/start`, and `turn/start`. Only a successfully completed turn supplies assistant text. See the [official app-server protocol](https://learn.chatgpt.com/docs/app-server). The bridge owns and cleans up its child processes; it does not inject messages into a native conversation or make a desktop chat the phone controller.

The HTTP adapter performs bounded parsing and authentication before inference. It allows one active inference, returns `429` when occupied, sends SSE heartbeats while waiting, and buffers assistant output until completion. Requests are limited to 6 MiB total, twelve images in at most thirteen user content parts, 4 MiB per decoded image, and 32,768 combined prompt characters. Client disconnects and timeouts end the owned child process. Invalid input, model failures, or interrupted turns return errors rather than partial actions. Known tool features and configured MCP servers are disabled; unexpected tool or approval requests abort inference. Tool isolation is bounded by the tested CLI's features and protocol, not a universal guarantee for future Codex versions. The app-server transport is never exposed to the phone.

The CLI uses its existing local ChatGPT login. The backend strips API credential/access-token environment overrides and rejects `OPENAI_BASE_URL` or custom `openai` provider overrides. No OAuth credential extraction, token copying, or API-key fallback is part of the design. Catalog entries must explicitly advertise image input; account limits and model permissions still apply. Phone frames are sent to the model provider; private Tailscale transport does not make inference local. See [authentication and data-policy boundaries](https://learn.chatgpt.com/docs/auth).

## Why the phone currently occupies Wi-Fi

The S3 is an AirPlay receiver. By default it creates a SoftAP, receives the phone's mirrored H.264 stream, and forwards frames over BLE to the same phone's app. The app sends HID commands over BLE, and the board presents USB mouse/keyboard input to the phone. [Upstream flow](../README.md#how-it-works), [BLE protocol](ble-protocol.md), and [video forwarding](../hardware/hid_bridge/video_link.cpp) describe the separate paths.

In [netcfg.cpp](../hardware/hid_bridge/netcfg.cpp), `noDefaultRoute()` removes router and DNS advertisements from the hotspot's DHCP replies. That deliberately leaves cellular as the phone's internet route. The first bridge deployment therefore preserves the calibrated capture/input path and sends inference requests through cellular → Tailscale → Mac.

This is a starting topology, not a hardware requirement that the phone always use a board hotspot.

## Alternatives

| Option | What changes | Evidence and remaining work |
|---|---|---|
| **Hotspot + cellular + Tailscale** | Model endpoint changes and the updated iOS client handles bridge errors. | Existing capture/input design; needs HTTP and phone-side Tailscale verification. Requires usable cellular data. |
| **Shared LAN / station mode** | Board joins the same 2.4 GHz LAN as the phone. AirPlay uses that LAN; the phone keeps ordinary Wi-Fi internet. | Firmware and BLE configuration support already exist. User-facing onboarding, recovery, discovery, and physical tests remain. Recommended next network increment. |
| **Direct screen broadcast to Mac** | A new capture extension sends frames to the Mac; the S3 retains USB HID input. | A separate capture/frame pipeline and app integration. Requires current iOS SDK feasibility and lifecycle tests. It is not an endpoint setting. |

### Shared LAN is the least invasive next step for the iPhone app

Station mode already exists in [netcfg.cpp](../hardware/hid_bridge/netcfg.cpp): `WIFI_STA`, persistent SSID/password, connection retries, and a `none` status while disconnected. The encrypted BLE `net` characteristic accepts `{"mode":"sta","ssid":"…","pass":"…"}` and can restore `{"mode":"ap"}`. [BoardLink.swift](../ios/IPhoneAgent/Board/BoardLink.swift) has `writeNet`, and [the BLE protocol](ble-protocol.md#net-json) documents it.

Choose this increment when keeping the iPhone app's own agent loop. For native Codex chat ownership, the existing legacy MCP route above is the more direct fit.

These capabilities do not yet constitute a complete LAN setup. [HotspotJoin.swift](../ios/IPhoneAgent/App/HotspotJoin.swift) and onboarding assume joining the board's AP. A robust LAN mode should:

1. Provision credentials over the bonded, encrypted BLE link, then verify the board's reported station state and IP.
2. Keep the phone on its existing Wi-Fi and discover the AirPlay receiver on the same LAN. Test local-network permission, multicast discovery, and routers with client isolation.
3. Preserve a visible **Return to board hotspot** recovery action over BLE. The firmware retries STA indefinitely; automatic return to AP is not implemented.
4. Show distinct BLE, USB, Wi-Fi, mirroring, and model states so a missing frame is not mistaken for a model outage.
5. Prove reconnect after board reboot, wrong credentials, LAN loss, and an app restart before making LAN the default.

Inference can continue through Tailscale in LAN mode. This avoids coupling the model endpoint to a particular home network and keeps its authentication unchanged. It does not require AP+STA routing, NAT, or giving the board internet credentials for a cloud model.

### A screen-broadcast redesign is a separate project

A ReplayKit-style broadcast extension could move screen capture away from AirPlay and the board entirely, with frames delivered directly to the Mac and actions returned to the phone/board control path. Apple's [screen broadcast session slides](https://devstreaming-cdn.apple.com/videos/wwdc/2018/601nz4m863hyf0/601/601_live_screen_broadcast_with_replaykit.pdf) describe broadcast extensions and sample buffers. This historical source establishes the concept, not a verified implementation for the current SDK.

It needs explicit screen-broadcast consent, an extension target, bounded frame transport, frame freshness/backpressure, and a reliable action/control channel while the main app is backgrounded. Protected content, lock/background behavior, thermal/battery cost, extension termination, and stopping must be measured on-device. The present agent loop lives in the main app; a new frame source alone does not solve its background execution. Keep this research separate from the first working bridge.

## Security and publication boundary

Use Tailscale Serve HTTPS to proxy an unused port to a loopback-only HTTP listener. Authenticate the injected Tailscale login or an explicit bridge token. [Serve identity headers](https://tailscale.com/docs/features/tailscale-serve#identity-headers) are trustworthy only inside the local proxy boundary; other local processes are trusted. Do not expose app-server or this bridge through Funnel.

Bound request bytes, text, image count, execution time, and concurrency. Keep prompt bodies and screenshots out of bridge logs. Ephemeral threads avoid saved local chat history, but provider handling and the iOS app's run storage remain separate. Treat screenshots as data, not privileged Mac instructions. Keep the known-tool configuration and unexpected-request rejection covered when upgrading Codex.

Public setup examples must contain only generic hostnames and identities. Keep runtime credentials, actual tailnet identifiers, user captures, signed builds, and private diagnostic artifacts outside git. Preserve upstream attribution and license. Local unit tests, model catalog access, completed inference, and a successful physical task are different evidence levels; report which passed rather than labeling the whole system verified from a build.

An initial backend smoke test completed synthetic-image inference through a real local ChatGPT login and the account's default model. That verifies one backend/account/model path. HTTP transport and physical iPhone acceptance remain separate pending checks.
