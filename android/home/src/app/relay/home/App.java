package app.relay.home;

import android.app.Application;

/** The process: one Search shared by the screen and the agent socket, which listens from the first launch on. */
public class App extends Application {
    static Search search;
    static Agent agent;

    @Override
    public void onCreate() {
        super.onCreate();
        search = new Search(this);
        agent = new Agent(this, search);
        agent.start();
    }
}
