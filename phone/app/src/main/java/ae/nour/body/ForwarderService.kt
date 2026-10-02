package ae.nour.body

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.ServiceInfo
import android.os.BatteryManager
import android.os.Build
import android.os.Handler
import android.os.HandlerThread
import android.os.IBinder
import android.os.PowerManager
import android.os.SystemClock
import android.telephony.TelephonyManager
import androidx.core.content.ContextCompat
import org.json.JSONObject
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.time.Instant
import java.time.format.DateTimeFormatter
import java.util.concurrent.ThreadLocalRandom

/**
 * Foreground service (phone.md §1.3/§1.4): drains the outbox to `POST <ingress>/webhooks/phone`
 * with the device bearer token, retrying with exponential backoff and jitter, and posts a
 * heartbeat every [HEARTBEAT_MS] so the server watchdog can declare the body dead.
 *
 * Single worker thread; nothing here touches the main thread except lifecycle callbacks.
 * On 401/403 the server has revoked this device: the token is forgotten, the queue purged and
 * the service stops. Re-pairing from the Config screen is the only way back.
 */
class ForwarderService : Service() {

    private lateinit var thread: HandlerThread
    private lateinit var handler: Handler
    private var backoffMs = BACKOFF_MIN_MS
    private var beating = false

    private val drain = Runnable { drainOnce() }
    private val beat = Runnable {
        heartbeatOnce()
        handler.postDelayed(beat, HEARTBEAT_MS)
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        thread = HandlerThread("nour-forwarder").apply { start() }
        handler = Handler(thread.looper)
        createChannel()
    }

    /**
     * Promote to foreground on every start. On Android 12+ a start from the background without an
     * exemption makes *this* call throw ForegroundServiceStartNotAllowedException; the exemptions we
     * rely on are BOOT_COMPLETED, the system-bound listener and the battery-optimisation allowlist.
     */
    private fun enterForeground(): Boolean {
        val notification = Notification.Builder(this, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.stat_notify_sync)
            .setContentTitle(getString(R.string.fgs_title))
            .setContentText(getString(R.string.fgs_text))
            .setOngoing(true)
            .build()
        return try {
            if (Build.VERSION.SDK_INT >= 34) {
                startForeground(NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
            } else {
                startForeground(NOTIFICATION_ID, notification)
            }
            true
        } catch (e: RuntimeException) {
            lastStatus = "foreground refused: ${e.javaClass.simpleName}"
            false
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (!enterForeground()) {
            // Queued rows are safe in SQLite; the next boot, listener (re)bind or Config visit retries.
            stopSelf()
            return START_NOT_STICKY
        }
        lastStatus = "running"
        handler.post {
            if (!beating) {
                beating = true
                handler.post(beat)
            }
            if (!handler.hasCallbacks(drain)) handler.post(drain)
        }
        return START_STICKY
    }

    override fun onDestroy() {
        handler.removeCallbacksAndMessages(null)
        thread.quitSafely()
        lastStatus = "stopped"
        super.onDestroy()
    }

    // ---- outbox ------------------------------------------------------------------------------

    private fun drainOnce() {
        if (!Config.isProvisioned(this)) {
            lastStatus = if (Config.isRevoked(this)) "revoked by server; re-pair" else "not paired"
            return
        }
        val queue = OutboxQueue.get(this)
        val rows = queue.peek(BATCH)
        if (rows.isEmpty()) {
            backoffMs = BACKOFF_MIN_MS
            return
        }
        val url = Config.ingressUrl(this) + Config.NOTIFICATION_PATH
        val token = Config.deviceToken(this) ?: return
        for (row in rows) {
            when (val r = Http.postJson(url, token, row.body)) {
                Http.Result.Ok -> {
                    queue.remove(row.seq)
                    backoffMs = BACKOFF_MIN_MS
                    lastStatus = "sent ${row.id}"
                }
                is Http.Result.Rejected -> {
                    // The ingress will never accept this body (400/413/422...): drop it so one
                    // bad row cannot block the queue. The brain's audit log is the record, not this phone.
                    queue.remove(row.seq)
                    lastStatus = "rejected HTTP ${r.code} ${row.id}"
                }
                Http.Result.Revoked -> {
                    onRevoked()
                    return
                }
                is Http.Result.Retry -> {
                    queue.bumpAttempts(row.seq)
                    val delay = jitter(backoffMs)
                    lastStatus = "retry in ${delay / 1000}s (${r.reason}), queue ${queue.depth()}"
                    handler.removeCallbacks(drain)
                    handler.postDelayed(drain, delay)
                    backoffMs = minOf(backoffMs * 2, BACKOFF_MAX_MS)
                    return
                }
            }
        }
        handler.post(drain) // the batch filled up; there may be more
    }

    private fun onRevoked() {
        Config.markRevoked(this)
        OutboxQueue.get(this).purge()
        lastStatus = "revoked by server; re-pair"
        handler.removeCallbacksAndMessages(null)
        stopSelf()
    }

    // ---- heartbeat ---------------------------------------------------------------------------

    private fun heartbeatOnce() {
        if (!Config.isProvisioned(this)) return
        val url = Config.ingressUrl(this) + Config.HEARTBEAT_PATH
        val token = Config.deviceToken(this) ?: return
        val pm = getSystemService(PowerManager::class.java)
        val battery = batteryState()
        val body = JSONObject()
            .put("device_id", Config.deviceId(this))
            .put("sent_at", DateTimeFormatter.ISO_INSTANT.format(Instant.now()))
            .put("battery_pct", battery.first)
            .put("charging", battery.second)
            .put("thermal_status", pm.currentThermalStatus)
            .put("listener_connected", NourNotificationListener.connected)
            .put("sim_state", simState())
            .put("queue_depth", OutboxQueue.get(this).depth())
            .put("app_version", BuildConfig.VERSION_NAME)
            .put("uptime_s", SystemClock.elapsedRealtime() / 1000)
        when (val r = Http.postJson(url, token, body.toString())) {
            Http.Result.Ok -> {
                lastHeartbeatAt = body.getString("sent_at")
                lastStatus = "heartbeat ok"
            }
            Http.Result.Revoked -> {
                onRevoked()
                return
            }
            is Http.Result.Rejected -> lastStatus = "heartbeat rejected HTTP ${r.code}"
            is Http.Result.Retry -> lastStatus = "heartbeat failed (${r.reason})"
        }
        // The tick doubles as a periodic drain in case a kick was lost while the service was down.
        if (!handler.hasCallbacks(drain)) handler.post(drain)
    }

    /** (percent or -1, on charger). "Charging" means plugged in: a battery held at the 80 % limit still counts. */
    private fun batteryState(): Pair<Int, Boolean> {
        // Sticky system broadcast read with a null receiver; no receiver flags apply.
        val i = applicationContext.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        val level = i?.getIntExtra(BatteryManager.EXTRA_LEVEL, -1) ?: -1
        val scale = i?.getIntExtra(BatteryManager.EXTRA_SCALE, -1) ?: -1
        val pct = if (level >= 0 && scale > 0) level * 100 / scale else -1
        val plugged = (i?.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0) ?: 0) != 0
        return pct to plugged
    }

    /** phone.md §5: Literal["ready", "locked", "absent"]; locked covers PIN/PUK/network locks. */
    private fun simState(): String =
        when (getSystemService(TelephonyManager::class.java).simState) {
            TelephonyManager.SIM_STATE_READY -> "ready"
            TelephonyManager.SIM_STATE_PIN_REQUIRED,
            TelephonyManager.SIM_STATE_PUK_REQUIRED,
            TelephonyManager.SIM_STATE_NETWORK_LOCKED,
            TelephonyManager.SIM_STATE_PERM_DISABLED -> "locked"
            else -> "absent"
        }

    // ---- plumbing ----------------------------------------------------------------------------

    private fun createChannel() {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, getString(R.string.fgs_channel), NotificationManager.IMPORTANCE_LOW)
        )
    }

    private fun jitter(ms: Long): Long = ms + ThreadLocalRandom.current().nextLong(ms / 2 + 1)

    companion object {
        private const val CHANNEL_ID = "nour_body"
        private const val NOTIFICATION_ID = 1
        private const val HEARTBEAT_MS = 60_000L
        private const val BACKOFF_MIN_MS = 1_000L
        private const val BACKOFF_MAX_MS = 300_000L
        private const val BATCH = 20

        @Volatile var lastStatus: String = "not started"
            private set

        @Volatile var lastHeartbeatAt: String? = null
            private set

        /** Start (or poke) the service. Safe to call from any context; a refused background start is reported, not fatal. */
        fun start(ctx: Context) {
            try {
                ContextCompat.startForegroundService(ctx, Intent(ctx, ForwarderService::class.java))
            } catch (e: RuntimeException) {
                // ForegroundServiceStartNotAllowedException and friends: the row stays queued;
                // the next boot, listener (re)bind or Config screen visit starts us.
                lastStatus = "start refused: ${e.javaClass.simpleName}"
            }
        }

        /** Something new is in the outbox. */
        fun kick(ctx: Context) = start(ctx)
    }
}

