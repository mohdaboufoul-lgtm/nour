# Phone adapter: the Operator's body

Backs SPEC §4 (brain on the server, phone is only the Operator's body), §5 (number, apps,
camera; no personal credentials on the phone), §7 (notifications as an event stream in phase
0; app control, camera, OCR and location in phase 3), §13 (phone theft: no credentials,
remote wipe, server-revocable tokens), §14 (break-glass, kill switch) and §16 (week one: buy
the phone and SIM, enable ADB over the network, phone stays on charger). Facts cite official
Android, AOSP, Google and Meta pages; anything else is marked **[unverified]**. Last verified
2026-10-02 (Android 15/16 behaviour; re-check the behaviour-changes page at each release).

## 1. Phase 0: the notification forwarder

A small private (sideloaded) companion app with one job: forward notifications to the brain
and send a heartbeat. It is never published on Google Play, so Play policies do not apply,
but platform restrictions do.

### 1.1 NotificationListenerService vs AccessibilityService

| | `NotificationListenerService` (NLS) | `AccessibilityService` (A11y) |
|---|---|---|
| Purpose | Receives every posted/removed notification with its full `Notification` object | Reads and drives the UI tree, dispatches gestures, takes screenshots |
| Manifest | `android:permission="android.permission.BIND_NOTIFICATION_LISTENER_SERVICE"`, intent filter `android.service.notification.NotificationListenerService` | `android:permission="android.permission.BIND_ACCESSIBILITY_SERVICE"`, intent filter `android.accessibilityservice.AccessibilityService`, meta-data XML (`canRetrieveWindowContent`, `canPerformGestures`, `canTakeScreenshot`) |
| Enablement | Settings, Notification access (`Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS`), or `adb shell cmd notification allow_listener pkg/.Svc` (used by AOSP CTS and Appium) | "exclusively by the user explicitly turning the service on in device settings" (`Settings.ACTION_ACCESSIBILITY_SETTINGS`); a device owner pins the allowed set with `setPermittedAccessibilityServices` |
| Lifecycle | Bound by the system; wait for `onListenerConnected()`; `requestRebind()` is the only safe call before it; `META_DATA_DEFAULT_AUTOBIND` defaults to true, so the system rebinds it after reboot without a boot receiver | Started when enabled; restarted after reboot while it stays enabled **[unverified: not explicit in the reference]** |
| Android 13+ restricted settings | Not gated at the Android 13 launch (Esper) **[unverified for 15/16]** | Blocked for apps installed by a non-session installer (browser, file manager, mail); session-based installers (app stores, Files by Google) exempt; `adb install` **[unverified]**. Workaround: App info, three-dot menu, "Allow restricted settings"; or `adb shell cmd appops set <pkg> ACCESS_RESTRICTED_SETTINGS allow` (F-Droid issue, **[unverified]**) |
| Play policy (if ever published) | None specific; `REQUEST_IGNORE_BATTERY_OPTIMIZATIONS` is policy-restricted | Permission Declaration Form, `isAccessibilityTool`, prominent disclosure |
| Android 15+ | Untrusted listeners get "sensitive notification content hidden" where an OTP is detected; trusted = holders of `RECEIVE_SENSITIVE_NOTIFICATIONS` (role/signature: OEM-signed, default launcher, preinstalled companion apps). A sideloaded app is never trusted | Same redaction in the screen tree? **[unverified]** |

Decision: phase 0 uses NLS only; the accessibility service is a phase 3 component (2.4) and
stays off before that gate because it is the larger attack surface. Sources:
<https://developer.android.com/reference/android/service/notification/NotificationListenerService>, <https://developer.android.com/guide/topics/ui/accessibility/service>,
<https://support.google.com/googleplay/android-developer/answer/10964491> (Play a11y policy), <https://developer.android.com/about/versions/15/behavior-changes-all> and <https://androidauthority.com/android-15-two-factor-authentication-codes-3492585> (OTP trust rule),
<https://support.google.com/android/answer/12623953> and <https://www.esper.io/blog/android-13-sideloading-restriction-harder-malware-abuse-accessibility-apis> (restricted settings), <https://discuss.appium.io/t/native-android-notification-access-control/33471> (`allow_listener`).

