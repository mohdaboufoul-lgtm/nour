# Nour phone body (phase 0)

The brain runs on the server; this phone is only the Operator's body (SPEC §4, §5). In phase 0
the body has one job: forward every notification the phone receives to the event bus, and prove
it is alive. Reference: `docs/adapters/phone.md` (verified 2026-10-02), `docs/DESIGN.md` §3.4, §3.20.

## What it does

- `NourNotificationListener` (a `NotificationListenerService`) turns each posted notification into
  one `PhoneNotification` JSON object and appends it to an on-device SQLite outbox (`OutboxQueue`).
- `ForwarderService` (foreground, type `specialUse`) drains the outbox to
  `POST <ingress>/webhooks/phone` with a bearer device token, retries with exponential backoff and
  jitter (1 s to 5 min), and posts a heartbeat every 60 s.
- `BootReceiver` restarts the service after reboot and after an upgrade; the platform rebinds the
  listener on its own.
- `ConfigActivity` pairs the phone (paste the `nour://pair?...` code or type ingress + token), opens
  the two one-time grants, and shows status (listener bound, queue depth, last heartbeat).

## What it deliberately does not do

- No WhatsApp on the phone, no app automation, no screen reading: no `AccessibilityService` before
  the phase 3 gate (phone.md §1.1, §2.4). No ADB dependence in phases 0 to 2.
- No credentials: no owner accounts, no bank or password apps, no SMS reading. The only secret is one
  server-revocable pairing token in `EncryptedSharedPreferences`. No dangerous (runtime) permissions.
- No redaction, filtering or allowlisting on the phone beyond its own, the system UI's and group
  summary notifications; the brain decides. Everything forwarded is observed data, never a command.
- No logs, backups or history: the outbox is the only state, bounded at 5 000 rows, purged on ack,
  unpair or revocation; `allowBackup=false`, cloud backup and device transfer excluded.
- Android 15+: a sideloaded listener is never trusted, so OTP notifications arrive already replaced
  by "Sensitive notification content hidden". The app forwards exactly that. Tool-account 2FA is
  TOTP held server-side; an SMS OTP, if ever unavoidable, is a separate `RECEIVE_SMS` decision.

## Contract with the ingress

`POST <ingress>/webhooks/phone`, `Authorization: Bearer <device_token>`, `Content-Type: application/json`,
one `PhoneNotification` per request, field names exactly as DESIGN §3.4:

```json
{
  "id": "0|com.whatsapp|1|null|10123@1759420800123",
  "app": "com.whatsapp",
  "title": "Ahmed (Buzz Avenue)",
  "text": "Ahmed: Is the 2kg box still AED 45?",
  "at": "2026-10-02T15:20:00.123Z",
  "device_id": "3f3e0a9c-6c7e-4a9b-9b1a-0b1c2d3e4f50"
}
```

- `id` = `StatusBarNotification.key` + `@` + `postTime` (ms); stable per posting, so the server
  deduplicates on `(device_id, id)` and may answer 409 for a repeat (treated as success).
- `text` is the fullest text available: MessagingStyle messages as `sender: text` lines, else
  `bigText`, else InboxStyle lines, else `text`. `at` is `postTime` as ISO-8601 UTC.
- Responses: 2xx/409 ack and delete the row; 401/403 mean revoked (token forgotten, queue purged,
  service stops); 408/429/5xx and I/O errors retry with backoff; any other 4xx drops the row so one
  bad body cannot block the queue.

Heartbeat: `POST <ingress>/webhooks/phone/heartbeat`, same headers. This route is not in DESIGN
§3.20 yet; it is this app's proposal for `Ingress`, and a 404 is harmless (notifications still flow).

```json
{"device_id": "...", "sent_at": "2026-10-02T15:20:00Z", "battery_pct": 80, "charging": true,
 "thermal_status": 0, "listener_connected": true, "sim_state": "ready", "queue_depth": 0,
 "app_version": "0.1.0", "uptime_s": 86400}
```

`charging` means on the charger (a battery held at the 80 % limit still counts). `sim_state` is
`ready`, `locked` (PIN/PUK/network lock) or `absent`. Watchdog rule (phone.md §1.4, DESIGN §3.17):
offline after 3 missed beats, so set the grace to at least 180 s for this 60 s interval; also alert on
`thermal_status >= 3`, `battery_pct < 20` while `charging`, `listener_connected=false`, `sim_state=locked`.

## Provisioning

1. Factory reset. Before any account exists, make the phone a managed device: AMAPI enrolment QR at
   the welcome screen (`afw#setup`), or later an own DPC in this app with
   `adb shell dpm set-device-owner ae.nour.body/.DeviceAdminReceiver` (not in phase 0).
2. Build and sign with a dedicated key (never the owner's), then `adb install -r app-release.apk`.
   The project ships without the Gradle wrapper jar; run `gradle wrapper --gradle-version 8.9` once
   or open the folder in Android Studio.
3. Notification access: Config screen, "Grant notification access", or
   `adb shell cmd notification allow_listener ae.nour.body/.NourNotificationListener`.
4. Battery: Config screen, "Request battery exemption" (phone stays on the charger, so Doze never
   engages, but the exemption also lets the listener start the service from the background).
5. Pairing: the control panel creates a device row and shows `nour://pair?ingress=<https base>&token=<token>`
   as a QR. Phase 0 pastes it into the Config screen; "Save and start". The panel shows the first
   heartbeat within a minute. Revoking the row ends the channel within one heartbeat.
6. Never "Force stop" the app (no boot broadcast afterwards). After a reboot someone on site must
   unlock the screen and enter the SIM PIN once; the heartbeat gap tells the watchdog.

## Device hardening checklist (phone.md §3, condensed)

- Pixel 6a or newer, stock Android, charging limited to 80 %, low-wattage charger, out of the sun.
- Device owner before any account; separate Google account for the phone only, never the owner's.
- No personal credentials, bank apps, password manager or UAE Pass on the phone, ever.
- Screen lock complexity HIGH, short time-to-lock; SIM PIN on; PIN and PUK in the vault; carrier
  port-out and SIM-swap lock requested.
- Restrictions: no unknown sources, no debugging outside maintenance windows, no factory reset, no
  VPN changes, no USB file transfer, no added users, no safe boot, no account changes.
- Network: dedicated SSID/VLAN, WireGuard always-on with lockdown to the UAE server, outbound only;
  the server certificate pinned in `res/xml/network_security_config.xml` once issued.
- Updates windowed into the nightly maintenance slot; Play Protect and app auto-update on.
- Physical: locked room, on a stand, on the charger. Power off or pull the SIM is the physical kill
  switch; the server-side kill switch never depends on the phone answering.
- Remote wipe = four server-side levers, each tier K with passphrase: revoke the device row, lock or
  lost mode, wipe (AMAPI `devices.delete` or `wipeDevice()`), carrier SIM suspension.

## Phase 3 path

Keep the planner on the server and put the executor on the phone: an `AccessibilityService` in this
app (`dump_tree`, `tap_node`, `set_text`, `swipe`, `global_action`, `screenshot`, `capture_photo`,
`locate`), each command a typed tier N tool call over the same channel, pinned by the device owner
with `setPermittedAccessibilityServices`. The static bearer token gives way to a non-exportable
Keystore EC key (mTLS or a per-connection JWT) and the HTTP posts to one persistent WebSocket with
server acks. Raw ADB stays a maintenance tool: wireless debugging only on the dedicated VLAN over
WireGuard, during a window, every session audited; if it is ever needed unattended, a Raspberry Pi on
the phone's USB cable as the ADB host and WireGuard peer, never root or a custom ROM.
