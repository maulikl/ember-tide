package app.embertide.wear

import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageInfo
import android.content.pm.PackageInstaller
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.provider.Settings
import java.io.File
import java.io.FileOutputStream
import java.net.HttpURLConnection
import java.net.URL
import java.security.MessageDigest
import java.util.concurrent.atomic.AtomicBoolean

/** In-app APK update for the Wear OS app: version check, verified HTTPS download, signing check, PackageInstaller handoff. */
object WatchUpdate {
    private const val MAX_REDIRECTS = 5
    private const val PREFS_NAME = "embertide_update"
    private const val KEY_LAST_ERROR = "last_error"
    internal const val ACTION_INSTALL_STATUS = "app.embertide.wear.INSTALL_STATUS"

    private val downloading = AtomicBoolean(false)

    sealed class CheckResult {
        data class Available(val offer: Offer) : CheckResult()
        data object UpToDate : CheckResult()
        data object Unavailable : CheckResult()
    }

    sealed class InstallResult {
        data object Started : InstallResult()
        data class Failed(val message: String) : InstallResult()
    }

    /** Blocking; call off the main thread. Returns null on any error or when nothing newer is available. */
    fun check(ctx: Context): Offer? = (checkStatus(ctx) as? CheckResult.Available)?.offer

    /** Blocking; call off the main thread. */
    fun checkStatus(ctx: Context): CheckResult {
        if (!WearApi.ready(ctx)) return CheckResult.Unavailable
        return try {
            val raw = WearApi.call(ctx, "GET", "/api/update")
            val code = getPackageInfo(ctx, 0).longVersionCode
            val offer = WatchLogic.offer(raw, code)
            when {
                offer != null -> CheckResult.Available(offer)
                WatchLogic.isUpToDate(raw, code) -> CheckResult.UpToDate
                else -> CheckResult.Unavailable
            }
        } catch (_: Exception) {
            CheckResult.Unavailable
        }
    }

    /** Blocking; call off the main thread. Downloads, verifies, and commits a PackageInstaller session. */
    fun install(ctx: Context, offer: Offer, onProgress: (Int) -> Unit): InstallResult {
        if (!WatchLogic.validWearEntry(offer.url, offer.sha256, offer.size)) {
            return InstallResult.Failed("That update link doesn't look right.")
        }

        val appCtx = ctx.applicationContext
        if (!appCtx.packageManager.canRequestPackageInstalls()) {
            openUnknownSourcesSettings(appCtx)
            return InstallResult.Failed("Allow Ember & Tide to install updates, then tap Update again.")
        }

        if (!downloading.compareAndSet(false, true)) {
            return InstallResult.Failed("An update is already downloading.")
        }

        val apk = File(File(appCtx.cacheDir, "updates"), "update.apk")
        var conn: HttpURLConnection? = null
        try {
            val dir = apk.parentFile
            if (dir != null && !dir.exists() && !dir.mkdirs()) {
                return InstallResult.Failed("Couldn't prepare a folder for the update.")
            }
            if (apk.exists()) apk.delete()

            conn = openHttpsConnection(offer.url)
            val contentLength = conn.contentLengthLong
            if (contentLength > offer.size) {
                return InstallResult.Failed("The download was damaged. Try again.")
            }

            val md = MessageDigest.getInstance("SHA-256")
            val buf = ByteArray(16384)
            var total = 0L
            var lastPct = -1
            var oversized = false
            conn.inputStream.use { inp ->
                FileOutputStream(apk).use { out ->
                    var n: Int
                    while (inp.read(buf).also { n = it } != -1) {
                        total += n
                        if (total > offer.size) {
                            oversized = true
                            break
                        }
                        md.update(buf, 0, n)
                        out.write(buf, 0, n)
                        val pct = minOf(100L, (total * 100L) / offer.size).toInt()
                        if (pct != lastPct) {
                            lastPct = pct
                            onProgress(pct)
                        }
                    }
                    if (!oversized) out.fd.sync()
                }
            }

            if (oversized || total != offer.size || !toHex(md.digest()).equals(offer.sha256, ignoreCase = true)) {
                apk.delete()
                return InstallResult.Failed("The download was damaged. Try again.")
            }

            val flags = PackageManager.GET_SIGNING_CERTIFICATES
            val pm = appCtx.packageManager
            val installed = getPackageInfo(appCtx, flags)
            val archive = getArchiveInfo(pm, apk.absolutePath, flags)
            if (archive == null || appCtx.packageName != archive.packageName) {
                apk.delete()
                return InstallResult.Failed("That download isn't Ember & Tide, so it wasn't installed.")
            }
            if (archive.signingInfo == null) {
                // Some Android versions return no signing info for an archive: fall back to the older field.
                @Suppress("DEPRECATION")
                val legacy = getArchiveInfo(pm, apk.absolutePath, PackageManager.GET_SIGNATURES)
                @Suppress("DEPRECATION")
                if (legacy != null && appCtx.packageName == legacy.packageName) archive.signatures = legacy.signatures
            }
            val installedCerts = certDigests(installed)
            val archiveCerts = certDigests(archive)
            if (installedCerts.isEmpty() || installedCerts != archiveCerts) {
                apk.delete()
                return InstallResult.Failed("That update isn't signed like this app, so it wasn't installed.")
            }
            if (archive.longVersionCode <= installed.longVersionCode) {
                apk.delete()
                return InstallResult.Failed("That download isn't newer than this app, so it wasn't installed.")
            }

            return commitInstall(appCtx, pm, apk)
        } catch (e: IllegalArgumentException) {
            apk.delete()
            return InstallResult.Failed(e.message ?: "That update link doesn't look right.")
        } catch (e: IllegalStateException) {
            apk.delete()
            return InstallResult.Failed(e.message ?: "Couldn't download the update.")
        } catch (_: Exception) {
            apk.delete()
            return InstallResult.Failed("Couldn't download the update. Check your connection and try again.")
        } finally {
            conn?.disconnect()
            downloading.set(false)
        }
    }

