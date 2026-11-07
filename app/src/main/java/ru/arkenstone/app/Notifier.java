package ru.arkenstone.app;

import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.os.Build;

import androidx.core.app.NotificationCompat;
import androidx.core.app.NotificationManagerCompat;

/** Показывает уведомления. Каналы: заказы, чат, отчёты — каждый можно выключить в настройках телефона. */
final class Notifier {
    private Notifier() {}

    static void channels(Context c) {
        if (Build.VERSION.SDK_INT < 26) return;
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        nm.createNotificationChannel(new NotificationChannel("orders", "Новые заказы", NotificationManager.IMPORTANCE_HIGH));
        nm.createNotificationChannel(new NotificationChannel("chat", "Чат", NotificationManager.IMPORTANCE_HIGH));
        nm.createNotificationChannel(new NotificationChannel("reports", "Отчёты табеля", NotificationManager.IMPORTANCE_DEFAULT));
    }

    static void show(Context c, long id, String kind, String title, String body, String tab) {
        if (id <= 0 || !Prefs.markShown(c, id)) return;
        if ("chat".equals(kind) && MainActivity.visible) return;  // чат и так открыт на экране
        channels(c);
        String ch = "orders".equals(kind) || "reports".equals(kind) ? kind : "chat";
        Intent open = new Intent(c, MainActivity.class)
                .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP)
                .putExtra("tab", tab == null ? "" : tab);
        PendingIntent pi = PendingIntent.getActivity(c, (int) (id % 100000), open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        NotificationCompat.Builder b = new NotificationCompat.Builder(c, ch)
                .setSmallIcon(R.drawable.ic_notify)
                .setColor(0xFF1F6B4F)
                .setContentTitle(title)
                .setContentText(body)
                .setStyle(new NotificationCompat.BigTextStyle().bigText(body))
                .setPriority(NotificationCompat.PRIORITY_HIGH)
                .setAutoCancel(true)
                .setContentIntent(pi);
        try {
            NotificationManagerCompat nm = NotificationManagerCompat.from(c);
            if (nm.areNotificationsEnabled()) nm.notify(ch, (int) (id % Integer.MAX_VALUE), b.build());
        } catch (SecurityException ignored) {
            // нет разрешения на уведомления — человек запретил, ничего не делаем
        }
    }
}
