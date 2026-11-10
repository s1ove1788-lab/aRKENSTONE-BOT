package ru.arkenstone.app;

import android.content.Context;
import android.content.SharedPreferences;

/** Что приложение помнит для уведомлений: ключ телефона, адрес сервера, адрес Firebase, что уже показано. */
final class Prefs {
    private Prefs() {}

    static SharedPreferences of(Context c) {
        return c.getSharedPreferences("ark", Context.MODE_PRIVATE);
    }

    static String token(Context c) { return of(c).getString("device", ""); }
    static String server(Context c) { return of(c).getString("server", ""); }
    static String fcm(Context c) { return of(c).getString("fcm", ""); }
    static long cursor(Context c) { return of(c).getLong("cursor", 0); }

    static void setDevice(Context c, String token, String server) {
        of(c).edit().putString("device", token).putString("server", server).apply();
    }
    static void setFcm(Context c, String t) { of(c).edit().putString("fcm", t).apply(); }
    static void setCursor(Context c, long v) { of(c).edit().putLong("cursor", v).apply(); }

    /** Строки, которые сейчас в карточке группы (последние 6). Возвращает всё, что в ней есть, вместе с новой. */
    static synchronized java.util.List<String> addLine(Context c, String group, String line) {
        java.util.List<String> out = new java.util.ArrayList<>();
        try {
            org.json.JSONArray a = new org.json.JSONArray(of(c).getString("lines:" + group, "[]"));
            for (int i = 0; i < a.length(); i++) out.add(a.getString(i));
        } catch (Exception ignored) { }
        out.add(line == null ? "" : line.length() > 200 ? line.substring(0, 200) + "…" : line);
        while (out.size() > 6) out.remove(0);
        of(c).edit().putString("lines:" + group, new org.json.JSONArray(out).toString()).apply();
        return out;
    }

    static synchronized void clearLines(Context c, String group) {
        of(c).edit().remove("lines:" + group).apply();
    }

    /** Показывали ли уже это уведомление (оно могло прийти и через Firebase, и через проверку раз в 15 минут). */
    static synchronized boolean markShown(Context c, long id) {
        String key = "," + id + ",";
        String shown = of(c).getString("shown", ",");
        if (shown.contains(key)) return false;
        String[] parts = shown.split(",");
        StringBuilder sb = new StringBuilder(",");
        int start = Math.max(0, parts.length - 60);  // помним последние 60
        for (int i = start; i < parts.length; i++) if (!parts[i].isEmpty()) sb.append(parts[i]).append(',');
        sb.append(id).append(',');
        of(c).edit().putString("shown", sb.toString()).apply();
        return true;
    }
}
