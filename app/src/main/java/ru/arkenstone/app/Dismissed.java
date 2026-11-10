package ru.arkenstone.app;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** Карточку уведомления смахнули рукой — забываем её сообщения, чтобы следующее начало новую карточку. */
public class Dismissed extends BroadcastReceiver {
    @Override
    public void onReceive(Context c, Intent i) {
        String g = i.getStringExtra("group");
        if (g != null) Prefs.clearLines(c, g);
    }
}
