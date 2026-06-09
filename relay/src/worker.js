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

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method !== "POST") {
      return new Response("POST only", { status: 405 });
    }
    if (url.pathname !== "/" + env.HOOK_SECRET) {
      return new Response("not found", { status: 404 });
    }

    const body = await request.text();
    const r = await fetch(env.FIRE_URL, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + env.ROUTINE_TOKEN,
      },
      // JSON.stringify escapes the payload correctly no matter what
      // TradingView sends — the wrapping can't be malformed.
      body: JSON.stringify({ text: body }),
    });
    return new Response(await r.text(), { status: r.status });
  },
};
