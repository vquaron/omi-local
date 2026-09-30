# Connecting an iPhone to your Mac

For direct IP connection without ngrok, use [Tailscale](TAILSCALE.md).
Set `OMI_LOCAL_TRANSPORT=ngrok` to choose this optional transport.

Ngrok gives the Mac a persistent HTTPS address that the phone can reach,
including over a mobile network. Audio passes through ngrok and is saved on the Mac.
The web library opens separately and is accessible only on the Mac itself.

## Setup

### Noninteractive setup: `.env`

Run `bash scripts/local-mac.sh init-env` from the project root.
It creates a private `.env` with mode `600`, reuses saved ngrok settings when
available, and generates a separate app key. It does not overwrite an existing
`.env`. See [omiloc.env.example](../omiloc.env.example) for the fields.

- `OMI_NGROK_URL`: the HTTPS address from [Domains](https://dashboard.ngrok.com/domains).
- `NGROK_AUTHTOKEN`: the value from [Your Authtoken](https://dashboard.ngrok.com/get-started/your-authtoken), not an API Key.
- `OMI_LOCAL_APP_KEY`: the generated 43-character app key, not the ngrok token.

When switching from the old wizard, stop services with `bash scripts/local-mac.sh down`.
Then run `./start.command`: a complete `.env` avoids prompts and secret output.
If settings are missing, fill in the empty fields in a text editor.
The file is read as data, not executed as shell code; substitutions and extra
fields are unsupported. Changed settings apply only while the stack is stopped;
recordings are preserved.

After installing the updated app, connect the unlocked iPhone and run
`./start.command --iphone-configure`. This relaunches the app with the address
and key from `.env`; it saves them in Keychain and displays them on **Local Mac**.
Tap **Check and connect** to verify the server. The authtoken stays on the Mac;
the app key is neither embedded in the binary nor passed in command arguments.
For Wi-Fi configuration, the iPhone must already be paired with the Mac and
reachable on the same network. The script checks the live connection even if the
device list shows a stale status. Keep the phone unlocked and its screen on until launch.

`.env` contains real secrets: do not commit it, send it in chat, or publish build
logs containing its environment. The backend still stores only a verification
hash of the key; the plaintext key is also stored in your private `.env`.

### Interactive wizard without `.env`

1. [Create an ngrok account](https://dashboard.ngrok.com/signup) and complete any
   account-verification prompts.
2. Get the account's **dev domain** from [Domains](https://dashboard.ngrok.com/domains).
   You do not need to purchase a domain: the free plan includes one such address.
   Use that assigned address; an arbitrary name will not work on the free plan.
3. Open [Your Authtoken](https://dashboard.ngrok.com/get-started/your-authtoken)
   and copy only the token value, without the command. An API Key is not used here.
4. Run `start.command`: it installs ngrok and asks for an HTTPS address such as
   `https://your-domain.ngrok-free.app` and an authtoken. Use the domain and token
   from the same account. Token input is hidden; enter it on a new Mac.
5. On the iPhone, open **Local Mac**, enter the domain and key from the boxed
   Terminal output, and check the connection.

The script creates the app key separately from the ngrok authtoken and shows it
once. Subsequent launches preserve the key and recordings.
On **Local Mac**, the eye icon reveals the key inline. Tap it again to hide the
key; moving the app to the background also hides it.
The save button stores the address and key in the iPhone's Keychain without a
network check. Field edits are also saved automatically. Pasted spaces and line
breaks are removed from the key; the counter shows its length against the required
43 characters. Connection checks preserve your input even if the server is unavailable.
Saving does not switch the active server; only a successful connection check does.
Do not share your app key or authtoken.
The free plan has [traffic and request limits](https://ngrok.com/docs/pricing-limits/free-plan-limits);
reaching them may stop transfers until the limit resets or the plan changes.

### Supplying the address and token through `.env`

Instead of entering values manually during initial setup, create `.env` beside
`start.command` from the template. This command preserves an existing file:

```bash
(umask 077; cp -n .env.example .env)
chmod 600 .env
```

Set `NGROK_URL` to your full address, including `https://`, and set
`NGROK_AUTHTOKEN` to the token from the same account. Run `./start.command` in
Terminal to supply both values automatically. Obtain the token from the dashboard
once; `.env` itself does not request it from ngrok. Empty fields are prompted for.
The file is read as data, without shell execution or `${...}` substitution.

`.env` is excluded from Git, and the token is not printed in Terminal. The iPhone
key is still created separately and displayed once. An existing configured
connection is reused without rereading `.env`. To apply changed values, follow
**Changing settings** below.

To test recording, connect the CV1, press its button once, say a few phrases,
and press it again. Open `omiloc` on the Mac and play the file. Mute does not finish recording.

## When the connection fails

The app shows separate states for the Mac, received audio, and live transcription.
See [what they mean and where to find logs](CONNECTION_DIAGNOSTICS.md).

- Ensure the Mac is on, awake, and running services started by `start.command`.
- Check the domain and key in **Local Mac** on the iPhone.
- Open `https://<your-address>/v1/health`: `{"status":"ok"}` means the server
  is reachable. The app button verifies the key separately.
- Run `bash scripts/local-mac.sh status` from the project directory to check services.
  For more detail, run `bash scripts/local-mac.sh check`.

## Changing settings

Stop services first. Run all commands from the project directory:

```bash
bash scripts/local-mac.sh down
bash scripts/local-mac.sh edit-connection
./start.command
```

`edit-connection` changes the address or authtoken while preserving the app key.
After an address change, update it on the iPhone; you do not need to reinstall
the app. To replace a lost key, use `rotate-key` instead of `edit-connection`
and enter the new key on the phone.

For manual installation and startup, run `bash scripts/local-mac.sh install`,
then `configure` and `up`. For daily use, [start.command](START.md) is sufficient.

[Transcribing recordings](LOCAL_STT.md) · [App installation](LOCAL_SETUP.md) ·
[Technical checks](DEVELOPMENT.md).
