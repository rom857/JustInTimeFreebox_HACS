# Just-In-Time Freebox Integration

A Home Assistant integration that manages Freebox port-forwarding rules based on external grant APIs. Automatically opens/closes ports according to current permissions, supporting multiple simultaneous grants.

## Features

- **Multi-Grant Support**: Manage multiple simultaneous port-forwarding grants from a single API call
- **Per-Target Binary Sensors**: Separate binary sensors for each granted target showing open/closed status
- **Automatic Reconciliation**: Keeps Freebox rules synchronized with grant state in real-time
- **Multi-Profile Support**: Manage multiple Freebox instances with different grants APIs
- **Exponential Backoff**: Intelligent error handling with backoff for Freebox communication failures
- **Time-Window Support**: Respect grant expiration times automatically
- **Secret Storage**: Secure API key storage via Home Assistant's secrets/password selectors

## Installation (HACS)

1. In HACS, add this repository as a custom repository (category: *Integration*).
2. Install **Just-In-Time Freebox** from HACS.
3. Restart Home Assistant.
4. Settings → Devices & services → Add integration → *Just-In-Time Freebox*.

## Configuration

The setup form asks for:

- **Profile name (optional)** — friendly label to distinguish entries.
- **Grants API URL** — full URL of your grants endpoint.
- **Grants API key** — sent as `X-Access-Key: <key>`.
- **Reuse existing credentials (optional)** — auto-fill Freebox host/port from existing entry.
- **Freebox host** — e.g. `mafreebox.freebox.fr` (default) or your LAN IP.
- **Poll interval (seconds)** — minimum 5, default 30.

After submitting, you will be prompted to **press the right arrow (▶) on the Freebox front panel** to authorize Home Assistant. The resulting `app_token` is stored under an entry-specific file in `<config>/just_in_time_freebox/` (managed by the `freebox-api` library).

TLS to the Freebox is handled by the library, which bundles the Freebox CA — no HTTPS toggle is needed.

Grants URL, grants key and poll interval can be edited later via *Configure* on the integration card. Changing the Freebox host requires removing and re-adding the integration.

Multiple entries are allowed for the same Freebox host, but duplicate entries using the same host + port + grants URL are blocked.

## Grants API Format

The integration supports both single-grant and multi-grant API responses:

### New Format (Multiple Grants - v0.1.0+)
```json
[
  {
    "targetId": "openvpn",
    "granted": true,
    "port": 1194,
    "protocol": "udp",
    "startedUtc": "2026-05-27T18:00:00+00:00",
    "expiresUtc": "2026-05-27T19:00:00+00:00",
    "remainingSeconds": 1800
  },
  {
    "targetId": "db-prod",
    "granted": false,
    "port": 1433,
    "protocol": "tcp",
    "startedUtc": null,
    "expiresUtc": null,
    "remainingSeconds": null
  }
]
```

### Legacy Format (Single Grant - Still Supported)
```json
{
  "granted": true,
  "port": 1194,
  "protocol": "udp",
  "startedUtc": "2026-05-27T18:00:00+00:00",
  "expiresUtc": "2026-05-27T19:00:00+00:00",
  "remainingSeconds": 1800
}
```

## How it works

Each poll cycle:
1. Fetch all grants from the external API (single or multiple)
2. For each granted and non-expired target, find the matching Freebox port-forwarding rule
3. Enable the rule if currently disabled
4. Disable rules that are no longer granted or have expired
5. Report status via per-target binary sensors and diagnostic sensors

On **every poll** the integration reconciles rules' state, so any out-of-band toggle (manual edit, restart, etc.) is corrected on the next tick.

The integration **never creates or deletes Freebox rules** — it only toggles existing ones. If no matching rule exists, the `last_action` sensor reports `rule_not_found` and that target is otherwise ignored.

## Entities

### Binary Sensors (Dynamic)
- `binary_sensor.{profile}_{targetId}_opened` — open/closed status for each target
  - **ON** if target is granted AND not expired
  - **OFF** otherwise
  - **Icon**: lan-connect (open) / lan-disconnect (closed)
  - **Attributes**: port, protocol, remaining_seconds, expires_at, targetId

