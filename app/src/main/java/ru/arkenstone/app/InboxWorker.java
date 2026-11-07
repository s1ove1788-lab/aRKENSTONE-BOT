package ru.arkenstone.app;

import android.content.Context;

import androidx.annotation.NonNull;
import androidx.work.Constraints;
import androidx.work.ExistingPeriodicWorkPolicy;
import androidx.work.NetworkType;
import androidx.work.OneTimeWorkRequest;
import androidx.work.PeriodicWorkRequest;
import androidx.work.WorkManager;
import androidx.work.Worker;
import androidx.work.WorkerParameters;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.concurrent.TimeUnit;

/**
 * Запасной путь уведомлений: раз в 15 минут спрашиваем у сервера «что нового для этого телефона».
 * Работает и на телефонах без сервисов Google, и когда Firebase не дошёл.
 */
public class InboxWorker extends Worker {
    public InboxWorker(@NonNull Context c, @NonNull WorkerParameters p) { super(c, p); }

    static void schedule(Context c) {
        Constraints net = new Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build();
        PeriodicWorkRequest w = new PeriodicWorkRequest.Builder(InboxWorker.class, 15, TimeUnit.MINUTES)
                .setConstraints(net).build();
        WorkManager.getInstance(c).enqueueUniquePeriodicWork("inbox", ExistingPeriodicWorkPolicy.KEEP, w);
    }

    static void runNow(Context c) {
        WorkManager.getInstance(c).enqueue(new OneTimeWorkRequest.Builder(InboxWorker.class).build());
    }

    @NonNull
    @Override
    public Result doWork() {
        Context c = getApplicationContext();
        try {
            long cursor = Prefs.cursor(c);
            String res = Net.call(c, "api/push/inbox?after=" + cursor, null);
            if (res == null) return Result.success();
            JSONObject j = new JSONObject(res);
            JSONArray evs = j.optJSONArray("events");
            if (cursor > 0 && evs != null) {
                for (int i = 0; i < evs.length(); i++) {
                    JSONObject e = evs.getJSONObject(i);
                    Notifier.show(c, e.optLong("id"), e.optString("kind"), e.optString("title"),
                            e.optString("body"), e.optString("tab"));
                }
            }
            Prefs.setCursor(c, Math.max(cursor, j.optLong("last", cursor)));
        } catch (Exception ignored) {
            // нет связи — попробуем через 15 минут
        }
        return Result.success();
    }
}
