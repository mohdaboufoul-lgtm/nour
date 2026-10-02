package ae.nour.body

import android.app.Activity
import android.app.NotificationManager
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.SharedPreferences
import android.net.Uri
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.widget.Button
import android.widget.EditText
import android.widget.TextView
import android.widget.Toast
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import java.util.UUID

/**
 * Device-local settings (phone.md §1.4, SPEC §5/§13).
 *
 * - `device_id`: a random UUID minted once on first use; public, plain prefs.
 * - `ingress_url`: the brain's HTTPS base; not secret, plain prefs.
 * - `device_token`: the pairing token the control panel issued; EncryptedSharedPreferences,
 *   keyed by a hardware-backed Keystore master key. The server can revoke it at any time
 *   (a 401/403 makes the forwarder forget it and purge the queue).
 *
 * Nothing here is ever an owner credential. The phone holds one revocable token and nothing else.
 */
object Config {
    private const val PLAIN = "nour_body"
    private const val SECRET = "nour_body_secret"
    private const val KEY_DEVICE_ID = "device_id"
    private const val KEY_INGRESS = "ingress_url"
    private const val KEY_TOKEN = "device_token"
    private const val KEY_REVOKED = "revoked"

    const val NOTIFICATION_PATH = "/webhooks/phone"            // DESIGN §3.20, Ingress.accept_phone
    const val HEARTBEAT_PATH = "/webhooks/phone/heartbeat"     // proposed sibling route (README)

    @Volatile private var secretPrefs: SharedPreferences? = null

    private fun plain(ctx: Context): SharedPreferences =
        ctx.applicationContext.getSharedPreferences(PLAIN, Context.MODE_PRIVATE)

    private fun secret(ctx: Context): SharedPreferences {
        secretPrefs?.let { return it }
        synchronized(this) {
            secretPrefs?.let { return it }
            val app = ctx.applicationContext
            val key = MasterKey.Builder(app).setKeyScheme(MasterKey.KeyScheme.AES256_GCM).build()
            val prefs = EncryptedSharedPreferences.create(
                app, SECRET, key,
                EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
                EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
            )
            secretPrefs = prefs
            return prefs
        }
    }

    @Synchronized
    fun deviceId(ctx: Context): String {
        val p = plain(ctx)
        p.getString(KEY_DEVICE_ID, null)?.let { return it }
        val id = UUID.randomUUID().toString()
        p.edit().putString(KEY_DEVICE_ID, id).commit()
        return id
    }

    fun ingressUrl(ctx: Context): String? = plain(ctx).getString(KEY_INGRESS, null)

    fun deviceToken(ctx: Context): String? = secret(ctx).getString(KEY_TOKEN, null)

    fun isRevoked(ctx: Context): Boolean = plain(ctx).getBoolean(KEY_REVOKED, false)

    fun isProvisioned(ctx: Context): Boolean =
        !ingressUrl(ctx).isNullOrBlank() && !deviceToken(ctx).isNullOrBlank() && !isRevoked(ctx)

    /** Returns null when the pair is unusable; the ingress must be an https URL with no path. */
    fun save(ctx: Context, ingress: String, token: String): String? {
        val url = ingress.trim().trimEnd('/')
        val tok = token.trim()
        if (!url.startsWith("https://") || url.length <= "https://".length) return "ingress must be https://host[:port]"
        if (url.removePrefix("https://").contains('/')) return "ingress is a base URL, no path"
        if (tok.length < 16) return "token too short"
        secret(ctx).edit().putString(KEY_TOKEN, tok).commit()
        plain(ctx).edit().putString(KEY_INGRESS, url).putBoolean(KEY_REVOKED, false).commit()
        return null
    }

    /** Server said 401/403: the device row was revoked. Forget the token; the queue is purged by the caller. */
    fun markRevoked(ctx: Context) {
        secret(ctx).edit().remove(KEY_TOKEN).commit()
        plain(ctx).edit().putBoolean(KEY_REVOKED, true).commit()
    }