### Sensors (Diagnostic)
- `sensor.{profile}_grants_active_count` — Number of currently granted targets
- `sensor.{profile}_targets_summary` — Human-readable summary (e.g., "2 granted, 1 denied")
- `sensor.{profile}_last_action` — Last reconciliation result
  - `idle`: No changes needed
  - `enabled_rule`: Rule(s) just enabled
  - `disabled_rule`: Rule(s) just disabled
  - `partial_success`: Some rules failed, others succeeded
  - `rule_not_found`: Target(s) have no matching Freebox rule
  - `freebox_error`: Freebox communication error

## Required Freebox permissions

The Freebox API does **not** allow an app to request specific permissions during pairing — a fresh `app_token` starts with the default (minimal) permission set. You must grant the **Modification des réglages de la Freebox** (`settings`) permission to this integration manually, otherwise every Freebox call will fail with HTTP 403 `insufficient_rights` and entities will report `freebox_error`.

Steps (one-time, after pairing):

1. Open **Freebox OS** as administrator (`http://mafreebox.freebox.fr/`).
2. Go to **Paramètres de la Freebox → Mode avancé → Gestion des accès → Applications**.
3. Find the row named **JIT Freebox** (the `app_name` declared by this integration).
4. Tick **Modification des réglages de la Freebox**, then save.
5. In Home Assistant, reload the integration (or wait for the next poll).

> If the admin password is later reset on the Freebox, **all app permissions are reset to defaults** and you will need to redo step 4.

## Migration from v0.0.x to v0.1.0

### Breaking Changes
- **Old entities removed**: `sensor.*_port`, `sensor.*_protocol`
- **New binary sensors**: Each target now has its own `binary_sensor.*_{targetId}_opened` entity
- **New sensor schema**: `targets_summary` replaces individual port/protocol sensors

### Upgrade Path
1. Update integration to v0.1.0
2. Old entities will be removed automatically
3. New per-target binary sensors will be created
4. Update any automations/templates to use new entity names

### Backward Compatibility
- Old single-grant API format still works (auto-detected)
- Existing multi-profile entries continue without reconfiguration
- Token files remain in same location (per-entry scoped)

## Resilience

If the Freebox is unreachable, the integration applies **exponential backoff** to its Freebox calls (base = poll interval, capped at 15 minutes), resetting on the next successful call. Grants API failures mark entities unavailable until the next successful poll.

## Example Automations

### Notify when VPN port opens
```yaml
alias: VPN Opened
trigger:
  platform: state
  entity_id: binary_sensor.freebox_openvpn_opened
  to: "on"
action:
  service: notify.notify
  data:
    message: "OpenVPN port is now open"
```

### Disable rule when grant expires
```yaml
alias: Grant Expiring Soon
trigger:
  platform: numeric_state
  entity_id: sensor.freebox_openvpn_remaining_seconds
  below: 60
  value_template: "{{ state | int(0) }}"
action:
  service: notify.notify
  data:
    message: "OpenVPN grant expires in 1 minute"
```

## Troubleshooting

- **`rule_not_found`** — create the matching redirection on the Freebox first (Freebox OS → Paramètres de la Freebox → Gestion des ports), matching the grant's `port` (as `lan_port`) and `protocol`. The integration will only flip its `enabled` flag.
- **Binary sensor shows `off` while grant looks valid** — ensure the target rule exists and the app has Freebox `settings` permission; otherwise the integration cannot switch the rule to `enabled`.
- **Pairing fails** — confirm the Freebox host is reachable and that you pressed the front-panel button.
- **`freebox_error`** — verify `http://<host>/api_version` is reachable from Home Assistant; the integration uses it to discover the API endpoint.
- **Grants API failures** — check your API key, URL, and network connectivity.
- Enable debug logs via `logger:` → `custom_components.just_in_time_freebox: debug` (and `freebox_api: debug`) to inspect requests.

