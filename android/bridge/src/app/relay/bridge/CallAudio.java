package app.relay.bridge;

import android.content.Context;
import android.media.AudioAttributes;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioTrack;
import android.media.MediaRecorder;
import android.os.Looper;

import java.io.FileInputStream;
import java.io.InputStream;
import java.lang.reflect.Method;

/**
 * Feeds our own audio to every app that records for a call, in place of the microphone.
 *
 * It registers an Android audio policy whose mix matches recorders by capture preset (mic, voice call, ...) and
 * plays raw PCM (48 kHz, mono, 16-bit little endian) into it from a file or stdin. Apps keep recording "the mic"
 * and get our audio instead. Needs MODIFY_AUDIO_ROUTING, so it runs as root through app_process:
 *
 *   su -c "CLASSPATH=$(pm path app.relay.bridge | head -n 1 | cut -d: -f2) app_process /system/bin app.relay.bridge.CallAudio <file.raw|->"
 *
 * The policy only lasts while this process runs. The audio policy classes are system APIs, hence the reflection.
 */
public class CallAudio {
    static final int RATE = 48000;

    public static void main(String[] args) throws Exception {
        String source = args.length > 0 ? args[0] : "-";
        boolean hold = args.length > 1 && args[1].equals("hold");  // keep the policy up after the file ends
        Looper.prepareMainLooper();
        Object thread = Class.forName("android.app.ActivityThread").getMethod("systemMain").invoke(null);
        Context context = (Context) thread.getClass().getMethod("getSystemContext").invoke(thread);
        AudioManager audio = (AudioManager) context.getSystemService(Context.AUDIO_SERVICE);

        Class<?> ruleClass = Class.forName("android.media.audiopolicy.AudioMixingRule");
        Class<?> ruleBuilderClass = Class.forName("android.media.audiopolicy.AudioMixingRule$Builder");
        Object ruleBuilder = ruleBuilderClass.getConstructor().newInstance();
        try {
            int injector = ruleClass.getField("MIX_ROLE_INJECTOR").getInt(null);
            ruleBuilderClass.getMethod("setTargetMixRole", int.class).invoke(ruleBuilder, injector);
            say("mix role: injector");
        } catch (NoSuchFieldException | NoSuchMethodException e) {
            say("no mix roles on this Android, relying on capture-preset rules");
        }
        int matchPreset = ruleClass.getField("RULE_MATCH_ATTRIBUTE_CAPTURE_PRESET").getInt(null);
        Method addMixRule = ruleBuilderClass.getMethod("addMixRule", int.class, Object.class);
        Method setCapturePreset = AudioAttributes.Builder.class.getMethod("setCapturePreset", int.class);
        int[] presets = {MediaRecorder.AudioSource.MIC, MediaRecorder.AudioSource.VOICE_COMMUNICATION,
                MediaRecorder.AudioSource.VOICE_RECOGNITION, MediaRecorder.AudioSource.CAMCORDER,
                MediaRecorder.AudioSource.DEFAULT, MediaRecorder.AudioSource.UNPROCESSED};
        for (int preset : presets) {
            try {
                AudioAttributes.Builder attributes = new AudioAttributes.Builder();
                setCapturePreset.invoke(attributes, preset);
                addMixRule.invoke(ruleBuilder, matchPreset, attributes.build());
            } catch (Exception e) {
                say("preset " + preset + " skipped: " + cause(e));
            }
        }
        Object rule = ruleBuilderClass.getMethod("build").invoke(ruleBuilder);

        AudioFormat format = new AudioFormat.Builder().setSampleRate(RATE)
                .setEncoding(AudioFormat.ENCODING_PCM_16BIT).setChannelMask(AudioFormat.CHANNEL_IN_MONO).build();
        Class<?> mixClass = Class.forName("android.media.audiopolicy.AudioMix");
        Class<?> mixBuilderClass = Class.forName("android.media.audiopolicy.AudioMix$Builder");
        Object mixBuilder = mixBuilderClass.getConstructor(ruleClass).newInstance(rule);
        mixBuilderClass.getMethod("setFormat", AudioFormat.class).invoke(mixBuilder, format);
        int loopBack = mixClass.getField("ROUTE_FLAG_LOOP_BACK").getInt(null);
        mixBuilderClass.getMethod("setRouteFlags", int.class).invoke(mixBuilder, loopBack);
        Object mix = mixBuilderClass.getMethod("build").invoke(mixBuilder);

        Class<?> policyClass = Class.forName("android.media.audiopolicy.AudioPolicy");
        Class<?> policyBuilderClass = Class.forName("android.media.audiopolicy.AudioPolicy$Builder");
        Object policyBuilder = policyBuilderClass.getConstructor(Context.class).newInstance(context);
        policyBuilderClass.getMethod("addMix", mixClass).invoke(policyBuilder, mix);
        Object policy = policyBuilderClass.getMethod("build").invoke(policyBuilder);
        int status = (int) AudioManager.class.getMethod("registerAudioPolicy", policyClass).invoke(audio, policy);
        say("registerAudioPolicy: " + status + (status == 0 ? " (ok)" : " (error)"));
        if (status != 0) System.exit(1);

        AudioTrack track = (AudioTrack) policyClass.getMethod("createAudioTrackSource", mixClass).invoke(policy, mix);
        say("track: rate=" + track.getSampleRate() + " channels=" + track.getChannelCount() + " state=" + track.getState());
        track.play();

        Runtime.getRuntime().addShutdownHook(new Thread() {
            @Override
            public void run() {
                try {
                    AudioManager.class.getMethod("unregisterAudioPolicy", policyClass).invoke(audio, policy);
                } catch (Exception ignored) {
                }
            }
        });

        if (source.equals("serve")) {
            serve(track, args.length > 1 ? args[1] : "relay_callaudio");
            System.exit(0);
        }

        InputStream in = source.equals("-") ? System.in : new FileInputStream(source);
        byte[] chunk = new byte[RATE * 2 / 50];  // 20 ms
        long written = 0;
        int n;
        while ((n = in.read(chunk)) > 0) {
            written += track.write(chunk, 0, n);  // blocking write: paced by the recorders reading the mix
        }
        say("played " + (written / 2.0 / RATE) + " s");
        if (hold) {
            say("holding the policy; ctrl-c to stop");
            byte[] silence = new byte[chunk.length];
            while (true) track.write(silence, 0, silence.length);
        }
        System.exit(0);
    }

