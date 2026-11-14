package app.embertide;

import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.content.pm.Signature;
import android.content.pm.SigningInfo;
import android.net.Uri;
import android.os.Build;
import android.provider.Settings;

import androidx.core.content.FileProvider;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.security.MessageDigest;
import java.util.HashSet;
import java.util.Locale;
import java.util.Set;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.regex.Pattern;

/** In-app APK update for the Android phone app: version info, verified HTTPS download, signing check, installer handoff. */
@CapacitorPlugin(name = "EmberUpdate")
public class EmberUpdatePlugin extends Plugin {
    private static final String ALLOWED_URL_PREFIX = "https://github.com/Ncbudd/ember-tide/releases/download/";
    private static final Pattern SHA256_RE = Pattern.compile("^[0-9a-fA-F]{64}$");
    private static final long MAX_APK_BYTES = 200L * 1024L * 1024L;
    private static final int MAX_REDIRECTS = 5;

    private final AtomicBoolean downloading = new AtomicBoolean(false);

    @PluginMethod
    public void info(PluginCall call) {
        try {
            Context ctx = getContext();
            PackageInfo pi = getPackageInfo(ctx, 0);
            long versionCode = Build.VERSION.SDK_INT >= Build.VERSION_CODES.P
                    ? pi.getLongVersionCode()
                    : pi.versionCode;
            JSObject r = new JSObject();
            r.put("versionCode", versionCode);
            r.put("versionName", pi.versionName != null ? pi.versionName : "");
            r.put("flavor", EmberFcm.IN_BUILD ? "full" : "minimal");
            call.resolve(r);
        } catch (Exception e) {
            call.reject("Couldn't read the app version.");
        }
    }