    fun unpair(ctx: Context) {
        secret(ctx).edit().remove(KEY_TOKEN).commit()
        plain(ctx).edit().remove(KEY_INGRESS).remove(KEY_REVOKED).commit()
    }

    /**
     * QR provisioning stub. The control panel renders `nour://pair?ingress=<urlencoded https base>&token=<token>`
     * as a QR code; phase 0 types or pastes it, a later build scans it. Returns (ingress, token) or null.
     */
    fun parsePairing(code: String): Pair<String, String>? {
        val uri = runCatching { Uri.parse(code.trim()) }.getOrNull() ?: return null
        if (uri.scheme != "nour" || uri.host != "pair") return null
        val ingress = uri.getQueryParameter("ingress") ?: return null
        val token = uri.getQueryParameter("token") ?: return null
        return ingress to token
    }
}

/** The only screen: pairing, the two one-time grants, and a status readout. Plain Activity, no AppCompat. */
class ConfigActivity : Activity() {
    private lateinit var status: TextView
    private lateinit var pairing: EditText
    private lateinit var ingress: EditText
    private lateinit var token: EditText

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_config)
        status = findViewById(R.id.status)
        pairing = findViewById(R.id.pairing)
        ingress = findViewById(R.id.ingress)
        token = findViewById(R.id.token)
        ingress.setText(Config.ingressUrl(this).orEmpty())

        findViewById<Button>(R.id.save).setOnClickListener { save() }
        findViewById<Button>(R.id.notification_access).setOnClickListener {
            startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS))
        }
        findViewById<Button>(R.id.battery).setOnClickListener {
            startActivity(
                Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS)
                    .setData(Uri.parse("package:$packageName"))
            )
        }
        findViewById<Button>(R.id.unpair).setOnClickListener {
            Config.unpair(this)
            OutboxQueue.get(this).purge()
            stopService(Intent(this, ForwarderService::class.java))
            refresh()
        }
    }

    override fun onResume() {
        super.onResume()
        refresh()
    }

    private fun save() {
        val pair = pairing.text.toString().takeIf { it.isNotBlank() }?.let { code ->
            Config.parsePairing(code) ?: run { toast("pairing code not understood"); return }
        } ?: (ingress.text.toString() to token.text.toString())
        Config.save(this, pair.first, pair.second)?.let { toast(it); return }
        pairing.text.clear()
        token.text.clear()
        ingress.setText(Config.ingressUrl(this).orEmpty())
        ForwarderService.start(this)
        toast("paired; service started")
        refresh()
    }

    private fun refresh() {
        val nm = getSystemService(NotificationManager::class.java)
        val pm = getSystemService(PowerManager::class.java)
        val listenerGranted = nm.isNotificationListenerAccessGranted(
            ComponentName(this, NourNotificationListener::class.java)
        )
        status.text = buildString {
            appendLine("device_id        ${Config.deviceId(this@ConfigActivity)}")
            appendLine("ingress          ${Config.ingressUrl(this@ConfigActivity) ?: "-"}")
            appendLine("token            ${if (Config.deviceToken(this@ConfigActivity) != null) "set" else "-"}")
            appendLine("revoked          ${Config.isRevoked(this@ConfigActivity)}")
            appendLine("listener access  $listenerGranted")
            appendLine("listener bound   ${NourNotificationListener.connected}")
            appendLine("battery exempt   ${pm.isIgnoringBatteryOptimizations(packageName)}")
            appendLine("queue depth      ${OutboxQueue.get(this@ConfigActivity).depth()}")
            appendLine("last heartbeat   ${ForwarderService.lastHeartbeatAt ?: "never"}")
            append("status           ${ForwarderService.lastStatus}")
        }
    }

    private fun toast(msg: String) = Toast.makeText(this, msg, Toast.LENGTH_SHORT).show()
}
