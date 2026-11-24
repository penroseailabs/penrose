package app.relay.webfixture;

import android.app.Activity;
import android.os.Bundle;
import android.view.ViewGroup;
import android.webkit.JsResult;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;

/**
 * A hybrid screen for exercising relay-bridge's WebView support: native widgets above a WebView
 * that shows assets/index.html (forms, lists, shadow DOM, iframes, a modal, a long page).
 */
public class MainActivity extends Activity {
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);

        TextView header = new TextView(this);
        header.setText("Native header");
        header.setPadding(24, 16, 24, 8);
        root.addView(header);

        final Button nativeButton = new Button(this);
        nativeButton.setText("Native button");
        nativeButton.setOnClickListener(v -> Toast.makeText(this, "native clicked", Toast.LENGTH_SHORT).show());
        root.addView(nativeButton);

        WebView web = new WebView(this);
        WebSettings settings = web.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        web.setWebViewClient(new WebViewClient());
        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onJsAlert(WebView view, String url, String message, JsResult result) {
                Toast.makeText(MainActivity.this, "page alert: " + message, Toast.LENGTH_SHORT).show();
                result.confirm();
                return true;
            }
        });
        root.addView(web, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));
        setContentView(root);

        // `am start ... --es url https://...` points the same screen at a real site.
        String url = getIntent().getStringExtra("url");
        if (url != null) web.loadUrl(url);
        else web.loadDataWithBaseURL("https://fixture.test/", asset("index.html"), "text/html", "utf-8", null);
    }

    private String asset(String name) {
        try (InputStream in = getAssets().open(name)) {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            for (int n; (n = in.read(buf)) > 0; ) out.write(buf, 0, n);
            return out.toString("UTF-8");
        } catch (Exception e) {
            return "<p>asset " + name + " missing: " + e + "</p>";
        }
    }
}