    fun consumeLastError(ctx: Context): String? {
        val prefs = ctx.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        val err = prefs.getString(KEY_LAST_ERROR, null) ?: return null
        prefs.edit().remove(KEY_LAST_ERROR).apply()
        return err
    }

    internal fun saveLastError(ctx: Context, message: String) {
        ctx.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
            .edit()
            .putString(KEY_LAST_ERROR, message)
            .apply()
    }

    private fun clearLastError(ctx: Context) {
        ctx.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
            .edit()
            .remove(KEY_LAST_ERROR)
            .apply()
    }

    private fun openUnknownSourcesSettings(ctx: Context) {
        val manage = Intent(
            Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
            Uri.parse("package:${ctx.packageName}"),
        ).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        try {
            ctx.startActivity(manage)
        } catch (_: Exception) {
            runCatching {
                ctx.startActivity(
                    Intent(
                        Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                        Uri.fromParts("package", ctx.packageName, null),
                    ).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                )
            }
        }
    }

    private fun commitInstall(ctx: Context, pm: PackageManager, apk: File): InstallResult {
        val installer = pm.packageInstaller
        val params = PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL)
        params.setAppPackageName(ctx.packageName)
        var session: PackageInstaller.Session? = null
        return try {
            val sessionId = installer.createSession(params)
            session = installer.openSession(sessionId)
            session.openWrite("update.apk", 0, apk.length()).use { out ->
                apk.inputStream().use { inp ->
                    val buf = ByteArray(16384)
                    var n: Int
                    while (inp.read(buf).also { n = it } != -1) {
                        out.write(buf, 0, n)
                    }
                }
                session.fsync(out)
            }
            clearLastError(ctx)
            val intent = Intent(ctx, WatchUpdateReceiver::class.java)
                .setAction(ACTION_INSTALL_STATUS)
                .setPackage(ctx.packageName)
            // nosemgrep: java.android.security.mutable_pending_intent.mutable_pending_intent (reason: PackageInstaller requires FLAG_MUTABLE so the system installer can fill in status and confirmation extras)
            val mutability = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) PendingIntent.FLAG_MUTABLE else 0
            val pi = PendingIntent.getBroadcast(
                ctx,
                sessionId,
                intent,
                PendingIntent.FLAG_UPDATE_CURRENT or mutability,
            )
            session.commit(pi.intentSender)
            session.close()
            session = null
            InstallResult.Started
        } catch (_: SecurityException) {
            runCatching { session?.abandon() }
            runCatching { session?.close() }
            InstallResult.Failed("Allow Ember & Tide to install updates, then tap Update again.")
        } catch (_: Exception) {
            runCatching { session?.abandon() }
            runCatching { session?.close() }
            InstallResult.Failed("Couldn't start the update installer.")
        } finally {
            apk.delete()
        }
    }

    private fun openHttpsConnection(startUrl: String): HttpURLConnection {
        var cur = startUrl
        for (hop in 0..MAX_REDIRECTS) {
            val u = URL(cur)
            if (!"https".equals(u.protocol, ignoreCase = true)) {
                throw IllegalArgumentException("That update link doesn't look right.")
            }
            val conn = u.openConnection() as HttpURLConnection
            conn.instanceFollowRedirects = false
            conn.connectTimeout = 15_000
            conn.readTimeout = 30_000
            conn.setRequestProperty("User-Agent", "EmberTide-Wear")
            val code = conn.responseCode
            if (code == HttpURLConnection.HTTP_MOVED_TEMP
                || code == HttpURLConnection.HTTP_MOVED_PERM
                || code == HttpURLConnection.HTTP_SEE_OTHER
                || code == 307 || code == 308
            ) {
                val loc = conn.getHeaderField("Location")
                conn.disconnect()
                if (loc.isNullOrEmpty() || hop == MAX_REDIRECTS) {
                    throw IllegalStateException("Couldn't download the update.")
                }
                val next = URL(u, loc)
                if (!"https".equals(next.protocol, ignoreCase = true)) {
                    throw IllegalArgumentException("That update link doesn't look right.")
                }
                cur = next.toString()
                continue
            }
            if (code != HttpURLConnection.HTTP_OK) {
                conn.disconnect()
                throw IllegalStateException("Couldn't download the update (HTTP $code).")
            }
            return conn
        }
        throw IllegalStateException("Couldn't download the update.")
    }

    private fun getPackageInfo(ctx: Context, flags: Int): PackageInfo {
        val pm = ctx.packageManager
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            pm.getPackageInfo(ctx.packageName, PackageManager.PackageInfoFlags.of(flags.toLong()))
        } else {
            @Suppress("DEPRECATION")
            pm.getPackageInfo(ctx.packageName, flags)
        }
    }

    private fun getArchiveInfo(pm: PackageManager, path: String, flags: Int): PackageInfo? {
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            pm.getPackageArchiveInfo(path, PackageManager.PackageInfoFlags.of(flags.toLong()))
        } else {
            @Suppress("DEPRECATION")
            pm.getPackageArchiveInfo(path, flags)
        }
    }

    private fun certDigests(pi: PackageInfo?): Set<String> {
        val out = HashSet<String>()
        if (pi == null) return out
        val si = pi.signingInfo
        @Suppress("DEPRECATION")
        val sigs = if (si == null) {
            pi.signatures
        } else if (si.hasMultipleSigners()) {
            si.apkContentsSigners
        } else {
            si.signingCertificateHistory
        }
        if (sigs != null) {
            val md = MessageDigest.getInstance("SHA-256")
            for (s in sigs) {
                if (s != null) {
                    md.reset()
                    out.add(toHex(md.digest(s.toByteArray())))
                }
            }
        }
        return out
    }

    private fun toHex(bytes: ByteArray): String {
        val sb = StringBuilder(bytes.size * 2)
        for (b in bytes) {
            sb.append(Character.forDigit((b.toInt() shr 4) and 0xF, 16))
            sb.append(Character.forDigit(b.toInt() and 0xF, 16))
        }
        return sb.toString()
    }
}

