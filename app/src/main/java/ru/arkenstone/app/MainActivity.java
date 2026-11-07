package ru.arkenstone.app;

import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.graphics.Bitmap;
import android.net.Uri;
import android.os.Bundle;
import android.view.View;
import android.webkit.JavascriptInterface;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

import android.Manifest;
import android.content.pm.PackageManager;
import android.os.Build;

import com.google.firebase.messaging.FirebaseMessaging;

import org.json.JSONObject;

/**
 * Приложение Arkenstone: окно браузера, которое открывает экран сотрудников с сервера бота.
 * Вся логика живёт на сервере, поэтому обновления экранов приходят без переустановки APK.
 */
public class MainActivity extends Activity {

    private static final int PICK_FILE = 1;
    private WebView web;
    private String appUrl;
    private String appHost;
    private ValueCallback<Uri[]> fileCallback;
    /** Приложение сейчас на экране — уведомления чата не показываем, сообщения и так видно. */
    static volatile boolean visible;
    private Updater updater;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        appUrl = getString(R.string.app_url);
        appHost = Uri.parse(appUrl).getHost();

        web = new WebView(this);
        web.setBackgroundColor(0xFF1D2A24);
        setContentView(web);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);   // тут хранится ключ телефона — без него вход сбросится
        s.setDatabaseEnabled(true);
        s.setTextZoom(100);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        s.setUserAgentString(s.getUserAgentString() + " ArkenstoneApp/" + BuildConfigHelper.version(this));

        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest req) {
                return openOutside(req.getUrl());
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest req, WebResourceError err) {
                if (req.isForMainFrame()) showOffline();
            }

            @Override
            public void onPageStarted(WebView view, String url, Bitmap favicon) {
                super.onPageStarted(view, url, favicon);
            }
        });

        web.setWebChromeClient(new WebChromeClient() {
            // «Загрузить PDF с заказами»: выбор файла на телефоне
            @Override
            public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback, FileChooserParams params) {
                if (fileCallback != null) fileCallback.onReceiveValue(null);
                fileCallback = callback;
                // Что выбирать, решает страница: PDF с заказами или фото для чата
                String type = "*/*", title = "Выберите файл";
                for (String t : params.getAcceptTypes()) {
                    if (t == null) continue;
                    if (t.startsWith("image")) { type = "image/*"; title = "Выберите фото"; break; }
                    if (t.contains("pdf")) { type = "application/pdf"; title = "Выберите PDF"; }
                }
                Intent pick = new Intent(Intent.ACTION_GET_CONTENT);
                pick.addCategory(Intent.CATEGORY_OPENABLE);
                pick.setType(type);
                try {
                    startActivityForResult(Intent.createChooser(pick, title), PICK_FILE);
                } catch (ActivityNotFoundException e) {
                    fileCallback = null;
                    Toast.makeText(MainActivity.this, "Нет приложения для выбора файлов", Toast.LENGTH_LONG).show();
                    return false;
                }
                return true;
            }
        });

        updater = new Updater(this, new Updater.Ui() {
            @Override public void progress(int pct) { js("window.onUpdateProgress&&onUpdateProgress(" + pct + ")"); }
            @Override public void ready() { js("window.onUpdateReady&&onUpdateReady()"); }
            @Override public void error(String msg) { js("window.onUpdateError&&onUpdateError(" + JSONObject.quote(msg) + ")"); }
            @Override public void needPermission() { js("window.onUpdatePermission&&onUpdatePermission()"); }
        });
        web.addJavascriptInterface(new Bridge(), "ArkNative");

        if (state != null) web.restoreState(state);
        else web.loadUrl(withTab(getIntent()));

        setupNotifications();
    }

    /** Нажали на уведомление: открываем нужную вкладку (чат, отчёты…). */
    private String withTab(Intent i) {
        String tab = i == null ? null : i.getStringExtra("tab");
        return tab == null || tab.isEmpty() ? appUrl : appUrl + "#tab=" + tab;
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        String tab = intent.getStringExtra("tab");
        if (tab != null && !tab.isEmpty() && tab.matches("[a-z]+")) {
            web.evaluateJavascript("window.openTab && openTab('" + tab + "')", null);
        }
    }

    private void setupNotifications() {
        Notifier.channels(this);
        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 7);
        }
        try {
            InboxWorker.schedule(this);
        } catch (Exception ignored) {
        }
        try {  // Firebase есть, только если при сборке был google-services.json
            FirebaseMessaging.getInstance().getToken().addOnSuccessListener(t -> {
                if (t == null || t.equals(Prefs.fcm(this))) return;
                Prefs.setFcm(this, t);
                PushService.register(this, t);
                web.evaluateJavascript("window.onFcmToken && onFcmToken('" + t.replaceAll("[^A-Za-z0-9:_-]", "") + "')", null);
            });
        } catch (Exception ignored) {
            // Firebase не настроен — работает проверка раз в 15 минут
        }
    }

    private void js(String code) {
        web.evaluateJavascript(code, null);
    }

    /** То, что экран приложения может попросить у телефона. */
    private class Bridge {
        @JavascriptInterface
        public long versionCode() {
            return BuildConfigHelper.versionCode(MainActivity.this);
        }

        @JavascriptInterface
        public void update(String url) {
            runOnUiThread(() -> updater.start(url));
        }

        @JavascriptInterface
        public void installUpdate() {
            runOnUiThread(() -> updater.install());
        }

        @JavascriptInterface
        public void setDevice(String token, String server) {
            if (token == null || !token.matches("[A-Za-z0-9_-]{32,128}")) return;
            if (server == null || !server.startsWith("https://")) return;
            boolean first = Prefs.cursor(MainActivity.this) == 0;
            Prefs.setDevice(MainActivity.this, token, server.endsWith("/") ? server : server + "/");
            if (first) InboxWorker.runNow(MainActivity.this);  // запомнить, с какого места ждать уведомления
        }

        @JavascriptInterface
        public String fcmToken() {
            return Prefs.fcm(MainActivity.this);
        }

        @JavascriptInterface
        public boolean notificationsAllowed() {
            return androidx.core.app.NotificationManagerCompat.from(MainActivity.this).areNotificationsEnabled();
        }
    }

    /** Свои страницы открываем внутри, звонки и карты — в приложениях телефона. */
    private boolean openOutside(Uri uri) {
        String scheme = uri.getScheme() == null ? "" : uri.getScheme();
        if (("https".equals(scheme) || "http".equals(scheme)) && appHost.equals(uri.getHost())) {
            return false;
        }
        Intent intent = "tel".equals(scheme) ? new Intent(Intent.ACTION_DIAL, uri) : new Intent(Intent.ACTION_VIEW, uri);
        try {
            startActivity(intent);
        } catch (ActivityNotFoundException e) {
            Toast.makeText(this, "Нечем открыть ссылку", Toast.LENGTH_SHORT).show();
        }
        return true;
    }

    private void showOffline() {
        String html = "<html><head><meta name='viewport' content='width=device-width,initial-scale=1'>"
                + "<style>body{background:#1D2A24;color:#e9efe9;font-family:sans-serif;display:flex;"
                + "flex-direction:column;align-items:center;justify-content:center;height:90vh;text-align:center;padding:20px}"
                + "a{margin-top:24px;background:#4fb487;color:#0d1a13;padding:14px 28px;border-radius:12px;"
                + "text-decoration:none;font-weight:bold}</style></head><body>"
                + "<h2>Нет связи с сервером</h2><div>Проверьте интернет и попробуйте ещё раз.</div>"
                + "<a href='" + appUrl + "'>Повторить</a></body></html>";
        web.loadDataWithBaseURL(appUrl, html, "text/html", "utf-8", appUrl);
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        if (requestCode == PICK_FILE && fileCallback != null) {
            Uri[] result = null;
            if (resultCode == RESULT_OK && data != null && data.getData() != null) {
                result = new Uri[]{data.getData()};
            }
            fileCallback.onReceiveValue(result);
            fileCallback = null;
            return;
        }
        super.onActivityResult(requestCode, resultCode, data);
    }

    /** «Назад»: сначала закрываем открытое окошко в приложении, потом выходим. */
    @Override
    public void onBackPressed() {
        web.evaluateJavascript(
                "(function(){var s=document.querySelector('.sheet-bg');if(s&&window.closeSheet){closeSheet();return 1}return 0})()",
                value -> {
                    if ("1".equals(value)) return;
                    if (web.canGoBack()) web.goBack();
                    else MainActivity.super.onBackPressed();
                });
    }

    @Override
    protected void onSaveInstanceState(Bundle out) {
        super.onSaveInstanceState(out);
        web.saveState(out);
    }

    @Override
    protected void onResume() {
        super.onResume();
        visible = true;
        if (updater != null) updater.onResume();
        web.onResume();
    }

    @Override
    protected void onPause() {
        visible = false;
        web.onPause();
        super.onPause();
    }
}
