package app.relay.home;

import android.Manifest;
import android.content.Context;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.net.Uri;
import android.provider.ContactsContract;

import java.util.ArrayList;
import java.util.List;

/** The user's contacts, by name, with a number to call or message and an email when there is one. */
final class Contacts implements Source {
    static final class Entry {
        final String name, phone, email;
        Entry(String name, String phone, String email) { this.name = name; this.phone = phone; this.email = email; }
    }

    private final Context context;

    Contacts(Context context) { this.context = context; }

    @Override
    public void search(Query q, List<Result> out) {
        if (q.isEmpty()) return;
        if (context.checkSelfPermission(Manifest.permission.READ_CONTACTS) != PackageManager.PERMISSION_GRANTED) return;
        Uri uri = Uri.withAppendedPath(ContactsContract.Contacts.CONTENT_FILTER_URI, Uri.encode(q.raw));
        String[] cols = {ContactsContract.Contacts._ID, ContactsContract.Contacts.DISPLAY_NAME};
        try (Cursor c = context.getContentResolver().query(uri, cols, null, null, null)) {
            if (c == null) return;
            int n = 0;
            while (c.moveToNext() && n++ < 10) {
                String id = c.getString(0), name = c.getString(1);
                if (name == null) continue;
                String phone = first(ContactsContract.CommonDataKinds.Phone.CONTENT_URI, ContactsContract.CommonDataKinds.Phone.CONTACT_ID,
                    id, ContactsContract.CommonDataKinds.Phone.NUMBER);
                String email = first(ContactsContract.CommonDataKinds.Email.CONTENT_URI, ContactsContract.CommonDataKinds.Email.CONTACT_ID,
                    id, ContactsContract.CommonDataKinds.Email.ADDRESS);
                List<String> actions = new ArrayList<>();
                if (phone != null) {
                    actions.add("call");
                    actions.add("sms");
                    if (Actions.installed(context, "com.whatsapp")) actions.add("whatsapp");
                }
                if (email != null) actions.add("email");
                actions.add("show");
                String sub = phone != null ? phone : email != null ? email : "";
                out.add(new Result(Result.CONTACT, name, sub, Math.max(q.score(name), 1) + 5, new Entry(name, phone, email),
                    actions.toArray(new String[0])));
            }
        } catch (Exception ignored) {
        }
    }

    private String first(Uri uri, String idColumn, String id, String column) {
        try (Cursor c = context.getContentResolver().query(uri, new String[]{column}, idColumn + "=?", new String[]{id}, null)) {
            if (c != null && c.moveToFirst()) return c.getString(0);
        } catch (Exception ignored) {
        }
        return null;
    }
}
