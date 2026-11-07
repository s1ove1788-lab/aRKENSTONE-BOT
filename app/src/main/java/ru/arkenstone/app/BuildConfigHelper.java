package ru.arkenstone.app;

import android.content.Context;
import android.content.pm.PackageInfo;

/** Версия приложения для подписи запросов (видно в логах, какая версия APK у сотрудника). */
final class BuildConfigHelper {
    private BuildConfigHelper() {}

    static long versionCode(Context ctx) {
        try {
            PackageInfo p = ctx.getPackageManager().getPackageInfo(ctx.getPackageName(), 0);
            return android.os.Build.VERSION.SDK_INT >= 28 ? p.getLongVersionCode() : p.versionCode;
        } catch (Exception e) {
            return 0;
        }
    }

    static String version(Context ctx) {
        try {
            PackageInfo p = ctx.getPackageManager().getPackageInfo(ctx.getPackageName(), 0);
            return p.versionName;
        } catch (Exception e) {
            return "?";
        }
    }
}
