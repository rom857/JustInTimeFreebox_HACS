# Just-In-Time Freebox (HACS)

A Home Assistant custom integration that polls an external "grants" API and, when a grant is active, **enables a matching port-forwarding rule on your local Freebox**. When the grant expires (or is revoked), the rule is automatically disabled.

## How it works

Each poll, the integration calls your grants API. The expected JSON response is:

```json
{
  "granted": true,
  "port": 1194,
  "protocol": "udp",
  "startedUtc": "2026-05-09T05:16:14.7766561+00:00",
  "expiresUtc": "2026-05-09T13:16:14.7766561+00:00",
  "remainingSeconds": 26641
}
```

When `granted` is `true`, the integration looks for an existing Freebox port-forwarding rule (`/api/vN/fw/redir/`) whose `lan_port` and `ip_proto` match `port` and `protocol`. If found, the rule's `enabled` flag is set to `true`. When `expiresUtc` is reached or `granted` flips to `false`, the rule is set back to `enabled: false`.

The integration **never creates or deletes Freebox rules** — it only toggles existing ones. If no matching rule exists, a single warning is logged and the grant is ignored.

## Installation (HACS)

1. In HACS, add this repository as a custom repository (category: *Integration*).
2. Install **Just-In-Time Freebox** from HACS.
3. Restart Home Assistant.
4. Settings → Devices & services → Add integration → *Just-In-Time Freebox*.

## Configuration

The setup form asks for:

- **Grants API URL** — full URL of your grants endpoint.
- **Grants API key** — sent as `X-Access-Key: <key>`.
- **Freebox host** — e.g. `mafreebox.freebox.fr` or `192.168.1.254`.
- **Use HTTPS to reach Freebox** — see the HTTPS note below.
- **Poll interval (seconds)** — minimum 5, default 30.

After submitting, you will be prompted to **press the right arrow (▶) on the Freebox front panel within 60 seconds** to authorize Home Assistant. The resulting `app_token` is stored in the config entry.

All settings (including grants URL/key, poll interval, Freebox host and HTTPS) can be edited later via *Configure* on the integration card. Changing the host typically requires re-pairing — remove and re-add the integration in that case.

### HTTPS note

This integration uses **standard SSL certificate verification**. The default `mafreebox.freebox.fr` endpoint uses a self-signed certificate that will **not** validate, so leave HTTPS **off** when targeting it locally. Use HTTPS only if your Freebox is exposed under a name backed by a publicly trusted certificate (e.g. via Freebox remote access).

## Entities

A single device is created with:

- `binary_sensor.*_granted` — current grant state (`connectivity` device class).
- `sensor.*_status` — `granted` / `not_granted` / `unknown`.
- `sensor.*_port` — current port.
- `sensor.*_protocol` — `tcp` / `udp`.
- `sensor.*_expires_at` — timestamp.
- `sensor.*_remaining` — duration in seconds.
- `sensor.*_last_action` — diagnostic: `idle`, `enabled_rule`, `disabled_rule`, `rule_not_found`, `freebox_error`.

## Resilience

If the Freebox is unreachable, the integration applies **exponential backoff** to its Freebox calls (base = poll interval, capped at 15 minutes), resetting on the next successful call. Grants API failures simply mark entities unavailable until the next successful poll.

## Troubleshooting

- **`No Freebox port-forward rule matches port=… proto=…`** — create the matching redirection on the Freebox first (Freebox OS → Paramètres de la Freebox → Gestion des ports). The integration will only flip its `enabled` flag.
- **Pairing fails** — confirm the Freebox host is reachable and that you pressed the front-panel button within 60 s.
- **HTTPS fails** — see the HTTPS note above.
