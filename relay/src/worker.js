// tv-relay — TradingView → Claude Code routine forwarder.
//
// TradingView webhooks cannot send custom headers, so this Worker sits in
// the middle: it receives the raw alert JSON, wraps it as {"text": <body>},
// attaches the routine's bearer token, and forwards to the /fire endpoint.
//
// Runtime variables (Settings → Variables and Secrets):
//   FIRE_URL      (plaintext) routine /fire endpoint URL
//   ROUTINE_TOKEN (SECRET)    sk-ant-oat01-... token, no "Bearer " prefix
//   HOOK_SECRET   (plaintext) random string; the URL path TradingView must hit

async function fireRoutine(env, text) {
  return fetch(env.FIRE_URL, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Authorization": "Bearer " + env.ROUTINE_TOKEN,
      "anthropic-version": "2023-06-01",
    },
    body: JSON.stringify({ text }),
  });
}

export default {
  // Cron trigger (Settings -> Triggers): wakes the routine in SCOUT mode
  // premarket to hunt off-watchlist catalyst names. No trading happens in
  // scout mode — it journals a morning-candidates report.
  async scheduled(event, env, ctx) {
    ctx.waitUntil(fireRoutine(env, JSON.stringify({ mode: "scout" })));
  },

  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method !== "POST") {
      return new Response("POST only", { status: 405 });
    }
    if (url.pathname !== "/" + env.HOOK_SECRET) {
      return new Response("not found", { status: 404 });
    }

    // JSON.stringify in fireRoutine escapes the payload correctly no
    // matter what TradingView sends — the wrapping can't be malformed.
    const r = await fireRoutine(env, await request.text());
    return new Response(await r.text(), { status: r.status });
  },
};
