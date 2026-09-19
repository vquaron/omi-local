# Connection and live transcription

The local app has a status card on the home screen, in **Local Mac**, and on the
current recording screen. It starts collapsed: tap to expand details, then tap
again to collapse. Errors remain visible when collapsed.
Data refreshes about every 5 seconds while the screen is open, or when you tap
the refresh button in the details. During a request, the last confirmed values
remain visible without flashing `Loading / Unknown`; an error or connection
change clears them. Run **Check and connect** first.

## The Omi button and recording state

The home and recording screens show capture state and brief feedback for completed
button actions separately. A neutral button icon acknowledges each received BLE gesture
for four seconds, including short press and release, even if starting audio stalls.
The separate action result and capture status confirm what happened next.
A short press starts recording; the next one stops it.
Releasing the button does not start or stop recording again.
Raw button events remain diagnostic: some firmware versions report only a recognized
short press and release, so the physical press time cannot be inferred without an event.

A connected Omi that is not recording shows **Not recording**, not Listening.
Startup shows preparation, followed by waiting for data until the first audio packet.
In local live mode, audio packets received on the iPhone confirm recording activity:
after 3 seconds without packets, the status returns to waiting for data. This does
not stop the session; the status recovers when audio resumes. Pause and errors have
separate states. Phone status does not prove Mac storage or successful transcription.

## Mac status

- **Mac**: a successful request authenticated with the app key. A public
  `/v1/health` response alone proves neither authentication nor transcription.
- **Audio**: the amount of audio the backend has received in the current active
  session. A rising counter confirms receipt; an unchanged value does not prove
  that new packets are arriving. The active session disappears after Stop.
- **Live transcript**: disabled, unavailable, model ready, busy, text received,
  or session error. Model readiness does not prove recognition of your speech.
  A live-process error does not mean WAV storage has stopped.

If live transcription is disabled, starting backend/ngrok alone is not enough:
a separate local ASR process and models are required. See [LIVE_PREVIEW.md](LIVE_PREVIEW.md).
Automatic processing of completed WAVs is separate: [LOCAL_STT.md](LOCAL_STT.md).
The live card does not verify its readiness.

## Mac logs

In Terminal, enter the project directory. To watch connection events:

```bash
tail -n 40 -F .local/dev-harness/ngrok/logs/backend.log
```

`Ctrl+C` stops viewing the log, not the backend. Log entries include:

- `Local Mac profile check: status=200`: the key check succeeded;
  `401`: the key was rejected.
- `Local Mac listen: opened` / `accepted`: a WebSocket request / accepted connection.
- `first_binary_frame`: the first audio packet arrived; this is not an ASR result.
- `peer_closed`: the close code and total packet/byte counters;
  `server_closed`: the backend closed the connection.
- `Local preview: first_segment_sent`: the first live-text segment was sent.
- `Local preview ended`: update count, EOF acknowledgement, and any session error.

The tunnel and library have separate logs:

```bash
tail -n 40 -F .local/dev-harness/ngrok/logs/ngrok.log
tail -n 40 -F .local/dev-harness/ngrok/logs/library.log
```

ASR logs appear in the terminal running `serve_parakeet`. It emits technical JSON
events such as `model_ready`, `session_started`, and `session_finished`.
Check a prepared worker at `http://127.0.0.1:18090/health`.
If the worker is not running, no response is expected.

Check service and final-transcription queue status:

```bash
bash scripts/local-mac.sh status
bash scripts/local-mac.sh transcription-status
```

Do not publish `.env`, complete logs, or screenshots showing a revealed key.
Counters in old logs refer to earlier runs. For a fresh check, start viewing the
log, record 30–60 seconds with the CV1, open the recording card on the iPhone,
and verify new text. After Stop, check the WAV separately in the library.