/** Receives PackageInstaller session results: launches user confirmation or records install failures. */
class WatchUpdateReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (!intent.hasExtra(PackageInstaller.EXTRA_STATUS)) return
        val status = intent.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE)
        when (status) {
            PackageInstaller.STATUS_PENDING_USER_ACTION -> {
                val confirm = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                    intent.getParcelableExtra(Intent.EXTRA_INTENT, Intent::class.java)
                } else {
                    @Suppress("DEPRECATION")
                    intent.getParcelableExtra(Intent.EXTRA_INTENT)
                }
                if (confirm != null) {
                    confirm.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                    runCatching { context.startActivity(confirm) }
                }
            }
            PackageInstaller.STATUS_SUCCESS -> Unit
            else -> {
                val plain = when (status) {
                    PackageInstaller.STATUS_FAILURE_ABORTED -> "Update cancelled."
                    PackageInstaller.STATUS_FAILURE_STORAGE -> "Not enough space on your watch to install the update."
                    PackageInstaller.STATUS_FAILURE_CONFLICT -> "That update conflicts with the installed app."
                    PackageInstaller.STATUS_FAILURE_INCOMPATIBLE -> "That update isn't compatible with this watch."
                    PackageInstaller.STATUS_FAILURE_INVALID -> "The update package wasn't valid."
                    else -> "Couldn't install the update."
                }
                WatchUpdate.saveLastError(context, plain)
            }
        }
    }
}
