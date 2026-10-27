# Local security and recovery

The paired runtime is a single-owner audio library. Keep its app key, provider
keys, `.env`, `.env.docker`, signing files, logs and recordings out of Git.
Provider keys belong to the server; the phone receives only the paired app key.

## Network boundary

Use the owned launchers and Tailscale or an authenticated ngrok tunnel. Publish
only the paired backend ingress. Firebase, Redis, provider relays and STT workers
stay private. The library stays on loopback unless its documented authenticated
Tailscale Serve access is explicitly configured. Never expose emulator ports.

HTTP and WebSocket credentials are confined to the exact paired origin, with
redirects disabled. Docker's API ingress streams request bodies to that boundary
instead of buffering them first, with a 16 MiB request cap. The public health
endpoint proves reachability only. Check
an authenticated profile, then actual audio storage and transcription separately.
See [connection diagnostics](CONNECTION_DIAGNOSTICS.md).

Provider POST responses are streamed with byte limits and elapsed-time checks;
unexpected compressed responses and redirects are rejected. The time budget is
checked between blocks, with each network operation separately timed out. It is
not a precise wall-clock cancellation guarantee for synchronous I/O.

New durable audio uploads require a 128 MiB free-space reserve plus bounded
PCM/WAV staging space. Insufficient capacity returns HTTP 507 and leaves the
phone part queued. An already stored identical part can still be acknowledged
without spending that reserve. Storage errors do not trigger archive deletion.

## Dependencies

Use Node 22 for the pinned Firebase CLI. `npm ci --ignore-scripts --omit=optional`
installs the lockfile without dependency lifecycle scripts. CI runs the same
installation and rejects high or critical findings with:

```bash
npm audit --package-lock-only --ignore-scripts --audit-level=high
```

The 2026-10-27 update removed unused Expo, ioredis and dotenv-cli dependencies,
updated Firebase CLI to 15.34.1, and refreshed compatible transitive patches.
The resulting audit has no high or critical findings. Three moderate findings
remain in the Firebase CLI → Pub/Sub → OpenTelemetry Core dependency chain
([GHSA-8988-4f7v-96qf](https://github.com/advisories/GHSA-8988-4f7v-96qf)).
The CLI dependency currently requests OpenTelemetry Core 1.x; forcing the fixed
2.x/3.x line or downgrading Firebase CLI is not a verified compatible repair.
The paired local runtime does not enable the cloud Pub/Sub workflow. This
limits the relevant surface; it is not evidence that every advisory is harmless.
Recheck the lockfile after updates. Do not use `npm audit fix --force` as a
substitute for a tested dependency change.

## Archive recovery

Finish capture and wait for pending phone uploads before stopping the owned
server. Preserve the complete native state or Docker state volume: WAVs,
transcripts, job/import receipts, provider registry and secrets, pairing and
Firebase export. Preserve the separate model volume if offline recovery needs
its cached models. Never copy a running emulator's files as a consistent backup.

Restore into a separate runtime and storage location first. Verify private file
permissions, the expected owner, a playable WAV, its transcript, and the linked
conversation before considering the recovery successful. Never run
`docker compose down -v` against the archive. Docker storage and backup guidance is in
[Docker](DOCKER.md#data-network-and-diagnostics).

A backup or restore is not claimed complete merely because a copy command or a
health check succeeded. Clean-machine restoration and physical phone endurance
still require their own recorded evidence.
