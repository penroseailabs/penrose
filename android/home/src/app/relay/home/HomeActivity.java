package app.relay.home;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.Editable;
import android.text.TextWatcher;
import android.view.View;
import android.view.ViewGroup;
import android.view.inputmethod.EditorInfo;
import android.widget.BaseAdapter;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * The home screen: a search field and the results, nothing else. The user types; a tap runs a result's
 * first action, a long press offers the others. Plain Views with named ids, so the bridge reads it as
 * cleanly as the agent socket does, and the same lines the agent gets are what the rows show. The
 * results list describes every result to the bridge, not just the rows on screen (ResultsList).
 */
public class HomeActivity extends Activity {
    private static final Handler MAIN = new Handler(Looper.getMainLooper());
    private static final long MAIL_PAUSE_MS = 400;
    private static final long MAIL_WAIT_MS = 20_000;  // the screen shows Gmail's emails when they come, or says not yet
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private final ExecutorService mailWorker = Executors.newSingleThreadExecutor();
    private EditText searchField;
    private TextView status;
    private ProgressBar mailProgress;
    private final List<Result> results = new ArrayList<>();
    private BaseAdapter adapter;
    private int generation;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(dp(16), dp(40), dp(16), dp(8));

        searchField = new EditText(this);
        searchField.setId(R.id.search);
        searchField.setHint(R.string.search_hint);
        searchField.setSingleLine(true);
        searchField.setImeOptions(EditorInfo.IME_ACTION_GO);
        searchField.setTextSize(18);
        root.addView(searchField, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        // The status line, with a spinner while Gmail is searching (the agent's screen reads it as loading).
        LinearLayout statusRow = new LinearLayout(this);
        statusRow.setOrientation(LinearLayout.HORIZONTAL);
        statusRow.setGravity(android.view.Gravity.CENTER_VERTICAL);
        status = new TextView(this);
        status.setId(R.id.status);
        status.setTextSize(12);
        status.setPadding(dp(4), dp(4), dp(4), dp(4));
        statusRow.addView(status);
        mailProgress = new ProgressBar(this, null, android.R.attr.progressBarStyleSmall);
        mailProgress.setId(R.id.mail_progress);
        mailProgress.setIndeterminate(true);
        mailProgress.setContentDescription("searching Gmail");
        mailProgress.setVisibility(View.GONE);
        statusRow.addView(mailProgress, new LinearLayout.LayoutParams(dp(16), dp(16)));
        root.addView(statusRow);

        ResultsList list = new ResultsList(this);
        list.setId(R.id.results);
        list.setDivider(null);
        list.bind(results, new ResultsList.Listener() {
            @Override public void run(Result r, String action) { HomeActivity.this.run(r, action); }
            @Override public void choose(Result r) { HomeActivity.this.choose(r); }
        });
        adapter = new BaseAdapter() {
            @Override public int getCount() { return results.size(); }
            @Override public Object getItem(int i) { return results.get(i); }
            @Override public long getItemId(int i) { return i; }
            @Override public View getView(int i, View convert, ViewGroup parent) { return row(i, convert); }
        };
        list.setAdapter(adapter);
        list.setOnItemClickListener((p, v, i, id) -> run(results.get(i), results.get(i).actions.get(0)));
        list.setOnItemLongClickListener((p, v, i, id) -> { choose(results.get(i)); return true; });
        root.addView(list, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));
        setContentView(root);

        searchField.addTextChangedListener(new TextWatcher() {
            @Override public void beforeTextChanged(CharSequence s, int a, int b, int c) {}
            @Override public void onTextChanged(CharSequence s, int a, int b, int c) {}
            @Override public void afterTextChanged(Editable s) { search(s.toString()); }
        });
        searchField.setOnEditorActionListener((v, actionId, event) -> {
            if (!results.isEmpty()) run(results.get(0), results.get(0).actions.get(0));
            return true;
        });
        search("");
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        // The Home key: back to a clean search.
        searchField.setText("");
        searchField.requestFocus();
    }

    @Override
    protected void onResume() {
        super.onResume();
        searchField.requestFocus();
    }

    /** Whether the home screen shows now. Android lets a launcher start an app or a shortcut only while it does. */
    static volatile boolean showing;

    @Override
    protected void onStart() {
        super.onStart();
        showing = true;
    }

    @Override
    protected void onStop() {
        super.onStop();
        showing = false;
        // Behind another app now: whoever comes back (the user, or an agent after its app was closed or
        // stopped) starts from a clean search, not the last query typed here.
        if (searchField.length() > 0) searchField.setText("");
    }

    @Override
    public void onBackPressed() {
        // A launcher has nowhere to go back to; Back clears the search instead.
        if (searchField.length() > 0) searchField.setText("");
    }

    private void search(String query) {
        final int gen = ++generation;
        worker.execute(() -> {
            List<Result> found = App.search.run(query);
            boolean mailComing = App.search.mailPending(query);
            if (mailComing) App.search.warmMail(query);
            MAIN.post(() -> {
                if (gen != generation) return;  // a newer query is on its way
                results.clear();
                results.addAll(found);
                adapter.notifyDataSetChanged();
                status.setText((query.isEmpty() ? found.size() + " apps" : found.size() + " results")
                    + (mailComing ? " · searching Gmail…" : ""));
                mailProgress.setVisibility(mailComing ? View.VISIBLE : View.GONE);
                // Gmail once typing pauses (it searches its servers), then the results again with its emails.
                if (mailComing) MAIN.postDelayed(() -> {
                    if (gen != generation) return;
                    mailWorker.execute(() -> {
                        App.search.fetchMail(query, MAIL_WAIT_MS);
                        MAIN.post(() -> { if (gen == generation) search(query); });
                    });
                }, MAIL_PAUSE_MS);
            });
        });
    }

    private void run(Result r, String action) {
        String outcome = Actions.perform(this, r, action);
        status.setText(outcome);
        if (outcome.startsWith("refused") || outcome.startsWith("failed")) Toast.makeText(this, outcome, Toast.LENGTH_LONG).show();
    }

    private void choose(Result r) {
        String[] actions = r.actions.toArray(new String[0]);
        new AlertDialog.Builder(this).setTitle(r.kind + ": " + r.title)
            .setItems(actions, (d, which) -> run(r, actions[which])).show();
    }

    private View row(int i, View convert) {
        RowView row = convert instanceof RowView ? (RowView) convert : new RowView(this);
        Result r = results.get(i);
        row.set(r.title, r.describe() + "  {" + String.join(" | ", r.actions) + "}");
        return row;
    }

    private int dp(int v) { return Math.round(v * getResources().getDisplayMetrics().density); }
}
