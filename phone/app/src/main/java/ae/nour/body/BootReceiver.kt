package ae.nour.body

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * Boot persistence (phone.md §1.3). BOOT_COMPLETED is an explicit exemption from background
 * foreground-service start limits, and specialUse is a type Android 15 still allows from it.
 * MY_PACKAGE_REPLACED restarts the service after an `adb install -r` upgrade.
 *
 * Caveats the operator must know: a force-stopped app receives no boot broadcast (never
 * "Force stop" this app), and with a screen lock set the broadcast arrives only after the
 * first unlock. The listener itself is rebound by the platform regardless of this receiver.
 */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        when (intent.action) {
            Intent.ACTION_BOOT_COMPLETED, Intent.ACTION_MY_PACKAGE_REPLACED -> ForwarderService.start(context)
        }
    }
}
