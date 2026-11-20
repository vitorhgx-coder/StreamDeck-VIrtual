package com.streamdeck.painel;

import android.app.Activity;
import android.content.Context;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.view.inputmethod.EditorInfo;
import android.view.inputmethod.InputMethodManager;
import android.webkit.JavascriptInterface;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.TextView;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.NetworkInterface;
import java.net.URL;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Enumeration;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

/**
 * Stream Deck: abre o painel do PC dentro do app (sem navegador).
 * Sem servidor ligado, mostra a tela "PC desconectado" e fica procurando o PC na rede;
 * quando o servidor liga, conecta sozinho. Se o servidor cair, volta para a tela de desconectado.
 */
public class MainActivity extends Activity {
    private static final String PREFS = "streamdeck";
    private static final String KEY_ADDR = "addr";
    private static final int DEFAULT_PORT = 8765;

    private final Handler ui = new Handler(Looper.getMainLooper());
    private FrameLayout root;
    private ScrollView offlineView;
    private WebView web;
    private TextView statusText, hintText;
    private EditText addrEdit;
    private ProgressBar spinner;

    private volatile boolean running = false;
    private volatile boolean connected = false;
    private volatile String base = null;           // ex.: 192.168.0.15:8765 (host:porta atual)
    private volatile boolean forceManual = false;   // usuario pediu para trocar o endereco
    private Thread loop;

