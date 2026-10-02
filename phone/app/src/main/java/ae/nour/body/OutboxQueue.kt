package ae.nour.body

import android.content.ContentValues
import android.content.Context
import android.database.DatabaseUtils
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper

/**
 * On-device at-least-once queue (phone.md §1.4): rows live until the ingress acknowledges them.
 * This is the only data the app keeps; it is bounded (oldest rows dropped past MAX_ROWS) and
 * purged on unpair or revocation. There are no logs on the phone beyond this table.
 */
class OutboxQueue private constructor(ctx: Context) : SQLiteOpenHelper(ctx, "outbox.db", null, 1) {

    data class Row(val seq: Long, val id: String, val body: String, val attempts: Int)

    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL(
            "CREATE TABLE outbox (" +
                "seq INTEGER PRIMARY KEY AUTOINCREMENT, " +
                "id TEXT NOT NULL UNIQUE, " +
                "body TEXT NOT NULL, " +
                "attempts INTEGER NOT NULL DEFAULT 0, " +
                "created_at INTEGER NOT NULL)"
        )
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {
        db.execSQL("DROP TABLE IF EXISTS outbox")
        onCreate(db)
    }

    /** Idempotent on `id`; returns false when the row was already queued. */
    fun enqueue(id: String, body: String): Boolean {
        val values = ContentValues().apply {
            put("id", id)
            put("body", body)
            put("created_at", System.currentTimeMillis())
        }
        val seq = writableDatabase.insertWithOnConflict("outbox", null, values, SQLiteDatabase.CONFLICT_IGNORE)
        trim()
        return seq != -1L
    }

    fun peek(limit: Int): List<Row> {
        val out = ArrayList<Row>(limit)
        readableDatabase.query(
            "outbox", arrayOf("seq", "id", "body", "attempts"), null, null, null, null, "seq ASC", limit.toString(),
        ).use { c ->
            while (c.moveToNext()) {
                out += Row(c.getLong(0), c.getString(1), c.getString(2), c.getInt(3))
            }
        }
        return out
    }

    fun remove(seq: Long) {
        writableDatabase.delete("outbox", "seq = ?", arrayOf(seq.toString()))
    }

    fun bumpAttempts(seq: Long) {
        writableDatabase.execSQL("UPDATE outbox SET attempts = attempts + 1 WHERE seq = ?", arrayOf<Any>(seq))
    }

    fun depth(): Long = DatabaseUtils.queryNumEntries(readableDatabase, "outbox")

    fun purge() {
        writableDatabase.delete("outbox", null, null)
    }

    private fun trim() {
        writableDatabase.execSQL(
            "DELETE FROM outbox WHERE seq NOT IN (SELECT seq FROM outbox ORDER BY seq DESC LIMIT $MAX_ROWS)"
        )
    }

    companion object {
        const val MAX_ROWS = 5_000

        @Volatile private var instance: OutboxQueue? = null

        fun get(ctx: Context): OutboxQueue =
            instance ?: synchronized(this) {
                instance ?: OutboxQueue(ctx.applicationContext).also { instance = it }
            }
    }
}
