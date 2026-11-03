package ru.arkenstone.app;

import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.graphics.Bitmap;
import android.net.Uri;
import android.os.Bundle;
import android.view.View;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

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
                Intent pick = new Intent(Intent.ACTION_GET_CONTENT);
                pick.addCategory(Intent.CATEGORY_OPENABLE);
                pick.setType("application/pdf");
                try {
                    startActivityForResult(Intent.createChooser(pick, "Выберите PDF"), PICK_FILE);
                } catch (ActivityNotFoundException e) {
                    fileCallback = null;
                    Toast.makeText(MainActivity.this, "Нет приложения для выбора файлов", Toast.LENGTH_LONG).show();
                    return false;
                }
                return true;
            }
        });

        if (state != null) web.restoreState(state);
        else web.loadUrl(appUrl);
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
        web.onResume();
    }

    @Override
    protected void onPause() {
        web.onPause();
        super.onPause();
    }
}
