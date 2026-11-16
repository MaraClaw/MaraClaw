# HTTPS tunnels for local Telegram testing

Telegram's hosted Bot API requires a public HTTPS webhook URL. The engine can
continue serving HTTP locally: a tunnel accepts public HTTPS requests and forwards
them to the engine's HTTP port.

## Open-source options

| Project | Hosting | Use case |
|---|---|---|
| [cloudflared](https://github.com/cloudflare/cloudflared) | Open-source client using Cloudflare's hosted relay | Quick testing with a temporary HTTPS URL; Quick Tunnels need no account or domain |
| [localtunnel](https://github.com/localtunnel/localtunnel) | Open-source client and [server](https://github.com/localtunnel/server), with a hosted relay available | Simple Node.js option; self-hosting requires DNS and HTTPS termination |
| [frp](https://github.com/fatedier/frp) | Open-source client and server for self-hosting | Suitable when you own a public server and can configure a domain and HTTPS certificates |

Cloudflare Quick Tunnels are the recommended starting point for this test. The
client is open source; Cloudflare's hosted relay is a managed service.

## Test with a Cloudflare Quick Tunnel

1. Start the engine with its HTTP port published on the host at port 8000.
2. Install `cloudflared` using the project's installation instructions, then run
   this command on the host:

   ```bash
   cloudflared tunnel --url http://localhost:8000
   ```

3. Copy the public HTTPS address printed by `cloudflared`. Set `PUBLIC_BASE_URL`
   in `engine/.env` to that address. Use the base address only, without a webhook
   path. The following is an example, not an active tunnel address:

   ```dotenv
   PUBLIC_BASE_URL=https://example-tunnel.trycloudflare.com
   ```

4. Recreate the engine container with the updated environment. From `engine/`,
   use the existing launcher:

   ```bash
   ./start-from-docker.sh
   ```

   Restarting a container without recreating it does not load changed environment
   variables from `.env`.

5. In `web-e`, open the agent's Channels tab, enter the BotFather bot token in
   Telegram's **Bot token** field, and click **Save**. Engine generates and stores
   the webhook secret and registers its callback URL with Telegram automatically.
6. Send the bot a private message and follow the account-linking instructions
   shown by MaraClaw if the sender is not linked yet.

Keep the tunnel process running while testing. Recreating a Quick Tunnel changes
its public address. Update `PUBLIC_BASE_URL`, recreate the engine container, and
save the Telegram configuration again to register the new callback address.

The public callback must accept Telegram's requests without an interactive login,
email PIN, or browser challenge. A tunnel makes the forwarded engine port publicly
reachable; stop it when testing is finished.

## Alternative quick start

With Node.js available, localtunnel's documented client command is:

```bash
npx localtunnel --port 8000
```

Use the resulting HTTPS address in the same `PUBLIC_BASE_URL` workflow. Verify
that the relay forwards webhook POST requests without an interactive access page.
For fully self-hosted infrastructure, use frp or localtunnel's server and configure
public HTTPS termination yourself.

## Troubleshooting

- **Save returns HTTP 503:** Engine requires a configured public HTTPS
  `PUBLIC_BASE_URL`. An empty value, localhost address, private IP address, or HTTP
  URL is rejected. The UI may show a generic credentials error for this deployment
  problem.
- **Telegram rejects registration:** Check that the tunnel is running, its address
  is current, the callback is reachable, and the BotFather token is valid.
- **Testing without a public URL:** The current Telegram integration uses webhooks.
  Localhost-only testing would require a separate long-polling implementation.

See [channel documentation](channels.md) for the Telegram integration contract and
[Cloudflare's Quick Tunnel instructions](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)
for tunnel setup and limitations. The commands above are setup guidance; no live
Telegram registration was performed when writing this document.
