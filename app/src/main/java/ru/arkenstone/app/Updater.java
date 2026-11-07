package ru.arkenstone.app;

import android.app.Activity;
import android.content.Intent;
import android.net.Uri;
import android.os.Build;
import android.provider.Settings;

import androidx.core.content.FileProvider;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;

/**
 * Обновление приложения без магазина: скачиваем новый APK с GitHub и отдаём его установщику Android.
 * Установить «молча» Android не позволяет — человек нажимает «Установить» в системном окне.
 */
final class Updater {
    /** Скачиваем только выпуски нашего репозитория — чужой файл через этот мост не поставить. */
    private static final String ALLOWED = "https://github.com/s1ove1788-lab/aRKENSTONE-BOT/releases/download/";

    interface Ui {
        void progress(int pct);
        void ready();
        void error(String msg);
        void needPermission();
    }

    private final Activity act;
    private final Ui ui;
    private volatile boolean busy;
    private File apk;
    boolean waitingPermission;

    Updater(Activity act, Ui ui) {
        this.act = act;
        this.ui = ui;
    }

    void start(String url) {
        if (busy) return;
        if (url == null || !url.startsWith(ALLOWED) || !url.endsWith(".apk")) {
            ui.error("Неверная ссылка на обновление");
            return;
        }
        busy = true;
        new Thread(() -> {
            try {
                File dir = new File(act.getCacheDir(), "updates");
                if (!dir.isDirectory() && !dir.mkdirs()) throw new Exception("нет места");
                File out = new File(dir, "Arkenstone.apk");
                download(url, out);
                apk = out;
                act.runOnUiThread(() -> {
                    ui.ready();
                    install();
                });
            } catch (Exception e) {
                act.runOnUiThread(() -> ui.error("Не получилось скачать обновление, проверьте интернет"));
            } finally {
                busy = false;
            }
        }).start();
    }

    private void download(String url, File out) throws Exception {
        String u = url;
        HttpURLConnection h = null;
        boolean ok = false;
        for (int i = 0; i < 5 && !ok; i++) {  // GitHub отдаёт файл через перенаправление на другой адрес
            h = (HttpURLConnection) new URL(u).openConnection();
            h.setInstanceFollowRedirects(false);
            h.setConnectTimeout(20000);
            h.setReadTimeout(30000);
            int code = h.getResponseCode();
            if (code / 100 == 3) {
                u = h.getHeaderField("Location");
                h.disconnect();
                if (u == null || !u.startsWith("https://")) throw new Exception("redirect");
                continue;
            }
            if (code != 200) throw new Exception("HTTP " + code);
            ok = true;
        }
        if (!ok) throw new Exception("too many redirects");
        long total = h.getContentLengthLong();
        int last = -1;
        try (InputStream in = h.getInputStream(); OutputStream o = new FileOutputStream(out)) {
            byte[] buf = new byte[64 * 1024];
            long got = 0;
            int n;
            while ((n = in.read(buf)) > 0) {
                o.write(buf, 0, n);
                got += n;
                int pct = total > 0 ? (int) (got * 100 / total) : -1;
                if (pct >= 0 && pct != last) {
                    last = pct;
                    final int p = pct;
                    act.runOnUiThread(() -> ui.progress(p));
                }
            }
        } finally {
            h.disconnect();
        }
        if (out.length() < 100_000) throw new Exception("файл слишком маленький");
    }

    /** Открывает окно установки. Если Android ещё не разрешил приложению ставить обновления — ведёт в настройки. */
    void install() {
        if (apk == null || !apk.exists()) return;
        if (Build.VERSION.SDK_INT >= 26 && !act.getPackageManager().canRequestPackageInstalls()) {
            waitingPermission = true;
            ui.needPermission();
            act.startActivity(new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                    Uri.parse("package:" + act.getPackageName())));
            return;
        }
        waitingPermission = false;
        Uri uri = FileProvider.getUriForFile(act, act.getPackageName() + ".files", apk);
        Intent i = new Intent(Intent.ACTION_VIEW)
                .setDataAndType(uri, "application/vnd.android.package-archive")
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_ACTIVITY_NEW_TASK);
        act.startActivity(i);
    }

    /** Вернулись из настроек: если разрешение дали — сразу ставим. */
    void onResume() {
        if (waitingPermission && Build.VERSION.SDK_INT >= 26 && act.getPackageManager().canRequestPackageInstalls()) {
            install();
        }
    }
}