    // ------------------------------------------------------------------ ciclo de vida
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        if (Build.VERSION.SDK_INT >= 28) {
            getWindow().getAttributes().layoutInDisplayCutoutMode =
                    WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_SHORT_EDGES;
        }
        buildUi();
        showOffline("Procurando o PC…", "Ligue o servidor no PC (iniciar.bat). Assim que ele ligar, o app conecta sozinho.", true);
        addrEdit.setText(savedAddr());
    }

    @Override
    protected void onResume() {
        super.onResume();
        hideSystemBars();
        startLoop();
    }

    @Override
    protected void onPause() {
        super.onPause();
        running = false;
    }

    @Override
    protected void onDestroy() {
        running = false;
        if (web != null) { web.destroy(); web = null; }
        super.onDestroy();
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) hideSystemBars();
    }

    @SuppressWarnings("deprecation")
    private void hideSystemBars() {
        getWindow().getDecorView().setSystemUiVisibility(
                View.SYSTEM_UI_FLAG_LAYOUT_STABLE | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                        | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                        | View.SYSTEM_UI_FLAG_FULLSCREEN | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY);
    }

    @SuppressWarnings("deprecation")
    @Override
    public void onBackPressed() {
        if (connected && web != null && web.getVisibility() == View.VISIBLE && web.canGoBack()) {
            web.goBack();
        } else {
            super.onBackPressed();
        }
    }

    // ------------------------------------------------------------------ interface
    private int dp(float v) { return Math.round(v * getResources().getDisplayMetrics().density); }

    @SuppressWarnings("SetJavaScriptEnabled")
    private void buildUi() {
        root = new FrameLayout(this);
        root.setBackgroundColor(Color.parseColor("#0A0C10"));

        // ---- WebView (painel)
        web = new WebView(this);
        web.setBackgroundColor(Color.parseColor("#0A0C10"));
        web.setVisibility(View.GONE);
        WebSettings st = web.getSettings();
        st.setJavaScriptEnabled(true);
        st.setDomStorageEnabled(true);
        st.setSupportZoom(false);
        st.setBuiltInZoomControls(false);
        st.setMediaPlaybackRequiresUserGesture(false);
        st.setUserAgentString(st.getUserAgentString() + " StreamDeckApp/1");
        web.setOverScrollMode(View.OVER_SCROLL_NEVER);
        web.setLongClickable(false);
        web.setOnLongClickListener(v -> true);
        web.addJavascriptInterface(new Bridge(), "SDApp");
        web.setWebViewClient(new WebViewClient() {
            @Override
            public void onReceivedError(WebView view, WebResourceRequest req, WebResourceError err) {
                if (req.isForMainFrame()) onServerLost();
            }
        });
        root.addView(web, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        // ---- tela "desconectado"
        LinearLayout col = new LinearLayout(this);
        col.setOrientation(LinearLayout.VERTICAL);
        col.setGravity(Gravity.CENTER_HORIZONTAL);
        col.setPadding(dp(28), dp(36), dp(28), dp(36));

        ImageView logo = new ImageView(this);
        logo.setImageResource(R.drawable.logo);
        col.addView(logo, new LinearLayout.LayoutParams(dp(96), dp(96)));

        TextView title = new TextView(this);
        title.setText("Stream Deck");
        title.setTextColor(Color.WHITE);
        title.setTextSize(24);
        title.setTypeface(Typeface.DEFAULT_BOLD);
        title.setPadding(0, dp(14), 0, dp(4));
        col.addView(title);

        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER_VERTICAL);
        View dot = new View(this);
        GradientDrawable gd = new GradientDrawable();
        gd.setShape(GradientDrawable.OVAL);
        gd.setColor(Color.parseColor("#FF4757"));
        dot.setBackground(gd);
        row.addView(dot, new LinearLayout.LayoutParams(dp(10), dp(10)));
        statusText = new TextView(this);
        statusText.setTextColor(Color.parseColor("#FF8A94"));
        statusText.setTextSize(17);
        statusText.setTypeface(Typeface.DEFAULT_BOLD);
        statusText.setPadding(dp(8), 0, 0, 0);
        row.addView(statusText);
        spinner = new ProgressBar(this);
        row.addView(spinner, new LinearLayout.LayoutParams(dp(18), dp(18)));
        ((LinearLayout.LayoutParams) spinner.getLayoutParams()).leftMargin = dp(10);
        col.addView(row);

        hintText = new TextView(this);
        hintText.setTextColor(Color.parseColor("#8B93A7"));
        hintText.setTextSize(14);
        hintText.setGravity(Gravity.CENTER);
        hintText.setPadding(0, dp(10), 0, dp(22));
        col.addView(hintText, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        TextView lbl = new TextView(this);
        lbl.setText("ENDEREÇO DO PC (opcional)");
        lbl.setTextColor(Color.parseColor("#8B93A7"));
        lbl.setTextSize(11);
        lbl.setLetterSpacing(0.08f);
        lbl.setTypeface(Typeface.DEFAULT_BOLD);
        lbl.setPadding(dp(4), 0, 0, dp(6));
        col.addView(lbl, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        addrEdit = new EditText(this);
        addrEdit.setHint("192.168.0.15:8765");
        addrEdit.setHintTextColor(Color.parseColor("#5B6377"));
        addrEdit.setTextColor(Color.WHITE);
        addrEdit.setSingleLine(true);
        addrEdit.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        addrEdit.setImeOptions(EditorInfo.IME_ACTION_GO);
        GradientDrawable eb = new GradientDrawable();
        eb.setColor(Color.parseColor("#141821"));
        eb.setCornerRadius(dp(12));
        eb.setStroke(dp(1), Color.parseColor("#2A3142"));
        addrEdit.setBackground(eb);
        addrEdit.setPadding(dp(14), dp(12), dp(14), dp(12));
        addrEdit.setOnEditorActionListener((v, actionId, ev) -> { saveAddrFromField(); return true; });
        col.addView(addrEdit, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        Button go = new Button(this);
        go.setText("Conectar neste endereço");
        go.setAllCaps(false);
        go.setTextColor(Color.parseColor("#0A0C10"));
        go.setTypeface(Typeface.DEFAULT_BOLD);
        GradientDrawable gb = new GradientDrawable();
        gb.setColor(Color.parseColor("#8B5CF6"));
        gb.setCornerRadius(dp(12));
        go.setBackground(gb);
        go.setOnClickListener(v -> saveAddrFromField());
        LinearLayout.LayoutParams glp = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(48));
        glp.topMargin = dp(12);
        col.addView(go, glp);

        TextView tip = new TextView(this);
        tip.setText("Deixe em branco para o app procurar o PC sozinho na rede (mesmo Wi-Fi).");
        tip.setTextColor(Color.parseColor("#5B6377"));
        tip.setTextSize(12);
        tip.setGravity(Gravity.CENTER);
        tip.setPadding(0, dp(12), 0, 0);
        col.addView(tip);

        offlineView = new ScrollView(this);
        offlineView.setFillViewport(true);
        FrameLayout.LayoutParams clp = new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        col.setLayoutParams(new ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        LinearLayout holder = new LinearLayout(this);
        holder.setGravity(Gravity.CENTER);
        holder.addView(col, new LinearLayout.LayoutParams(dp(420), ViewGroup.LayoutParams.WRAP_CONTENT));
        offlineView.addView(holder, new ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        root.addView(offlineView, clp.width, clp.height);
        ((FrameLayout.LayoutParams) offlineView.getLayoutParams()).width = ViewGroup.LayoutParams.MATCH_PARENT;
        ((FrameLayout.LayoutParams) offlineView.getLayoutParams()).height = ViewGroup.LayoutParams.MATCH_PARENT;

        setContentView(root);
    }

    private void showOffline(String status, String hint, boolean searching) {
        ui.post(() -> {
            if (web != null) web.setVisibility(View.GONE);
            offlineView.setVisibility(View.VISIBLE);
            statusText.setText(status);
            hintText.setText(hint);
            spinner.setVisibility(searching ? View.VISIBLE : View.GONE);
        });
    }

    private void showPanel() {
        ui.post(() -> {
            offlineView.setVisibility(View.GONE);
            web.setVisibility(View.VISIBLE);
            hideKeyboard();
        });
    }

    private void hideKeyboard() {
        try {
            InputMethodManager imm = (InputMethodManager) getSystemService(Context.INPUT_METHOD_SERVICE);
            if (imm != null) imm.hideSoftInputFromWindow(root.getWindowToken(), 0);
        } catch (Exception ignored) { }
    }

    // ------------------------------------------------------------------ ponte JS (segurar o titulo do painel)
    private class Bridge {
        @JavascriptInterface
        public void changeServer() {
            forceManual = true;
            ui.post(() -> {
                addrEdit.setText(savedAddr());
                onServerLost();
                showOffline("Trocar de PC", "Digite o endereço do PC (ex.: 192.168.0.15:8765) ou deixe em branco para procurar sozinho.", false);
            });
        }
    }

    // ------------------------------------------------------------------ endereco salvo
    private SharedPreferences prefs() { return getSharedPreferences(PREFS, MODE_PRIVATE); }
    private String savedAddr() { return prefs().getString(KEY_ADDR, ""); }

    private void saveAddrFromField() {
        hideKeyboard();
        String a = normalize(addrEdit.getText().toString());
        prefs().edit().putString(KEY_ADDR, a == null ? "" : a).apply();
        forceManual = false;
        if (a != null) addrEdit.setText(a);
        showOffline("Procurando o PC…", a == null ? "Procurando o PC na rede…" : "Tentando " + a + "…", true);
        wake();
    }

    /** "192.168.0.15", "http://pc:8765/" -> "192.168.0.15:8765"; vazio -> null */
    static String normalize(String s) {
        if (s == null) return null;
        s = s.trim().replaceAll("(?i)^https?://", "").replaceAll("/.*$", "");
        if (s.isEmpty()) return null;
        if (!s.matches(".*:\\d{1,5}$")) s = s + ":" + DEFAULT_PORT;
        return s;
    }

    // ------------------------------------------------------------------ laco de conexao
    private final Object lock = new Object();

    private void wake() { synchronized (lock) { lock.notifyAll(); } }

    private void startLoop() {
        if (running) return;
        running = true;
        loop = new Thread(this::runLoop, "sd-loop");
        loop.setDaemon(true);
        loop.start();
    }

    private void runLoop() {
        long lastScan = 0;
        int fails = 0;
        while (running) {
            try {
                if (connected) {
                    if (ping(base, 1500)) fails = 0;
                    else if (++fails >= 2) { fails = 0; onServerLost(); }
                } else {
                    String saved = savedAddr();
                    String target = null;
                    if (!saved.isEmpty() && ping(saved, 1200)) {
                        target = saved;
                    } else if (!forceManual && System.currentTimeMillis() - lastScan > 5000) {
                        lastScan = System.currentTimeMillis();
                        int port = portOf(saved);
                        String found = scanLan(port);
                        if (found != null) { target = found; prefs().edit().putString(KEY_ADDR, found).apply(); }
                    }
                    if (target != null) {
                        final String t = target;
                        base = t;
                        connected = true;
                        ui.post(() -> { addrEdit.setText(t); web.loadUrl("http://" + t + "/"); });
                        showPanel();
                    } else {
                        showOffline(saved.isEmpty() ? "PC desconectado" : "PC desconectado",
                                saved.isEmpty()
                                        ? "Não achei o servidor na rede. Ligue o iniciar.bat no PC (mesmo Wi-Fi do celular). Procurando…"
                                        : "Sem resposta de " + saved + ". Ligue o iniciar.bat no PC; o app conecta sozinho. Procurando…",
                                !forceManual);
                    }
                }
            } catch (Throwable ignored) { }
            synchronized (lock) {
                try { lock.wait(connected ? 3000 : 2000); } catch (InterruptedException e) { return; }
            }
        }
    }

    private void onServerLost() {
        if (!connected) return;
        connected = false;
        showOffline("PC desconectado", "A conexão caiu. Quando o servidor voltar, o app reconecta sozinho. Procurando…", true);
        wake();
    }

    // ------------------------------------------------------------------ rede
    private static int portOf(String hp) {
        try {
            int i = hp.lastIndexOf(':');
            if (i > 0) return Integer.parseInt(hp.substring(i + 1));
        } catch (Exception ignored) { }
        return DEFAULT_PORT;
    }

    /** O servidor responde {"ok": true, "name": "streamdeck"} em /api/ping (sem PIN). */
    static boolean ping(String hostPort, int timeoutMs) {
        if (hostPort == null || hostPort.isEmpty()) return false;
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL("http://" + hostPort + "/api/ping").openConnection();
            c.setConnectTimeout(timeoutMs);
            c.setReadTimeout(timeoutMs);
            c.setUseCaches(false);
            if (c.getResponseCode() != 200) return false;
            BufferedReader r = new BufferedReader(new InputStreamReader(c.getInputStream()));
            String line = r.readLine();
            return line != null && line.contains("streamdeck");
        } catch (Exception e) {
            return false;
        } finally {
            if (c != null) c.disconnect();
        }
    }

    private static String localIp() {
        try {
            List<NetworkInterface> ifs = Collections.list(NetworkInterface.getNetworkInterfaces());
            String fallback = null;
            for (NetworkInterface ni : ifs) {
                if (!ni.isUp() || ni.isLoopback()) continue;
                Enumeration<InetAddress> en = ni.getInetAddresses();
                while (en.hasMoreElements()) {
                    InetAddress a = en.nextElement();
                    if (a instanceof Inet4Address && a.isSiteLocalAddress()) {
                        if (ni.getName().startsWith("wlan")) return a.getHostAddress();
                        if (fallback == null) fallback = a.getHostAddress();
                    }
                }
            }
            return fallback;
        } catch (Exception e) {
            return null;
        }
    }

    /** Procura o servidor em toda a faixa x.y.z.1-254 da rede do celular. */
    private static String scanLan(int port) {
        String ip = localIp();
        if (ip == null) return null;
        final String pre = ip.substring(0, ip.lastIndexOf('.') + 1);
        final AtomicReference<String> found = new AtomicReference<>();
        ExecutorService ex = Executors.newFixedThreadPool(48);
        List<String> hosts = new ArrayList<>();
        for (int i = 1; i < 255; i++) hosts.add(pre + i + ":" + port);
        for (final String h : hosts) {
            ex.execute(() -> { if (found.get() == null && ping(h, 450)) found.compareAndSet(null, h); });
        }
        ex.shutdown();
        try { ex.awaitTermination(10, TimeUnit.SECONDS); } catch (InterruptedException ignored) { }
        ex.shutdownNow();
        return found.get();
    }
}
