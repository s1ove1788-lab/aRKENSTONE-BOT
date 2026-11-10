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

    // Короткая двойная вибрация. Android не даёт поменять настройки уже созданного канала,
    // поэтому у каналов с вибрацией новые имена (…_v), а старые без вибрации удаляем.
    static final long[] BUZZ = {0, 250, 150, 250};

    static void channels(Context c) {
        if (Build.VERSION.SDK_INT < 26) return;
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        for (String old : new String[]{"orders", "chat", "reports"}) {
            try { nm.deleteNotificationChannel(old); } catch (Exception ignored) { }
        }
        nm.createNotificationChannel(channel("orders_v", "Новые заказы", NotificationManager.IMPORTANCE_HIGH));
        nm.createNotificationChannel(channel("chat_v", "Чат", NotificationManager.IMPORTANCE_HIGH));
        nm.createNotificationChannel(channel("reports_v", "Отчёты табеля", NotificationManager.IMPORTANCE_HIGH));
    }

    /** Человек прочитал переписку (или открыл заказы) — убираем её карточку из шторки. */
    static void clear(Context c, String group) {
        if (group == null || group.isEmpty()) return;
        Prefs.clearLines(c, group);
        try { NotificationManagerCompat.from(c).cancel(group, 1); } catch (Exception ignored) { }
    }

    /** Один раз после обновления: убрать уведомления старых версий (они шли по одному на сообщение и не убирались). */
    static void cleanOnce(Context c) {
        if (Prefs.of(c).getBoolean("groups_v1", false)) return;
        try { NotificationManagerCompat.from(c).cancelAll(); } catch (Exception ignored) { }
        Prefs.of(c).edit().putBoolean("groups_v1", true).apply();
    }

    static String plural(int n, String one, String few, String many) {
        int a = Math.abs(n) % 100, b = a % 10;
        return a > 10 && a < 20 ? many : b == 1 ? one : b >= 2 && b <= 4 ? few : many;
    }

    private static NotificationChannel channel(String id, String name, int importance) {
        NotificationChannel ch = new NotificationChannel(id, name, importance);
        ch.enableVibration(true);
        ch.setVibrationPattern(BUZZ);
        ch.enableLights(true);
        ch.setLightColor(0xFF4FB487);
        return ch;
    }

    static void show(Context c, long id, String kind, String title, String body, String tab) {
        show(c, id, kind, title, body, tab, "");
    }

    /**
     * group — переписка или раздел (личка с человеком, общий чат, заказы). В шторке одна карточка на группу:
     * новое сообщение дописывается в неё («Николай · 3 новых»), а когда человек прочитал — карточка убирается целиком.
     */
    static void show(Context c, long id, String kind, String title, String body, String tab, String group) {
        if (id <= 0 || !Prefs.markShown(c, id)) return;
        if (("chat".equals(kind) || "dm".equals(kind)) && MainActivity.visible) return;  // приложение и так открыто
        channels(c);
        String g = group == null || group.isEmpty() ? (kind == null ? "other" : kind) : group;
        String line = "dm".equals(kind) || g.startsWith("dm-") ? body : (title + ": " + body);
        java.util.List<String> lines = Prefs.addLine(c, g, line);
        int n = lines.size();
        String ch = ("orders".equals(kind) || "reports".equals(kind) ? kind : "chat") + "_v";
        Intent open = new Intent(c, MainActivity.class)
                .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP)
                .putExtra("tab", tab == null ? "" : tab);
        PendingIntent pi = PendingIntent.getActivity(c, g.hashCode(), open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        NotificationCompat.Style style;
        if (n > 1) {
            NotificationCompat.InboxStyle inbox = new NotificationCompat.InboxStyle();
            for (String l : lines) inbox.addLine(l);
            inbox.setSummaryText(n + " " + plural(n, "новое", "новых", "новых"));
            style = inbox;
        } else {
            style = new NotificationCompat.BigTextStyle().bigText(body);
        }
        NotificationCompat.Builder b = new NotificationCompat.Builder(c, ch)
                .setSmallIcon(R.drawable.ic_notify)
                .setColor(0xFF1F6B4F)
                .setContentTitle(n > 1 && g.startsWith("dm-") ? title + " · " + n + " " + plural(n, "сообщение", "сообщения", "сообщений") : title)
                .setContentText(body)
                .setNumber(n)
                .setOnlyAlertOnce(false)
                .setShowWhen(true)
                .setWhen(System.currentTimeMillis())
                .setDeleteIntent(PendingIntent.getBroadcast(c, g.hashCode(),
                        new Intent(c, Dismissed.class).putExtra("group", g), PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE))
                .setStyle(style)
                .setPriority(NotificationCompat.PRIORITY_HIGH)
                .setVibrate(BUZZ)                                   // для Android 7 (там нет каналов)
                .setDefaults(NotificationCompat.DEFAULT_SOUND | NotificationCompat.DEFAULT_LIGHTS)
                .setAutoCancel(true)
                .setContentIntent(pi);
        try {
            NotificationManagerCompat nm = NotificationManagerCompat.from(c);
            if (nm.areNotificationsEnabled()) nm.notify(g, 1, b.build());
        } catch (SecurityException ignored) {
            // нет разрешения на уведомления — человек запретил, ничего не делаем
        }
    }
}
