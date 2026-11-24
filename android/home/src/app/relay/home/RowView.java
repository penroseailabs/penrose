package app.relay.home;

import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Paint;
import android.graphics.Typeface;
import android.text.TextPaint;
import android.text.TextUtils;
import android.util.TypedValue;
import android.view.View;

/**
 * One result row: a bold title over a smaller line with the kind, subtitle and actions. Drawn, not built
 * from TextViews, so the bridge does not read the rows on screen as well as the list's description of
 * every result (ResultsList).
 */
final class RowView extends View {
    private final TextPaint titlePaint = new TextPaint(Paint.ANTI_ALIAS_FLAG);
    private final TextPaint linePaint = new TextPaint(Paint.ANTI_ALIAS_FLAG);
    private final int padH, padV, gap;
    private String title = "", line = "";

    RowView(Context context) {
        super(context);
        titlePaint.setTextSize(sp(16));
        titlePaint.setTypeface(Typeface.DEFAULT_BOLD);
        titlePaint.setColor(0xDE000000);
        linePaint.setTextSize(sp(12));
        linePaint.setColor(0x8A000000);
        padH = dp(8);
        padV = dp(10);
        gap = dp(2);
        setImportantForAccessibility(IMPORTANT_FOR_ACCESSIBILITY_NO);
    }

    void set(String title, String line) {
        this.title = title;
        this.line = line;
        invalidate();
    }

    @Override
    protected void onMeasure(int widthSpec, int heightSpec) {
        int height = padV * 2 + lineHeight(titlePaint) + gap + lineHeight(linePaint);
        setMeasuredDimension(MeasureSpec.getSize(widthSpec), height);
    }

    @Override
    protected void onDraw(Canvas canvas) {
        float width = getWidth() - padH * 2;
        float y = padV - titlePaint.getFontMetrics().ascent;
        canvas.drawText(TextUtils.ellipsize(title, titlePaint, width, TextUtils.TruncateAt.END).toString(), padH, y, titlePaint);
        y += titlePaint.getFontMetrics().descent + gap - linePaint.getFontMetrics().ascent;
        canvas.drawText(TextUtils.ellipsize(line, linePaint, width, TextUtils.TruncateAt.END).toString(), padH, y, linePaint);
    }

    private static int lineHeight(Paint p) {
        Paint.FontMetrics m = p.getFontMetrics();
        return (int) Math.ceil(m.descent - m.ascent);
    }

    private float sp(int v) {
        return TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_SP, v, getResources().getDisplayMetrics());
    }

    private int dp(int v) {
        return Math.round(v * getResources().getDisplayMetrics().density);
    }
}