    @PluginMethod
    public void install(PluginCall call) {
        final String url = call.getString("url");
        final String sha256 = call.getString("sha256");
        // Capacitor's getLong only accepts a Long; a JSON size under 2 GB arrives as an Integer
        Object rawSize = call.getData().opt("size");
        final long size = rawSize instanceof Number ? ((Number) rawSize).longValue() : -1L;
        if (!isValidInput(url, sha256, size)) {
            call.reject("That update link doesn't look right.");
            return;
        }

        final Context ctx = getContext().getApplicationContext();
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O && !ctx.getPackageManager().canRequestPackageInstalls()) {
            Intent i = new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                    Uri.parse("package:" + ctx.getPackageName()))
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            ctx.startActivity(i);
            call.reject("Allow Ember & Tide to install updates, then tap Update again.", "permission");
            return;
        }

        if (!downloading.compareAndSet(false, true)) {
            call.reject("An update is already downloading.");
            return;
        }

        new Thread(() -> {
            File apk = new File(new File(ctx.getCacheDir(), "updates"), "update.apk");
            HttpURLConnection conn = null;
            try {
                File dir = apk.getParentFile();
                if (dir != null && !dir.exists() && !dir.mkdirs()) {
                    call.reject("Couldn't prepare a folder for the update.");
                    return;
                }
                if (apk.exists()) apk.delete();

                conn = openHttpsConnection(url);
                long contentLength = conn.getContentLengthLong();
                if (contentLength > size) {
                    call.reject("The download was damaged. Try again.");
                    return;
                }

                MessageDigest md = MessageDigest.getInstance("SHA-256");
                byte[] buf = new byte[16384];
                long total = 0;
                int lastPct = -1;
                boolean oversized = false;
                try (InputStream in = conn.getInputStream();
                     FileOutputStream out = new FileOutputStream(apk)) {
                    int n;
                    while ((n = in.read(buf)) != -1) {
                        total += n;
                        if (total > size) {
                            oversized = true;
                            break;
                        }
                        md.update(buf, 0, n);
                        out.write(buf, 0, n);
                        int pct = (int) Math.min(100L, (total * 100L) / size);
                        if (pct != lastPct) {
                            lastPct = pct;
                            JSObject ev = new JSObject();
                            ev.put("percent", pct);
                            notifyListeners("progress", ev);
                        }
                    }
                    if (!oversized) out.getFD().sync();
                }

                if (oversized || total != size || !toHex(md.digest()).equalsIgnoreCase(sha256)) {
                    apk.delete();
                    call.reject("The download was damaged. Try again.");
                    return;
                }

                @SuppressWarnings("deprecation")
                int flags = Build.VERSION.SDK_INT >= Build.VERSION_CODES.P
                        ? PackageManager.GET_SIGNING_CERTIFICATES
                        : PackageManager.GET_SIGNATURES;
                PackageManager pm = ctx.getPackageManager();
                PackageInfo installed = getPackageInfo(ctx, flags);
                PackageInfo archive = getArchiveInfo(pm, apk.getAbsolutePath(), flags);
                if (archive == null || !ctx.getPackageName().equals(archive.packageName)) {
                    apk.delete();
                    call.reject("That download isn't Ember & Tide, so it wasn't installed.");
                    return;
                }
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P && archive.signingInfo == null) {
                    // Some Android versions return no signing info for an archive: fall back to the older field.
                    @SuppressWarnings("deprecation")
                    PackageInfo legacy = getArchiveInfo(pm, apk.getAbsolutePath(), PackageManager.GET_SIGNATURES);
                    if (legacy != null && ctx.getPackageName().equals(legacy.packageName)) archive.signatures = legacy.signatures;
                }
                Set<String> installedCerts = certDigests(installed);
                Set<String> archiveCerts = certDigests(archive);
                if (installedCerts.isEmpty() || !installedCerts.equals(archiveCerts)) {
                    apk.delete();
                    call.reject("That update isn't signed like this app, so it wasn't installed.");
                    return;
                }
                long newCode = Build.VERSION.SDK_INT >= Build.VERSION_CODES.P ? archive.getLongVersionCode() : archive.versionCode;
                long oldCode = Build.VERSION.SDK_INT >= Build.VERSION_CODES.P ? installed.getLongVersionCode() : installed.versionCode;
                if (newCode <= oldCode) {
                    apk.delete();
                    call.reject("That download isn't newer than this app, so it wasn't installed.");
                    return;
                }

                Uri uri = FileProvider.getUriForFile(ctx, ctx.getPackageName() + ".fileprovider", apk);
                Intent intent = new Intent(Intent.ACTION_VIEW)
                        .setDataAndType(uri, "application/vnd.android.package-archive")
                        .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_ACTIVITY_NEW_TASK);
                ctx.startActivity(intent);
                JSObject res = new JSObject();
                res.put("started", true);
                call.resolve(res);
            } catch (IllegalArgumentException | IllegalStateException e) {
                apk.delete();
                call.reject(e.getMessage());
            } catch (Exception e) {
                apk.delete();
                call.reject("Couldn't download the update. Check your connection and try again.");
            } finally {
                if (conn != null) conn.disconnect();
                downloading.set(false);
            }
        }).start();
    }

    private static boolean isValidInput(String url, String sha256, long size) {
        if (url == null || url.length() > 500
                || !url.toLowerCase(Locale.US).startsWith(ALLOWED_URL_PREFIX.toLowerCase(Locale.US))) {
            return false;
        }
        for (int i = 0; i < url.length(); i++) {
            char ch = url.charAt(i);
            if (Character.isWhitespace(ch) || ch < 32) return false;
        }
        if (sha256 == null || !SHA256_RE.matcher(sha256).matches()) {
            return false;
        }
        return size > 0 && size < MAX_APK_BYTES;
    }

    private static HttpURLConnection openHttpsConnection(String startUrl) throws Exception {
        String cur = startUrl;
        for (int hop = 0; hop <= MAX_REDIRECTS; hop++) {
            URL u = new URL(cur);
            if (!"https".equalsIgnoreCase(u.getProtocol())) {
                throw new IllegalArgumentException("That update link doesn't look right.");
            }
            HttpURLConnection conn = (HttpURLConnection) u.openConnection();
            conn.setInstanceFollowRedirects(false);
            conn.setConnectTimeout(15_000);
            conn.setReadTimeout(30_000);
            conn.setRequestProperty("User-Agent", "EmberTide-Android");
            int code = conn.getResponseCode();
            if (code == HttpURLConnection.HTTP_MOVED_TEMP
                    || code == HttpURLConnection.HTTP_MOVED_PERM
                    || code == HttpURLConnection.HTTP_SEE_OTHER
                    || code == 307 || code == 308) {
                String loc = conn.getHeaderField("Location");
                conn.disconnect();
                if (loc == null || loc.isEmpty() || hop == MAX_REDIRECTS) {
                    throw new IllegalStateException("Couldn't download the update.");
                }
                URL next = new URL(u, loc);
                if (!"https".equalsIgnoreCase(next.getProtocol())) {
                    throw new IllegalArgumentException("That update link doesn't look right.");
                }
                cur = next.toString();
                continue;
            }
            if (code != HttpURLConnection.HTTP_OK) {
                conn.disconnect();
                throw new IllegalStateException("Couldn't download the update (HTTP " + code + ").");
            }
            return conn;
        }
        throw new IllegalStateException("Couldn't download the update.");
    }

    @SuppressWarnings("deprecation")
    private static PackageInfo getPackageInfo(Context ctx, int flags) throws PackageManager.NameNotFoundException {
        PackageManager pm = ctx.getPackageManager();
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            return pm.getPackageInfo(ctx.getPackageName(), PackageManager.PackageInfoFlags.of(flags));
        }
        return pm.getPackageInfo(ctx.getPackageName(), flags);
    }

    @SuppressWarnings("deprecation")
    private static PackageInfo getArchiveInfo(PackageManager pm, String path, int flags) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            return pm.getPackageArchiveInfo(path, PackageManager.PackageInfoFlags.of(flags));
        }
        return pm.getPackageArchiveInfo(path, flags);
    }

    private static Set<String> certDigests(PackageInfo pi) throws Exception {
        Set<String> out = new HashSet<>();
        if (pi == null) return out;
        Signature[] sigs = null;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            SigningInfo si = pi.signingInfo;
            if (si == null) {
                @SuppressWarnings("deprecation")
                Signature[] legacy = pi.signatures;
                sigs = legacy;
            } else {
                sigs = si.hasMultipleSigners() ? si.getApkContentsSigners() : si.getSigningCertificateHistory();
            }
        } else {
            @SuppressWarnings("deprecation")
            Signature[] legacy = pi.signatures;
            sigs = legacy;
        }
        if (sigs != null) {
            MessageDigest md = MessageDigest.getInstance("SHA-256");
            for (Signature s : sigs) {
                if (s != null) {
                    md.reset();
                    out.add(toHex(md.digest(s.toByteArray())));
                }
            }
        }
        return out;
    }

    private static String toHex(byte[] bytes) {
        StringBuilder sb = new StringBuilder(bytes.length * 2);
        for (byte b : bytes) {
            sb.append(Character.forDigit((b >> 4) & 0xF, 16));
            sb.append(Character.forDigit(b & 0xF, 16));
        }
        return sb.toString();
    }
}
