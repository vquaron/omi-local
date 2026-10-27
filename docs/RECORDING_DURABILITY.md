# Recording delivery and recovery

The updated local iPhone application keeps a private audio outbox on the phone.
For CV1 Opus and phone-microphone PCM16, each packet delivered to Flutter is
appended and flushed to a local journal before it is sent to live transcription.
A disconnected live socket does not discard this journal.
During startup, a bounded raw BLE buffer receives packets while codec
validation completes. Those packets enter the journal after validation,
before opening live preview. Termination during this initial validation can lose
the RAM-only startup buffer; it is not a durable recording yet.

The phone seals an immutable part after at most five minutes of audio or when
you stop recording. Parts preserve their recording identity, source, start time
and order. PCM duration comes from sample count; Opus duration comes from its
packet TOC and frame count ([RFC 6716](https://www.rfc-editor.org/info/rfc6716/)).
Network bursts and changes to the phone clock do not extend the audio limit.
The server library receives one recording per part, as in the existing
five-minute capture workflow. Live preview remains available while recording;
archiving a part is a separate operation.

When the server cannot be reached, live transcription pauses while the phone
journal continues. Received draft words remain visible through socket changes
within the same recording, with new draft timestamps offset by captured audio
time. Corrections and retractions affect only their originating draft. Starting
a new recording clears this transient text. After reconnection, the server
processes the uploaded prefix into a saved recording and final transcript,
including audio captured during the outage. Live drafts themselves are RAM-only;
the persisted audio is the recovery source after an app restart.

Opus parts carry a bounded suffix of at least 80 ms of preceding packets, as
recommended for independent decoding in [RFC 7845 section 4.6](https://www.rfc-editor.org/rfc/rfc7845.html#section-4.6).
The server decodes this context first and discards its samples before writing
the new WAV. Context does not extend the five-minute audio limit, duplicate
playback samples, or inflate recording duration. It counts toward storage and
request limits; the phone retains at most 32 context packets. PCM has no context.

## What the status means

The Local Mac card includes the number and size of parts retained on the phone.
Its header names the authenticated saved server (for example, `faeon`), not the
currently edited draft. During a network outage it shows recording on the phone,
offline state and pending parts. Expanding details preserves that summary.
Authentication or local storage failure remains an explicit error; unavailable
live recognition does not mean that the phone journal stopped.
**Pending** means phone storage, including the current recording part.
**Syncing** means delivery is in progress. **Sync failed** leaves the original
phone audio available for another attempt. Refresh retries delivery.
Parts disappear from the phone outbox only after the server acknowledges the
same part identity and SHA-256 after saving its WAV and metadata.

The live recording screen provides **Stop** even before any transcript appears
and while input is interrupted. It closes the selected input and seals its phone
journal without waiting for the server. Its timer shows flushed audio sample
time, including offline audio; server reachability and speech recognition do not
drive that timer. A local **Process Now** action seals the current part for
delivery while capture can continue; it does not invoke the cloud conversation
workflow or clear an active live draft.

Delivery retries every fifteen seconds while the application can execute, and
on pairing changes and application startup. Restored server reachability, a
successful Wi-Fi/cellular route change, and foreground return seal the current
flushed prefix for immediate delivery; they do not wait for five minutes or stop
the input. Subsequent packets continue in the next part of the same recording.
An upload already in progress picks up newly sealed parts in its next pass,
without waiting for another retry tick. Ordinary health polls do not seal parts
or replace healthy sockets. Stopping recording seals the part without waiting
for the network. Quota or filesystem failure stops acquisition
and shows a storage error; pending audio is retained. The default phone outbox
limit is 512 MiB, with at most 4 MiB of packets awaiting local writes.

## Server and app updates

An existing saved pairing is enough to start journaling without a network,
including after restarting the app. Phone-microphone capture begins before
waiting for server capability or the live socket; opening words are journaled
while those requests run. This does not automatically select an unpaired preset.
The app verifies the server's authenticated delivery capability before sending
version-two parts or entering the preview-only protocol. An old server produces
an update-required message and receives neither protocol. Pairing and pending
audio remain on the phone until a compatible server can acknowledge them.
Network socket loss changes transcription readiness, not microphone capture;
reconnecting does not stop or restart the recording input.
This also applies to a failed live-translation control message. The phone retains
its audio and resends the saved translation language pair on the next preview.
Turning Bluetooth off clears the cached device link even if no separate device
disconnect event arrives. Turning it back on retries previously connected devices
that were not manually disconnected, then restores notification subscriptions.
The phone journal covers audio already received by the phone. Audio spoken while
Bluetooth is absent requires recovery from the wearable's own storage; a connected
status alone does not prove that this interval was recovered.
Older apps continue to use the original server-side WAV capture path.

New protected recordings use journal protocol version 2 and require authenticated
server capability 2. Version 1 servers cannot receive these parts: the phone checks
capability before sending audio. The updated server still accepts version 1 parts
without pre-roll; recovery and retry preserve their original identity and checksum.

Each retained part is bound to a hash of its original paired origin and app key. It is never
sent to a newly selected server. Returning to that server permits retry; a part
for another server does not block parts belonging to the selected server.
The journal contains no access key. Credentials remain in Keychain.
Changing the app key also changes this binding: old pending parts stay on the
phone and require deliberate recovery using their original pairing. They are
never silently transferred to a replacement server using new credentials.

The paired-only `POST /v1/local/audio/parts` route authenticates before reading
audio, limits requests to 16 MiB, admits at most two simultaneous bodies, and
rejects unsupported source/codec combinations. It stages WAV and metadata
outside the library watch tree, then atomically publishes and syncs the complete
directory before acknowledging. A repeated identical part returns the same
receipt. Reusing its identity with different bytes returns conflict and cannot
overwrite the original. Interrupted staging never appears as a completed WAV.

## Recovery boundaries

On application startup, a valid unfinished journal is sealed for retry. An
incomplete final packet is trimmed; all complete packet records remain. An
invalid header or packet length leaves the original journal untouched for
investigation. Never delete pending files to resolve a connection error.

The guarantee starts with packets delivered to Flutter and successfully flushed
to phone storage. It does not prove uninterrupted BLE capture while iOS suspends
or terminates the app, physical device endurance, or a working background upload
budget. A process or power failure during a local write can leave a partial last
packet. Hardware testing must measure these boundaries separately.

The delivery tests cover persisted-before-send ordering, offline Start after
pairing restore, route and foreground recovery, checkpoints during an in-flight
upload, microphone continuity across socket failure, retained draft words,
lost acknowledgements, restart replay, incomplete journals, quota bounds,
checksum conflicts, request authentication/limits, and the legacy client capture path. Physical iPhone/CV1
recording and sustained interruption recovery still require fresh evidence.
