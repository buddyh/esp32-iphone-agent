# Native Codex control with BLE input and Mac AirPlay capture

This hybrid keeps the **current BLE firmware** on the S3 and moves screen capture to the Mac. The phone stays on ordinary Wi-Fi. A native Codex chat receives screenshots and calls the existing MCP gesture tools, so the chat owns each decision. No separate model API key, iPhone agent app, or app-server inference endpoint is needed for direct control.

```text
iPhone ─AirPlay on normal Wi-Fi→ UxPlay on Mac → Python JPEG frames
                                                        ↓
native Codex chat ← iphone_screenshot (image) ← stdio MCP adapter
native Codex chat ─tap/swipe/type/etc→ adapter → loopback Python HTTP
                                                  ↓
                                            Mac BLE transport
                                                  ↓
                                            S3 → USB HID → iPhone
```

The Mac and iPhone must share an AirPlay-reachable network. The S3's Wi-Fi connection is not part of this route; its existing hotspot can be ignored. This mode adds a BLE transport to the legacy Python controller rather than reflashing the board with legacy Wi-Fi/TCP firmware.

## What is established

| Evidence | Boundary |
|---|---|
| Existing MCP screenshot and gesture tools, Python gesture/calibration code, and the S3 BLE command protocol | Verified in source. See [MCP tools](../server/hidagent/mcp.py), [controller](../server/hidagent/phone.py), and [BLE transport](../server/hidagent/ble_device.py). |
| Mac AirPlay capture through UxPlay and GStreamer | Supported by the upstream tools; installation and actual phone capture need their own checks. |
| S3 BLE → USB HID control while the phone mirrors to the Mac | The integration to prove on hardware. Existing iPhone-app calibration is not proof of this controller's calibration. |
| Native Codex screenshot → action → fresh screenshot loop | Requires the physical verification sequence below. Unit tests or command acknowledgements alone do not establish it. |

Do not describe the hybrid as working on a device until capture, control, stop, and reconnect have passed together. A successful image request through the separate app-server experiment does not validate this MCP route.

## Install prerequisites

Use Python 3.10 or later and an existing `uv` installation. From this checkout's root, create a project-local environment and install the optional BLE and MCP dependencies:

```sh
uv venv .venv
uv pip install --python .venv/bin/python -e './server[ble,mcp]'
```

For macOS Apple Silicon, use UxPlay's stable `v1.73.7` release and Homebrew dependencies. Run the source checkout/build commands from a separate private tools directory outside this repository:

```sh
brew install cmake libplist openssl@3 pkg-config gstreamer
git clone --depth 1 --branch v1.73.7 https://github.com/FDH2/UxPlay uxplay-source
cmake -S uxplay-source -B uxplay-source/build
cmake --build uxplay-source/build --parallel
```

