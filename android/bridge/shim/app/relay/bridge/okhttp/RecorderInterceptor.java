package app.relay.bridge.okhttp;

import java.io.IOException;
import java.lang.reflect.InvocationHandler;

import okhttp3.Interceptor;
import okhttp3.Response;

/**
 * The bridge's OkHttp interceptor as a real class of the app's class loader (build.py dexes it
 * on its own and Module loads it with InMemoryDexClassLoader next to the app's okhttp3), not a
 * java.lang.reflect.Proxy. A Proxy may only throw the checked exceptions the interface method
 * declares, and R8 strips those declarations from release builds (Kleinanzeigen: no
 * `throws IOException` on Interceptor.intercept), so a failed request's IOException came back
 * as UndeclaredThrowableException, which OkHttp's dispatcher rethrows: the app died on every
 * unreachable host. A real method has no such check: the IOException reaches OkHttp as the
 * network failure it is, and the app's callback sees onFailure.
 *
 * Everything else happens in the module (Module.Recorder), reached through the one interface
 * both class loaders share: java.lang.reflect.InvocationHandler, called with a null method.
 */
public final class RecorderInterceptor implements Interceptor {
    private final InvocationHandler recorder;

    public RecorderInterceptor(InvocationHandler recorder) {
        this.recorder = recorder;
    }

    @Override
    public Response intercept(Chain chain) throws IOException {
        try {
            return (Response) recorder.invoke(this, null, new Object[]{chain});
        } catch (IOException | RuntimeException | Error e) {
            throw e;
        } catch (Throwable t) {  // a checked exception OkHttp does not declare: never crash the app for it
            throw new IOException("relay bridge interceptor: " + t, t);
        }
    }

    @Override
    public String toString() {
        return "RelayBridgeInterceptor";
    }
}