### 1.2 Fields available per notification

`StatusBarNotification` (<https://developer.android.com/reference/android/service/notification/StatusBarNotification>):
`getPackageName()`, `getKey()` ("a unique instance key for this notification record"),
`getId()`, `getTag()`, `getPostTime()` (ms epoch, "may be different than Notification.when"),
`getGroupKey()`, `getUser()`, `getUid()`, `isOngoing()`, `isClearable()`, `getNotification()`.
`Notification` (<https://developer.android.com/reference/android/app/Notification>): `when`,
`category` (`CATEGORY_MESSAGE`, `CATEGORY_CALL`, ...), `flags`, and `extras` keys
`android.title`, `android.text`, `android.bigText`, `android.subText`, `android.textLines`,
`android.messages` (MessagingStyle: sender, text, time per message; WhatsApp uses it),
`android.conversationTitle` (groups), `android.infoText`, `android.summaryText`. Forward
`onNotificationRemoved` with its `REASON_*` too. Event sent to the brain (source `phone`,
desk `operator`, coat from the app allowlist): `{event_id, device_id, key, package,
post_time, when, category, title, text, big_text, text_lines[], messages[{sender, text,
time}], conversation_title, ongoing, group_key, removed_reason|null}`. Notifications are a
signal, not a transport: grouped summaries, muted chats, truncation and OTP redaction all lose
content; full messages come from the channel's API. All text is observed data, `owner_verified=false`.

### 1.3 Staying alive: battery, Doze, boot

- Doze engages only when the device is "unplugged, stationary, and has the screen off", and
  connecting a charger exits it, so a phone on the charger is not dozed. Still request the
  exemption once at setup (`Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS`, check
  `PowerManager.isIgnoringBatteryOptimizations`); Play's restriction does not apply here.
  <https://developer.android.com/training/monitoring-device-state/doze-standby>
- Run the WebSocket and heartbeat in a foreground service (Android 14+ requires a type),
  started from a `RECEIVE_BOOT_COMPLETED` receiver on `ACTION_BOOT_COMPLETED`, an explicit
  exemption from background-start limits. On Android 15 `dataSync`, `mediaPlayback`,
  `mediaProjection` and `phoneCall` cannot start from `BOOT_COMPLETED`, so declare
  `connectedDevice` (`FOREGROUND_SERVICE_CONNECTED_DEVICE`) or `specialUse` with
  `android.app.PROPERTY_SPECIAL_USE_FGS_SUBTYPE` (Play review applies only to Play apps).
  <https://developer.android.com/develop/background-work/services/fgs/restrictions-bg-start>,
  <https://developer.android.com/develop/background-work/services/fg-service-types>,
  <https://developer.android.com/reference/android/content/Intent#ACTION_BOOT_COMPLETED>
- A force-stopped app receives no boot broadcast; never "Force stop" the companion; the
  watchdog (1.4) is the safety net. Device owner only, phase 3: `setGlobalSetting(admin,
  Settings.Global.STAY_ON_WHILE_PLUGGED_IN, ..)` (not combinable with `setMaximumTimeToLock`).

### 1.4 Secure outbound channel to the brain

- Outbound only; the phone never exposes a port. All traffic rides the WireGuard tunnel (2.2)
  and is TLS inside it with the server certificate pinned.
- Identity: an EC key pair in the Android Keystore (hardware-backed, non-exportable;
  <https://developer.android.com/privacy-and-security/keystore>) is the device identity, used
  as the mTLS client certificate on the WebSocket or to sign a short-lived JWT per connection.
  The server keeps `device_id -> public key, enrolled_at, revoked_at`; enrolment is a one-time
  pairing code from the control panel; revocation is a row update that drops the connection
  within one heartbeat. No static API token on the phone.
- Transport: one persistent `wss://` connection from the foreground service; reconnect with
  exponential backoff plus jitter; an on-device SQLite queue gives at-least-once delivery
  with server acks; the server deduplicates on `(device_id, key, post_time)`.
- Heartbeat every 30 s: `{device_id, sent_at, battery_pct, charging, thermal_status
  (PowerManager.getCurrentThermalStatus), listener_connected, a11y_enabled, sim_state,
  network, app_version, queue_depth, uptime_s}`. The watchdog marks the phone `offline`
  after 3 missed beats (90 s), raises an incident on the owner's second channel and pauses
  phone-dependent tasks; it also alerts on `THERMAL_STATUS_SEVERE`, battery under 20 % while
  "charging" (dead charger), `listener_connected=false` or `sim_state=locked`.
  <https://developer.android.com/reference/android/os/PowerManager>
- Hygiene: a package allowlist in config decides what is forwarded; the companion never
  forwards its own or system notifications; no logs on the phone beyond the queue;
  `android:allowBackup="false"`; screenshots and photos (phase 3) deleted after upload.

## 2. ADB over the network

### 2.1 Enabling and reconnecting

Android 11+ (<https://developer.android.com/tools/adb>): Developer options, Wireless
debugging, "Pair device with pairing code"; on the server `adb pair ip:port` (enter the code)
then `adb connect ip:port`. Pairing persists "until you explicitly forget it or revoke adb
debugging authorizations" and the host reconnects automatically on the same network; the
connect port changes per session (`adb mdns services` finds it on the same L2 network,
**[unverified over WireGuard]**). Android 10 and lower: `adb tcpip 5555` over USB, then
`adb connect ip:5555`; 11+ wireless debugging has its own pairing and does not use 5555.

Reboot: the Wireless debugging toggle switches itself off after a reboot or inactivity on
current stable builds (GrapheneOS forum, Shizuku manual). Google's auto-enable on trusted
networks was Canary-only on 2025-12-12, expected in Android 16 QPR3 or 17
(<https://www.androidauthority.com/android-wireless-adb-auto-reconnect-3624945/>)
**[unverified whether shipped]**. In order of preference: (1) no ADB dependence in phases 0
to 2, maintenance only; (2) phase 3 uses the on-device agent (2.4); (3) if raw ADB is still
needed, a small always-on host at the site (a Raspberry Pi) on a powered USB hub, phone on
USB (charging from the same cable), the host runs `adb` over USB as a WireGuard peer, and USB
debugging survives reboots once the host key is authorised (a device owner can set
`setGlobalSetting(ADB_ENABLED, "1")`, but `adb_wifi_enabled` is not in that allowlist and
`settings put global adb_wifi_enabled 1` needs a shell first **[unverified]**); (4) rooting
or a custom ROM is rejected: it breaks attestation and Play Integrity and risks WhatsApp bans.

### 2.2 Security and mitigations

A paired adb host (`shell` user) can install apps, inject input, read the screen, change settings and factory-reset the phone.

- Phone on a dedicated SSID/VLAN with nothing else on it; wireless debugging enabled only
  there and only during a maintenance window; the router blocks LAN-to-phone inbound.
- WireGuard (<https://www.wireguard.com/install/>) to the UAE server, always-on with
  lockdown. Device owner: `setAlwaysOnVpnPackage(admin, "com.wireguard.android", true)`:
  "This connection is automatically granted and persisted after a reboot"; lockdown
  "disallow[s] networking when the VPN is not connected". Keep the lockdown allowlist empty
  so a VPN failure stops the companion too (the watchdog then fires, the right outcome), and
  add `UserManager.DISALLOW_CONFIG_VPN`. AMAPI: `alwaysOnVpnPackage{packageName,
  lockdownEnabled}`. Consumer Settings ("Always-on VPN", "Block connections without VPN",
  <https://support.google.com/android/answer/9089766>) is the fallback without device owner.
- Server firewall: only the WireGuard UDP port is public; `adb connect` targets the phone's
  tunnel address only. Outside maintenance: revoke authorisations on the phone and set
  `UserManager.DISALLOW_DEBUGGING_FEATURES` (AMAPI `advancedSecurityOverrides.developerSettings`).
- Every ADB session is an audit-log action with who, why and the commands run.

### 2.3 Commands for phase 3 app control (if ADB is used)

| Need | Command |
|---|---|
| Tap / swipe / key | `adb shell input tap X Y`; `input swipe X1 Y1 X2 Y2 [ms]`; `input keyevent KEYCODE_BACK` (4), `KEYCODE_HOME` (3), `KEYCODE_POWER` (26) |
| Text | `adb shell input text 'hello%sworld'` (`%s` = space; ASCII only, so no Arabic; use `ACTION_SET_TEXT` through the accessibility agent instead) |
| Screenshot | `adb exec-out screencap -p > screen.png`; `adb shell screenrecord --time-limit 30 /sdcard/demo.mp4` |
| UI tree | `adb shell uiautomator dump [--compressed] /sdcard/ui.xml` then `adb pull`; default file `window_dump.xml` (AOSP `DumpCommand.java`) |
| Launch | `adb shell am start -n pkg/.Activity`; `am start -a android.intent.action.VIEW -d "https://..."`; `am force-stop pkg` |
| Install | `adb install -r app.apk` |
| Grants | `cmd notification allow_listener pkg/.Svc`; `settings put secure enabled_accessibility_services pkg/.Svc` + `settings put secure accessibility_enabled 1` **[unverified]** |
| State | `dumpsys battery`; `dumpsys deviceidle whitelist +pkg` **[unverified flag]** |

`input` syntax is from the on-device usage text (`adb shell input` with no arguments; the
AOSP source has moved and was not re-verified). `uiautomator dump` takes about a second,
fails while the UI is "not idle" and misses WebView/canvas content. Sources:
<https://developer.android.com/tools/adb#shellcommands>,
<https://android.googlesource.com/platform/frameworks/base/+/refs/heads/main/cmds/uiautomator/cmds/uiautomator/src/com/android/commands/uiautomator/DumpCommand.java>.

### 2.4 The better alternative: an on-device accessibility-driven agent

Keep the planner on the server and put the executor on the phone. The companion's
`AccessibilityService` (<https://developer.android.com/reference/android/accessibilityservice/AccessibilityService>)
offers `getRootInActiveWindow()` (semantic tree: resource ids, text, content descriptions,
bounds, clickable flags), `AccessibilityNodeInfo.performAction(ACTION_CLICK | ACTION_SET_TEXT
| ACTION_SCROLL_FORWARD ...)` (Arabic text works), `dispatchGesture()` (needs
`canPerformGestures`), `performGlobalAction(GLOBAL_ACTION_BACK | HOME | RECENTS |
NOTIFICATIONS | LOCK_SCREEN | TAKE_SCREENSHOT)` and `takeScreenshot(Display.DEFAULT_DISPLAY,
...)` (needs `canTakeScreenshot`). The brain sends typed commands over the same WebSocket
(`open_app`, `dump_tree`, `tap_node`, `set_text`, `swipe`, `global_action`, `screenshot`,
`capture_photo`, `locate`); the phone executes and returns the new tree or image. Gains over
ADB: no debug port, no reboot fragility, a structured tree instead of XML scraping, every
command a typed tier N tool call. Limits: `FLAG_SECURE` apps give black screenshots; WebView,
Flutter and games expose thin trees (fall back to screenshot plus OCR); OTP text stays
redacted. A device owner pins the service with `setPermittedAccessibilityServices`. Open-source
agents built this way (Omni, OpenDroid, PokeClaw, Tetra on GitHub) show the pattern; none is
audited, so do not install them.

## 3. Hardening checklist for the dedicated agent phone

1. Hardware: a Pixel 6a or newer (stock Android, long update window, 80 % charge limit) or
   another Android Enterprise Recommended device; OEM battery managers that kill background
   services are a known risk **[unverified per model]**.
2. Factory reset, then make the companion/DPC the **device owner** before any account exists
   (AOSP: "Factory reset the target device", "Ensure the device does not contain any user
   accounts"). Two routes:
   - Android Management API (AMAPI; no DPC to maintain). At the welcome screen tap six times
     and scan the enrolment QR, or type `afw#setup`; Android Device Policy installs itself.
     Policy fields: `applications[].installType` `FORCE_INSTALLED`/`KIOSK`, `playStoreMode`,
     `permittedAccessibilityServices`, `alwaysOnVpnPackage`, `advancedSecurityOverrides.
     {developerSettings, untrustedAppsPolicy, googlePlayProtectVerifyApps}`, `systemUpdate.type`
     `AUTOMATIC`/`WINDOWED`, `passwordRequirements`, `maximumTimeToLock`, `factoryResetDisabled`,
     `frpAdminEmails`, `deviceConnectivityManagement.usbDataAccess`, `locationMode`,
     `keyguardDisabled`, `statusBarDisabled`, `kioskCustomization`. Remote: `enterprises.devices.
     issueCommand` (`LOCK`, `REBOOT`, `RESET_PASSWORD`, `START_LOST_MODE`, `CLEAR_APP_DATA`,
     `REQUEST_DEVICE_INFO`) and `enterprises.devices.delete` = wipe (`wipeDataFlags`:
     `WIPE_EXTERNAL_STORAGE`, `PRESERVE_RESET_PROTECTION_DATA`; not guaranteed "if the device
     remains offline for an extended duration"). §14 caveat: the AMAPI control plane is
     Google-hosted, not in the UAE; it sees device metadata and policy, never message content;
     owner decision. Docs: <https://developers.google.com/android/management/provision-device>,
     `.../reference/rest/v1/enterprises.policies`, `.../enterprises.devices/issueCommand`, `.../enterprises.devices/delete`.
   - Own DPC inside the companion: `adb shell dpm set-device-owner "pkg/.DeviceAdminReceiver"`
     (<https://source.android.com/docs/devices/admin/testing-setup>), then `DevicePolicyManager`
     (<https://developer.android.com/reference/android/app/admin/DevicePolicyManager>):
     `wipeDevice(flags)` (apps targeting Android 14+; `wipeData` from the primary user throws
     `IllegalStateException` there), `lockNow()`, `setAlwaysOnVpnPackage`,
     `setPermittedAccessibilityServices`, `setSystemUpdatePolicy`, `setMaximumTimeToLock`,
     `setRequiredPasswordComplexity(PASSWORD_COMPLEXITY_HIGH)`, `setLockTaskPackages`/
     `setKeyguardDisabled`/`setStatusBarDisabled` (phase 3 kiosk), `addUserRestriction` with
     `DISALLOW_INSTALL_UNKNOWN_SOURCES`, `DISALLOW_DEBUGGING_FEATURES`, `DISALLOW_FACTORY_RESET`,
     `DISALLOW_CONFIG_VPN`, `DISALLOW_USB_FILE_TRANSFER`, `DISALLOW_ADD_USER`, `DISALLOW_SAFE_BOOT`,
     `DISALLOW_MODIFY_ACCOUNTS` (<https://developer.android.com/reference/android/os/UserManager>).
     More code to own; the control plane stays on the UAE server.
3. Separate Google account, created for the phone only (Play updates, Find Hub); never the
   owner's. On an AMAPI fully managed device the managed Google Play account replaces it and
   `START_LOST_MODE`/`LOCK`/wipe replace Find Hub. Find Hub
   (<https://support.google.com/android/answer/6160491>) needs power, network, a signed-in
   account, Find Hub on and visibility on Play; "Erase device" factory-resets and factory
   reset protection then demands that account's password.
4. No personal credentials (§5, §13): no owner accounts, bank apps, password manager or UAE
   Pass. Tool-account passwords live in the server secrets manager; their 2FA is TOTP held
   server-side, not SMS to this SIM (Android 15 redacts those notifications anyway).
5. Screen lock: complexity HIGH, short `maximumTimeToLock`, unlock credential in the vault
   (Tier 2). Phase 3 screen driving needs an unlocked screen: lock task mode with
   `keyguardDisabled` only once the phone sits in a locked room; the change is tier K.
6. SIM PIN on (Pixel: Settings, Security & privacy, More security settings, SIM lock; path
   varies by OEM); PIN and PUK in the vault; three wrong PINs block the SIM until the PUK.
   Trade-off: after any reboot the SIM waits for the PIN, so SMS and calls stop until someone
   on site enters it (the heartbeat reports `sim_state=locked` over Wi-Fi). The PIN keeps a
   pulled SIM useless (§13 kill switch). Ask the carrier for a port-out/SIM-swap lock.
7. Updates: `systemUpdate.type=WINDOWED` (or a windowed `SystemUpdatePolicy`) in the nightly
   maintenance window, because an OS reboot re-asks the SIM PIN and drops wireless ADB; app
   auto-update and Play Protect on; `untrustedAppsPolicy` disallow (the companion is
   force-installed by policy or by `adb install` during provisioning). Developer options and
   USB data disabled outside maintenance (`developerSettings`, `DISALLOW_DEBUGGING_FEATURES`,
   `usbDataAccess`: "Does not impact charging").
8. Charger and thermal: permanently on the charger, so enable Charging optimization "Limit
   to 80%" (Pixel 6a+, Android 11+; Settings, Battery, Battery health, Charging optimization;
   <https://support.google.com/pixelphone/answer/17412942>); a low-wattage charger; out of
   direct sun, air-conditioned (UAE summer); thermal status in the heartbeat; the quarterly
   review checks for a swollen battery. Wi-Fi and mobile data both on.
9. Network: dedicated SSID/VLAN, WireGuard always-on with lockdown, `DISALLOW_CONFIG_VPN`.
   Location: `locationMode` enforced; the companion reports location only on request (phase
   3) and the server logs each request. On-device data: no history; queue purged on ack;
   media deleted after upload; no backups.
10. Physical: office, on charger, on a stand with the camera facing the scanning area, in a
    lockable room. Power off or pull the SIM is the physical kill switch (§13); the
    server-side kill switch never depends on the phone answering.
11. "Remote wipe revocable from the server" = four independent levers, each tier K with
    passphrase (add `phone_lock` and `phone_wipe` to `high_impact_actions` in
    `config/permissions.yaml`): (i) revoke the device row, which kills the channel within one
    heartbeat and alone satisfies "all tokens revocable from the server"; (ii) lock or lost
    mode (AMAPI `LOCK`/`START_LOST_MODE` or `lockNow()`); (iii) wipe (AMAPI `devices.delete`
    or `wipeDevice()`), executed when the phone next checks in, with FRP tied to the enrolment
    account; (iv) the carrier suspends the SIM (owner's phone call; no API). The WhatsApp API
    line is unaffected by all four because it does not run on the phone (section 4).

## 4. WhatsApp: Business app vs Cloud API on the phone

The company lines are WhatsApp Business Platform (Cloud API) numbers through a provider
(§16). Meta's rules (<https://developers.facebook.com/documentation/business-messaging/whatsapp/business-phone-numbers/phone-numbers/>):
the number must be owned by you and "be able to receive voice calls or SMS" for the one-time
SMS or voice verification; "Numbers already in use with WhatsApp cannot be registered unless
they are deleted first"; a registered number "cannot be used with WhatsApp Messenger". So the
phone's SIM can carry the Buzz Avenue number (it receives the verification code and any
re-verification) while no WhatsApp app runs on the phone for that number.

Coexistence (Meta, 2025; <https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/onboarding-business-app-users/>)
lets the WhatsApp Business app (2.24.17+) and the Cloud API share one number through the
Embedded Signup coexistence option: fixed throughput of 20 messages per second; 1:1 chats and
up to six months of history synced both ways; "Group chats will not be synchronized";
"Broadcast lists will be disabled"; profile and catalog stay on the app; partners are told to
keep the app open for the sync; the number cannot be deregistered through the API (only
Settings, Account, Business Platform, Disconnect Account in the app). Warning: do not run
the app on an API number. Two actors on one line breaks "fixed numbers per coat" (§13):
anything typed in the app bypasses the audit log, the self-critic and the send caps; the
brain sees the thread only as well as the sync works; a provider without coexistence support
fails registration; and a stolen phone would hold a live customer line. A hand-held line for
the owner gets its own number.

What the phone still does for messaging: holds the SIM for the Buzz Avenue number
(verification, inbound voice until the AI voice provider owns calls) or a separate operator
SIM if the API number is provider-hosted; the owner thread (`owner_thread.channel: whatsapp`
in `config/channels.yaml`) runs on an API line, where the sender number is verified
server-side from the webhook and nothing is truncated or redacted, never through phone
notifications; app-only services (delivery, marketplaces, utilities, courier and ride apps)
are the real reason for the phone, surfaced in phase 0 and driven in phase 3; camera and OCR
go through the companion; SMS OTPs for tool accounts are avoided by design and, if
unavoidable, read directly (`SMS_RECEIVED` receiver, `RECEIVE_SMS`) rather than through
notifications, which Android 15+ redacts; consumer WhatsApp on the phone: never.

## 5. Python ports and in-memory fakes

Models are pydantic (`PhoneNotification` mirrors the 1.2 event, `PhoneHeartbeat` the 1.4
beat with `sim_state: Literal["ready", "locked", "absent"]`). Phase 0 implements only
`PhoneNotificationPort`; `PhoneControlPort` and `PhoneAdminPort` ship as fakes until phase 3.

```python
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Literal, Protocol


class PhoneNotificationPort(Protocol):
    def stream(self) -> AsyncIterator[PhoneNotification]: ...
    async def ack(self, event_id: str) -> None: ...
    async def last_heartbeat(self, device_id: str) -> PhoneHeartbeat | None: ...
    def is_online(
        self, device_id: str, now: datetime, grace: timedelta = timedelta(seconds=90)
    ) -> bool: ...
    async def revoke_device(self, device_id: str, reason: str) -> None: ...


class ControlResult(BaseModel):
    ok: bool
    command_id: str
    tree: UiTree | None = None
    image_png: bytes | None = None
    error: str | None = None
    took_ms: int = 0


class PhoneControlPort(Protocol):  # phase 3; every call is a tier N tool call
    async def open_app(self, package: str) -> ControlResult: ...
    async def dump_tree(self) -> ControlResult: ...
    async def tap(self, x: int, y: int) -> ControlResult: ...
    async def tap_node(self, node_id: str) -> ControlResult: ...
    async def set_text(self, node_id: str, text: str) -> ControlResult: ...
    async def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 300) -> ControlResult: ...
    async def global_action(
        self, action: Literal["back", "home", "recents", "notifications", "lock"]
    ) -> ControlResult: ...
    async def screenshot(self) -> ControlResult: ...
    async def capture_photo(self, camera: Literal["back", "front"]) -> ControlResult: ...
    async def locate(self) -> Location | None: ...


class PhoneAdminPort(Protocol):  # tier K with passphrase; AMAPI or own DPC
    async def lock(self, device_id: str, reason: str) -> None: ...
    async def lost_mode(self, device_id: str, message: str, on: bool) -> None: ...
    async def wipe(self, device_id: str, reason: str) -> None: ...
```

Fakes (plain state, no sockets; `tests/` drives them with `freezegun`):

- `FakePhoneNotificationPort`: `inject(notification)` appends to the queue `stream()` yields
  from; `acked: list[str]`; `beat(device_id, at=None, **overrides)` stores a heartbeat
  (defaults: charging, 80 %, thermal 0, listener on, SIM ready); `go_offline(device_id)`
  freezes the stored beat so `is_online` turns false once `now - sent_at > grace`; `go_online`
  resumes; `inject` after `revoke_device` raises `DeviceRevoked`. Watchdog test: `beat(t0)`,
  advance the clock 91 s, assert `is_online` is false, a `phone_offline` incident is open and
  phone-dependent tasks are paused; `beat(t0 + 120 s)` clears it; `beat(sim_state="locked")`
  and `beat(thermal_status=3)` each raise their own alert.
- `FakePhoneControlPort`: `commands: list[ControlCommand]` records every call in order with
  arguments; `screens: dict[str, UiTree]` plus `set_screen(name)` decide what `dump_tree` and
  `screenshot` return; `fail_next(error)` makes the next call return `ok=False`; `latency_ms`
  is added to `took_ms` for timeout tests; `locate()` returns the configured fix or `None`.
- `FakePhoneAdminPort`: records `lock`, `lost_mode` and `wipe` with reasons. That a wipe is
  unreachable without a verified passphrase is asserted at the tool layer, not here.