Use the built `uxplay` executable by absolute path initially rather than installing it globally. The upstream [macOS instructions](https://github.com/FDH2/UxPlay#building-uxplay-on-macos-intel-x86_64-and-apple-silicon-macs) describe the Homebrew route; the [stable release](https://github.com/FDH2/UxPlay/releases/tag/v1.73.7) is separate from experimental master. Do not assume a `brew install uxplay` formula exists.

Homebrew's [GStreamer formula](https://formulae.brew.sh/formula/gstreamer) bundles its plugins. Verify the capture pipeline's required elements:

```sh
gst-inspect-1.0 h264parse
gst-inspect-1.0 decodebin
gst-inspect-1.0 videoconvert
gst-inspect-1.0 videorate
gst-inspect-1.0 jpegenc
gst-inspect-1.0 fdsink
```

[`jpegenc`](https://gstreamer.freedesktop.org/documentation/jpeg/jpegenc.html) encodes frames; [`fdsink`](https://gstreamer.freedesktop.org/documentation/coreelements/fdsink.html?gi-language=c) sends them to the Python process through a file descriptor. An installed `ffmpeg` alone does not satisfy this GStreamer pipeline.

## Prepare the phone and Mac

1. Keep the BLE-firmware S3 plugged directly into the phone. Leave AssistiveTouch enabled and keep the phone unlocked.
2. Stop the iPhone agent's run and force-close its app. Use one controller at a time.
3. Stop mirroring to the S3. Join ordinary Wi-Fi on the phone; keep the Mac on the same reachable LAN.
4. Enable Bluetooth on the Mac and permit the launching application to use it when macOS asks. [Bleak's macOS notes](https://bleak.readthedocs.io/en/latest/backends/macos.html) explain permissions and device identifiers.
5. Start the Python controller, then select its receiver from Control Center → Screen Mirroring. The receiver name defaults to **iPhone Agent**.

The BLE adapter can discover the board. If several compatible boards are nearby, configure the intended device explicitly with `HIDAGENT_BLE_ADDRESS`. On macOS this is the identifier returned by CoreBluetooth discovery, typically a UUID, not the board's printed Bluetooth MAC address. Do not choose a device by guessing its identifier.

## Start the controller locally

From the checkout root, set `UXPLAY_BIN` to the absolute path of the built receiver:

```sh
HIDAGENT_TRANSPORT=ble \
HOST=127.0.0.1 \
PORT=8890 \
UXPLAY_PORT=35000 \
UXPLAY_BIN="/absolute/path/to/uxplay-source/build/uxplay" \
.venv/bin/python -m hidagent
```

Configuration:

| Variable | Purpose |
|---|---|
| `HIDAGENT_TRANSPORT=ble` | Use BLE instead of the default legacy TCP transport. No board TCP listener is started in BLE mode. |
| `HIDAGENT_BLE_ADDRESS` | Optional exact discovered board identifier. Omit for discovery. |
| `HOST=127.0.0.1` | Keep the HTTP controller on loopback; this is the default. |
| `PORT=8890` | Local HTTP port. Use the same port in the CLI and MCP configuration. |
| `UXPLAY_BIN` | Absolute path to a private receiver build; otherwise `uxplay` is located on `PATH`. |
| `UXPLAY` | Older name for the receiver; a bare command is looked up on `PATH`. `UXPLAY_BIN` wins when both are set. |
| `MIRROR_STREAM` | Follow a standalone receiver (`python -m hidagent.receiver`, e.g. `127.0.0.1:8891`) instead of running UxPlay here. The receiver and GStreamer checks then run in that receiver, and screen liveness falls back to frame age because its connections are not counted. |
| `UXPLAY_PORT` | Optional AirPlay base port, such as `35000`. Startup checks the base and next two TCP/UDP ports for conflicts. |
| `UXPLAY_ARGS` | Optional additional receiver arguments, split without a shell. Set the port with `UXPLAY_PORT`, not `-p` here. |
| `GST_INSPECT_BIN` | Optional path to `gst-inspect-1.0` if it is outside `PATH`. |
| `HIDAGENT_ALLOWED_HOSTS` | HTTP Host allowlist, default `localhost,127.0.0.1,::1`. Keep the loopback defaults for local MCP. |
| `AIRPLAY_NAME` | Optional receiver name, default `iPhone Agent`. |
| `HIDAGENT_DATA` | Calibration and trace directory, default `~/.hidagent`. Keep captures outside git. |

Startup checks the receiver binary and required GStreamer elements. Inspect any reported prerequisite failure before attempting phone control. `GET /api/status` reports transport and capture readiness; inspect receiver state and frame age as well as device connection. On macOS, live capture also requires an established TCP connection owned by the UxPlay process; a failed connection inspection blocks actions. A still screen is not automatically a dead stream.

In a second terminal, check the local controller:

```sh
.venv/bin/iphone-agent --url http://127.0.0.1:8890 status
```

Use the web UI at `http://127.0.0.1:8890` for initial visual checks and calibration. Do not start its autonomous task runner when using direct MCP control.

## Connect native Codex over stdio

Use Codex's MCP settings or a private project-scoped `.codex/config.toml` entry. Replace the example checkout path below with its real absolute path; do not publish that machine-specific configuration.

```toml
[mcp_servers.iphone]
command = "/absolute/path/to/esp32-iphone-agent-legacy/.venv/bin/iphone-agent"
args = ["--url", "http://127.0.0.1:8890", "mcp"]
cwd = "/absolute/path/to/esp32-iphone-agent-legacy"
disabled_tools = ["delegate_task"]
```

The [official Codex MCP documentation](https://learn.chatgpt.com/docs/extend/mcp) covers stdio configuration. Register the server, then read back its tools in the intended native chat and call `iphone_status` followed by `iphone_screenshot` there. A running server, successful CLI request, or saved app-server thread does not automatically attach tools to the current conversation.

Keep `delegate_task` disabled initially. That tool launches the legacy server's separately configured model loop and requires its model credentials. Direct `iphone_screenshot`, tap, swipe, scroll, type, key, Home, Back, and calibration tools instead let the current Codex conversation make decisions. `stop_task` remains available as the existing emergency stop.

A useful first prompt after verification is:

> Use the iPhone MCP tools to open Settings and identify the page. Read status and inspect the current screenshot first. Observe after each action, do not call delegate_task, and stop if capture is missing, stale, or the visible result differs from the intended action.

## Prove the full path

Perform these steps under supervision before a broader task:

1. Read `iphone_status`; confirm BLE connection, receiver readiness, and a live phone frame.
2. Call `iphone_screenshot`. Change the phone screen manually, capture again, and verify the image reflects that change.
3. Calibrate this Python controller. If calibration returns `auto: false`, locate the pointer and provide both normalized coordinates. Do not reuse coordinates from the iPhone app.
4. Authorize one harmless gesture on a visible target. Call the action, capture again, and verify its effect. Coordinates are image fractions from 0 to 1.
5. Exercise `stop_task`, then disconnect the board and confirm readiness falls. Reconnect and verify that no old action is replayed and calibration remains valid or is requested again.
6. Stop and restart mirroring. Confirm the next screenshot is from the new live session before resuming.

An `ok` acknowledgement establishes command delivery, not visible success. AirPlay can still show a frame from before the action. Home and Back are gestures and need visual confirmation. Keyboard input is printable ASCII; type only into a visibly focused field.

## Privacy and publication

The HTTP server has no authentication and no controller ownership leases. Keep it bound to loopback. Do not expose it with a public listener, Tailscale Funnel, or an unauthenticated reverse proxy. Native local stdio MCP needs no inbound Tailscale endpoint. A later remote-controller deployment needs a separate authenticated design.

Keep one active controller: close the phone agent, avoid simultaneous web/MCP input, and keep delegated tasks disabled. Phone screenshots enter the Codex conversation and follow its model/account data policies. Local calibration, screenshots, and debug traces remain separate records; review them before sharing anything publicly.

Publish source and generic instructions only. Exclude local MCP configuration, device identifiers, hostnames, credentials, captures, and runtime logs. Preserve upstream attribution and record the exact software versions and completed hardware checks with any demonstration.
