# tv-relay — TradingView → routine webhook forwarder

TradingView webhooks can't send custom headers, but the routine's `/fire`
endpoint needs an `Authorization: Bearer` token. This Cloudflare Worker sits
in between:

```
TradingView alert ──POST raw JSON──▶ tv-relay ──POST {"text": <json>} + auth──▶ routine /fire
```

`JSON.stringify` does the wrapping, so escaping is correct by construction —
no Zapier field-mapping to get wrong.

## Deploy (Git-connected)

1. Cloudflare dashboard → Workers & Pages → the `tv-relay` Worker →
   **Settings → Build → Connect** (GitHub) → pick this repo.
2. Set **Root directory** to `relay/` and choose the production branch.
3. Every push to that branch auto-builds and deploys via `wrangler deploy`.

## Runtime variables (dashboard → Settings → Variables and Secrets)

| Name | Type | Value |
|---|---|---|
| `FIRE_URL` | Plaintext | the routine's `/fire` endpoint URL |
| `ROUTINE_TOKEN` | **Secret** | `sk-ant-oat01-...` token only — no `Bearer ` prefix |
| `HOOK_SECRET` | Plaintext | random string; becomes the URL path |

`keep_vars = true` in `wrangler.toml` prevents Git deploys from wiping these.
**Never commit the token.**

## TradingView webhook URL

```
https://tv-relay.<account>.workers.dev/<HOOK_SECRET>
```

## Test (PowerShell)

```powershell
Invoke-RestMethod -Uri 'https://tv-relay.<account>.workers.dev/<HOOK_SECRET>' `
  -Method Post -ContentType 'application/json' `
  -Body '{"ticker":"TEST","setup":"ORB","price":10.0,"stop":9.5,"target":11.25}'
```

Expected: the routine fires and its validator BLOCKs the fake `TEST` ticker —
that block is the success signal. `404` → path doesn't match `HOOK_SECRET`;
`401` passed through → token value is wrong.
