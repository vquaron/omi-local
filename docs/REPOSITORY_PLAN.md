# Repository improvement plan

Scope: the standalone Omi Local snapshot, reviewed on 2026-10-27.
The target branch is `sky-uat`; upstream cloud services are outside this plan.

## Implemented surfaces

| Area | Change | Acceptance |
| --- | --- | --- |
| Startup | One `omiloc` entry point; explicit preparation, startup, status and diagnostics | Existing settings survive; startup does not download models silently |
| Phone setup | Private `.env.servers` presets, saved server cards and distinct connection errors | Keychain import is atomic; repeat imports preserve edits and deletions; keys never enter the build |
| Server selection | Verify before switching; separate server caches | Failed checks leave recording active; older cached data is retained |
| Recording | Flushed phone outbox, offline Start, route/foreground checkpoints and exact receipts | Network recovery keeps capture and received draft words; pending audio survives lost acknowledgements and retries create no duplicate WAV |
| Opus continuity | Version 2 parts with decoder pre-roll; old pending parts accepted | Decode context without duplicating fresh samples or duration |
| Processing | Independent Live, final transcription and diarization profiles | Authenticated settings with explicit activation and uncertain-write recovery |
| Security | Authenticated streaming ingress, bounded uploads/provider responses, storage reserve and secret scanning | Unauthorized requests are rejected before upload; low disk leaves audio queued |
| Organization | One standalone test inventory and focused setup, transport, processing and recovery guides | The same active checks are discoverable locally and in CI |

See [development checks](DEVELOPMENT.md), [phone setup](LOCAL_SETUP.md),
[delivery guarantees](RECORDING_DURABILITY.md) and [security](SECURITY.md).
Passing software checks does not establish physical-device endurance or deployment.

## Remaining acceptance, in order

1. Deploy the verified commit to the existing server with its original archive
   and model volumes. Save the previous images/configuration for rollback.
   Check an authenticated profile, exact synthetic audio storage, retry and
   transcript import; check the existing library without modifying recordings.
2. Build and attest the exact Profile app, install that artifact, and import
   presets only at launch. Verify saved server selection and connection on the
   physical iPhone, then CV1 and phone-microphone recording.
3. Exercise a long CV1 recording with network loss, Bluetooth reconnect,
   background/foreground transitions, app relaunch and restored connectivity.
   Compare stored duration and packet/sample counts, including part boundaries.
4. Rehearse setup on a clean Mac and restoration into separate storage. Verify
   a playable recording, transcript and linked conversation before declaring
   recovery successful.
5. Recheck the three remaining moderate Firebase CLI dependency findings on
   compatible updates. Keep the high/critical dependency gate and secret scan.

Server switching is a secondary action. Daily use should require selecting the
already saved server and recording, without editing credentials or rebuilding.
