package okhttp3;

import java.io.IOException;

/** Compile-time stub of OkHttp's public Interceptor: the app's own class is used at runtime. */
public interface Interceptor {
    Response intercept(Chain chain) throws IOException;

    interface Chain {
        Request request();
        Response proceed(Request request) throws IOException;
    }
}
