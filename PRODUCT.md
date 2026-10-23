# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Windows users running their own WeChat bots locally. The primary use is repeated day-to-day operation: check accounts, start or stop a bot, manage plugins and supported groups, edit model settings, and inspect logs. The public project must also be understandable to a new operator.

## Product Purpose

helper provides a browser interface for the existing Python plugin framework, replacing routine console commands with visible controls. Success means the operator can identify the selected robot and its current state, complete the intended action, and see whether it succeeded.

## Operating Context

The operator starts a local Python service, normally with start_web.bat, and opens a browser on the same Windows computer. The console discovers logged-in WeChat accounts; the operator checks accounts and explicitly starts the selection together. Profiles are matched or prepared automatically, retaining existing configuration and isolating new accounts. WeChat login and phone confirmation take place in the WeChat client.

## Capabilities and Constraints

- Python serves plain HTML, CSS, and JavaScript on loopback. No frontend build step or additional web dependency is required.
- Preserve isolated multi-robot processes, start/stop/restart, plugin management, group and allowlist configuration, model and voice settings, and live logs. Manual profile creation and account binding are replaced by account discovery and explicit batch selection in the web workflow.
- Account discovery and page load never start bots. Selecting an account in the top selector prepares its configuration without starting it. Each account row directly starts or stops that account without changing the configuration view; omit the separate selected-robot status panel. Selections are not persisted across page reloads; unselected accounts stay unaffected.
- Replace overview setting shortcuts with a live per-account message monitor. Show local time, conversation/group ID, sender ID/name and plain text. Media use type placeholders; never embed media or expose raw metadata and file paths. Keep bounded memory caches separate from operational logs and summary history.
- The groups page offers manual text sending to discovered conversations, with a searchable member checklist for real group mentions (up to eight). Member rows distinguish owner, administrator, regular member and unknown from the selected account's WeChat cache. Connection tokens, stale-response guards and explicit send status keep account changes isolated; HTTP retries never automatically resend a message. Group identities are also available to plugins and the message monitor without assigning application permissions.
- Expand merged forwards into their original author, source time and text, retaining the forwarder and outer conversation. Images use placeholders and update their download status in place. Forwarded names never stand in for verified wxids; embedded commands and history are not replayed as live chat.
- Configuration edits require the selected robot to stop; plugin enable/disable/reload can operate while running.
- API keys stay in local configuration and are never returned by configuration reads. An empty key input preserves the stored key.
- Preserve existing local accounts, group settings, aliases, databases, and credentials during visual updates.
- UI copy is Chinese and concise: keep task labels and necessary feedback; avoid repeated explanations. IDs and technical settings remain selectable and readable.
- Support light and dark themes with browser-local preference; first use follows the system. The dark theme uses neutral black and gray surfaces, not green-tinted surfaces.
- The supported native client is WeChat 4.1.15.13 x64 on Windows. The native bridge also forwards voice, video and sticker metadata for monitoring without downloading those media.

## Brand Commitments

Retain the helper name. The user explicitly requested a less generic, less AI-looking interface and delegated design details and hierarchy to the designer. No binding palette, typeface, or illustration was supplied. The incumbent visual treatment may be replaced.

## Evidence on Hand

Existing implementation: webui/, wxbot_framework/web_panel.py, and wxbot_framework/robot_hub.py. Functional tests and simulated browser workflows cover multi-robot operation. Use synthetic accounts in design captures; do not publish personal screenshots or credentials.

## Product Principles

- Make the current robot and operation state unambiguous.
- Keep frequent operations direct and configuration edits deliberate.
- Make independent robots feel independent; switching views must not change their execution.
- Use visible product facts instead of ornamental metrics or invented activity.
- Keep local operation and privacy understandable without making the interface verbose.
