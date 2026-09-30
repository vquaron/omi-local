# Tailscale connection

Connect the server and iPhone to the same Tailscale network. Omi still connects
to the iPhone over Bluetooth; the phone sends recordings to the server through
Tailscale. Keep the server awake and allow the backend port in your tailnet policy.

## Native Mac

Tailscale must be installed and connected. For a fresh setup, run
`./omiloc bootstrap`: it selects Tailscale, generates the app key in a private `.env`,
and discovers this Mac's Tailscale IPv4. To prepare settings without starting:

```bash
./omiloc bootstrap
```

For an existing ngrok installation, finish recording, stop the owned stack with
`./omiloc down`, and add this line to your existing private `.env`:

```dotenv
OMI_LOCAL_TRANSPORT=tailscale
```

Keep the existing `OMI_LOCAL_APP_KEY`. You can leave the ngrok fields saved for
later use; they are not needed or sent anywhere in Tailscale mode. If pairing
predates `.env`, `init-env` preserves its hash and leaves the key field empty;
the previously paired phone keeps working. Enter the existing key locally if
you want automatic phone configuration. Do not rotate it just to change transport.

Optional `OMI_TAILSCALE_IP` pins the server's Tailscale IPv4. The launcher verifies
that it belongs to the connected Mac; otherwise startup stops. Normally leave
it empty and let the launcher discover the address. Then run `./start.command`.

On the updated iPhone app, open **Local Mac**, enter the Tailscale IP and the app
key, and select **Check and connect**. A bare IP selects port **20000**. You can
also enter `IP:port` or `http://IP:port`. A full HTTP URL without a port means
standard port 80. Changing the address later does not require rebuilding the app.
The first update enabling Tailscale IP pairing does require installing the updated app.
Use **Saved servers** to choose a server, **Add** for a new entry, and the name,
address and key fields to edit it. **Save Credentials** saves the entry in Keychain;
**Check and connect** saves and verifies it before switching the active connection.
A failed check keeps the previous pairing. Existing pairing and draft settings are
imported into the list on first use. **Delete** removes only the selected saved entry;
it does not stop the active connection or delete recordings. Signing out clears
all saved server credentials. Unsaved form edits remain a separate Keychain draft.

The backend uses one process listening on localhost and its verified Tailscale IP.
Firebase, Redis, processing services and the web library remain on localhost.
No ngrok process, Tailscale Serve, Funnel, certificate or subnet router is needed.
HTTP and WebSocket traffic travel inside the Tailscale VPN; the app sends its key
only to the exact paired address and port and does not follow redirects.

The library remains at `http://127.0.0.1:20001/`. Native recordings, pairing and
provider settings retain their existing state directory when transports change.

## Docker

Use `./omiloc --runtime docker bootstrap --transport tailscale`, then
`./omiloc --runtime docker up` (or `dev`) with Tailscale connected on
the Docker host. Enter **IP:21000** on the phone and use the Docker volume's app
key. Docker keeps its own recordings, models and credentials; it does not import
the native Mac state. The library stays on host localhost port 21001.
See [Docker Tailscale configuration](DOCKER.md#iphone-and-tailscale) for direct
Compose commands and the precise port bindings.

## Checks and switching back

`./omiloc doctor` checks native prerequisites. After startup,
`./omiloc status` reports owned services. Check and connect on
the phone verifies the key; recording received audio is a separate check.
If connection fails, confirm Tailscale is connected on both devices, the server
is awake, the selected port is allowed, and the app key belongs to that runtime.

Finish recording before switching transport. For native ngrok, stop the stack,
set `OMI_LOCAL_TRANSPORT=ngrok` in `.env`, keep/fill its existing ngrok settings,
and run `./start.command`. Pair the phone to its HTTPS address. Switching never
automatically falls back to another transport. [Ngrok instructions](NGROK.md).

Tests cover settings preservation, key isolation, listener failures and Compose
port boundaries. Physical CV1/iPhone recording, interruption recovery and
clean-machine setup require separate device evidence; configuration alone does
not establish those results.