    /**
     * Live mode: one client on a local socket (adb forward tcp:N localabstract:NAME) sends frames of
     * [1 byte kind][4 bytes big-endian length][payload]:
     *   'A' audio, 48 kHz mono s16le, queued and played in order;
     *   'F' flush: drop everything queued and not yet heard (the other person started talking);
     *   'Q' quit.
     * While the queue is empty the track gets 10 ms of silence at a time, so the call's clock keeps running and new
     * audio waits behind at most the track's own small buffer. The policy ends when the client goes away.
     */
    static void serve(final AudioTrack track, String name) throws Exception {
        try {
            int frames = RATE * 60 / 1000;  // ask for ~60 ms of buffer: the latency added in front of our audio
            AudioTrack.class.getMethod("setBufferSizeInFrames", int.class).invoke(track, frames);
        } catch (Exception e) {
            say("buffer size unchanged: " + cause(e));
        }
        say("buffer: " + track.getBufferSizeInFrames() + " frames");
        final java.util.concurrent.LinkedBlockingDeque<byte[]> queue = new java.util.concurrent.LinkedBlockingDeque<>();
        final Object lock = new Object();
        // Speech arrives in uneven bursts. Starting a reply the moment its first bytes land, then running dry
        // mid-word, would put gaps of silence into the voice (heard as "breaking up"). So after any silence the
        // writer waits until PREBUFFER of audio is queued (or the burst has gone quiet for as long), then plays it
        // through without gaps.
        final int prebuffer = RATE * 2 * 150 / 1000;  // 150 ms
        Thread writer = new Thread("callaudio-writer") {
            @Override
            public void run() {
                byte[] silence = new byte[RATE * 2 / 100];  // 10 ms
                boolean playing = false;
                long waitingSince = 0;
                try {
                    while (!isInterrupted()) {
                        if (!playing) {
                            int queued = 0;
                            for (byte[] b : queue) queued += b.length;
                            if (queued == 0) {
                                waitingSince = 0;
                            } else {
                                if (waitingSince == 0) waitingSince = System.nanoTime();
                                if (queued >= prebuffer || System.nanoTime() - waitingSince > 150_000_000L) playing = true;
                            }
                        }
                        byte[] chunk = playing ? queue.poll() : null;
                        if (chunk == null) playing = false;
                        synchronized (lock) {
                            if (chunk == null) track.write(silence, 0, silence.length);
                            else track.write(chunk, 0, chunk.length);
                        }
                    }
                } catch (Exception ignored) {
                }
            }
        };
        writer.setDaemon(true);
        writer.start();

        android.net.LocalServerSocket server = new android.net.LocalServerSocket(name);
        say("listening on localabstract:" + name);
        android.net.LocalSocket client = server.accept();
        say("client connected");
        java.io.DataInputStream in = new java.io.DataInputStream(new java.io.BufferedInputStream(client.getInputStream()));
        long received = 0;
        try {
            while (true) {
                int kind = in.read();
                if (kind < 0 || kind == 'Q') break;
                int length = in.readInt();
                byte[] payload = new byte[length];
                in.readFully(payload);
                if (kind == 'A') {
                    queue.add(payload);
                    received += length;
                } else if (kind == 'F') {
                    queue.clear();
                    synchronized (lock) {  // drop what the track holds too, between two writes
                        track.pause();
                        track.flush();
                        track.play();
                    }
                    say("flushed");
                }
            }
        } catch (java.io.EOFException ignored) {
        }
        say("client gone after " + (received / 2.0 / RATE) + " s of audio");
        writer.interrupt();
        client.close();
        server.close();
    }

    static String cause(Throwable e) {
        while (e.getCause() != null) e = e.getCause();
        return e.toString();
    }

    static void say(String line) {
        System.out.println(line);
        System.out.flush();
    }
}
