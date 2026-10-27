# Current limitations

| Feature | Current behavior |
|---|---|
| CV1 recording | Starts on connection unless previously stopped/muted. A CV1 press or the home controls start or finish a session. |
| Mute | Temporarily silences audio within a recording. |
| iPhone microphone | Starts through the original **+** button. Switching inputs finishes the previous recording while preserving the Omi Bluetooth connection. |
| Text during conversation | Experimental [live preview](LIVE_PREVIEW.md), with a separate Mac process. Tap the recording card to open its screen. Draft quality is still being evaluated. |
| Speaker separation | Available through configured live or final diarization. Speaker numbers do not establish a person's identity. |
| Delivery after a temporary server interruption | Updated apps retain received packets on the phone and retry acknowledged part uploads. Initial capture after app restart requires an authenticated updated-server check. See [delivery boundaries](RECORDING_DURABILITY.md). |
| Online Omi features | Disabled in local mode. |

Press the CV1 button once to start recording and again to finish.
A single press finishes the session even when muted. In the iPhone app,
the card button temporarily mutes and unmutes audio.
The browser supports browsing, playback, and deletion.

CV1 recordings can be quiet. Switching sources sometimes creates short additional
files. Physical interruption recovery, continuous BLE capture through iOS
suspension/termination and background upload availability remain unverified.

[Startup and usage](START.md) · [Transcription setup](LOCAL_STT.md).
