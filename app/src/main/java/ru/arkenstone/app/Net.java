package ru.arkenstone.app;

import android.content.Context;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/** Запросы к серверу бота от имени этого телефона (тот же ключ, что у экрана приложения). */
final class Net {
    private Net() {}

    static String call(Context c, String path, String jsonBody) throws Exception {
        String server = Prefs.server(c), token = Prefs.token(c);
        if (server.isEmpty() || token.isEmpty()) return null;
        HttpURLConnection h = (HttpURLConnection) new URL(server + path).openConnection();
        h.setConnectTimeout(15000);
        h.setReadTimeout(20000);
        h.setRequestProperty("X-Token", token);
        if (jsonBody != null) {
            h.setRequestMethod("POST");
            h.setDoOutput(true);
            h.setRequestProperty("Content-Type", "application/json");
            try (OutputStream o = h.getOutputStream()) { o.write(jsonBody.getBytes(StandardCharsets.UTF_8)); }
        }
        int code = h.getResponseCode();
        InputStream in = code < 400 ? h.getInputStream() : h.getErrorStream();
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        if (in != null) {
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
            in.close();
        }
        h.disconnect();
        return code < 400 ? out.toString("UTF-8") : null;
    }
}