/** Minimal HTTPS JSON POST on HttpURLConnection; classifies the outcome for the retry policy. */
private object Http {
    sealed class Result {
        data object Ok : Result()
        data object Revoked : Result()
        data class Rejected(val code: Int) : Result()
        data class Retry(val reason: String) : Result()
    }

    fun postJson(url: String, token: String, body: String): Result {
        if (!url.startsWith("https://")) return Result.Retry("ingress is not https")
        var conn: HttpURLConnection? = null
        return try {
            conn = (URL(url).openConnection() as HttpURLConnection).apply {
                requestMethod = "POST"
                connectTimeout = 10_000
                readTimeout = 20_000
                doOutput = true
                useCaches = false
                setRequestProperty("Authorization", "Bearer $token")
                setRequestProperty("Content-Type", "application/json; charset=utf-8")
                setRequestProperty("User-Agent", "nour-body/${BuildConfig.VERSION_NAME}")
            }
            conn.outputStream.use { it.write(body.toByteArray(Charsets.UTF_8)) }
            val code = conn.responseCode
            (if (code < 400) conn.inputStream else conn.errorStream)?.use { it.readBytes() }
            when {
                code in 200..299 || code == 409 -> Result.Ok            // 409: server already has this id
                code == 401 || code == 403 -> Result.Revoked
                code == 408 || code == 429 || code >= 500 -> Result.Retry("HTTP $code")
                code >= 400 -> Result.Rejected(code)
                else -> Result.Retry("HTTP $code")
            }
        } catch (e: IOException) {
            Result.Retry(e.javaClass.simpleName)
        } finally {
            conn?.disconnect()
        }
    }
}
