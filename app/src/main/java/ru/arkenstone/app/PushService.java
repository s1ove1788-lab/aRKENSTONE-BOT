package ru.arkenstone.app;

import androidx.annotation.NonNull;

import com.google.firebase.messaging.FirebaseMessagingService;
import com.google.firebase.messaging.RemoteMessage;

import org.json.JSONObject;

import java.util.Map;

/** Мгновенные уведомления через Firebase. Сервер шлёт только данные, уведомление рисуем сами. */
public class PushService extends FirebaseMessagingService {

    @Override
    public void onMessageReceived(@NonNull RemoteMessage msg) {
        Map<String, String> d = msg.getData();
        long id;
        try { id = Long.parseLong(d.get("id")); } catch (Exception e) { return; }
        Notifier.show(this, id, d.get("kind"), d.get("title"), d.get("body"), d.get("tab"));
    }

    @Override
    public void onNewToken(@NonNull String token) {
        Prefs.setFcm(this, token);
        register(this, token);
    }

    /** Сообщаем серверу новый адрес Firebase этого телефона. */
    static void register(android.content.Context c, String token) {
        new Thread(() -> {
            try {
                Net.call(c, "api/push/register", new JSONObject().put("fcm", token).toString());
            } catch (Exception ignored) {
            }
        }).start();
    }
}
