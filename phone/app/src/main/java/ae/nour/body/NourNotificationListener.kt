package ae.nour.body

import android.app.Notification
import android.os.Build
import android.os.Bundle
import android.os.Parcelable
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import org.json.JSONObject
import java.time.Instant
import java.time.format.DateTimeFormatter
import java.util.concurrent.Executors

/**
 * Phase 0 body (phone.md §1.1/§1.2): every posted notification becomes one `PhoneNotification`
 * (DESIGN §3.4: id, app, title, text, at, device_id) in the outbox, and the forwarder ships it.
 *
 * Policy lives on the server. This class redacts nothing and filters only what can never be a
 * signal: its own notifications, the system UI's, and group summaries (their children carry the
 * content). Everything forwarded is observed data (`owner_verified=false`); the brain decides.
 *
 * Android 15+: a sideloaded listener is never "trusted" (RECEIVE_SENSITIVE_NOTIFICATIONS is
 * role/signature only), so when the platform detects an OTP it replaces title/text with
 * "Sensitive notification content hidden" before we see it. We forward exactly that; no
 * workaround is attempted. Tool-account 2FA is TOTP held server-side by design (phone.md §3.4).
 */
class NourNotificationListener : NotificationListenerService() {

    private val io = Executors.newSingleThreadExecutor()

    override fun onListenerConnected() {
        connected = true
        ForwarderService.start(this)
    }

    override fun onListenerDisconnected() {
        connected = false
    }

    override fun onNotificationPosted(sbn: StatusBarNotification) {
        val pkg = sbn.packageName
        if (pkg == packageName || pkg in SKIP_PACKAGES) return
        val n = sbn.notification ?: return
        if (n.flags and Notification.FLAG_GROUP_SUMMARY != 0) return

        val extras = n.extras ?: Bundle()
        val title = extras.getCharSequence(Notification.EXTRA_TITLE)?.toString()?.trim().orEmpty()
        val text = bestText(extras)
        if (title.isEmpty() && text.isEmpty()) return

        val id = "${sbn.key}@${sbn.postTime}"
        val at = DateTimeFormatter.ISO_INSTANT.format(Instant.ofEpochMilli(sbn.postTime))

        io.execute {
            val body = JSONObject()
                .put("id", id)
                .put("app", pkg)
                .put("title", title)
                .put("text", text)
                .put("at", at)
                .put("device_id", Config.deviceId(this))
            if (OutboxQueue.get(this).enqueue(id, body.toString())) {
                ForwarderService.kick(this)
            }
        }
    }

    override fun onDestroy() {
        io.shutdown()
        super.onDestroy()
    }

    /**
     * The fullest text the notification carries, in this order: MessagingStyle messages
     * (WhatsApp, SMS, Telegram: "sender: text" per line, oldest first), bigText, InboxStyle
     * lines, then the plain text. Notifications are a signal, not a transport; truncation and
     * grouping lose content and the channel's own API is the source of record (phone.md §1.2).
     */
    private fun bestText(extras: Bundle): String {
        messagingLines(extras)?.let { return it }
        extras.getCharSequence(Notification.EXTRA_BIG_TEXT)?.toString()?.trim()
            ?.takeIf { it.isNotEmpty() }?.let { return it }
        extras.getCharSequenceArray(Notification.EXTRA_TEXT_LINES)
            ?.takeIf { it.isNotEmpty() }
            ?.let { lines -> return lines.joinToString("\n") { it.toString().trim() } }
        return extras.getCharSequence(Notification.EXTRA_TEXT)?.toString()?.trim().orEmpty()
    }

    private fun messagingLines(extras: Bundle): String? {
        val raw: Array<Parcelable>? = if (Build.VERSION.SDK_INT >= 33) {
            extras.getParcelableArray(Notification.EXTRA_MESSAGES, Parcelable::class.java)
        } else {
            @Suppress("DEPRECATION")
            extras.getParcelableArray(Notification.EXTRA_MESSAGES)
        }
        if (raw.isNullOrEmpty()) return null
        val messages = Notification.MessagingStyle.Message.getMessagesFromBundleArray(raw)
        if (messages.isEmpty()) return null
        return messages.joinToString("\n") { m ->
            val who = m.senderPerson?.name?.toString()?.trim().orEmpty()
            val body = m.text?.toString()?.trim().orEmpty()
            if (who.isEmpty()) body else "$who: $body"
        }
    }

    companion object {
        /** True while the system holds the binding; reported in every heartbeat as `listener_connected`. */
        @Volatile var connected: Boolean = false
            private set

        private val SKIP_PACKAGES = setOf("android", "com.android.systemui")
    }
}
