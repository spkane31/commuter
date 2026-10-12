interface Env {
  STRAVA_VERIFY_TOKEN: string;
  commuter: Queue;
}

interface StravaEvent {
  aspect_type: "create" | "update" | "delete";
  event_time: number;
  object_id: number;
  object_type: "activity" | "athlete";
  owner_id: number;
  subscription_id: number;
  updates?: Record<string, string | boolean>;
}

const MAX_EVENT_BYTES = 32 * 1024;

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname !== "/strava") {
      return new Response("Not found", { status: 404 });
    }

    if (request.method === "GET") {
      return verifySubscription(url, env);
    }
    if (request.method !== "POST") {
      return new Response("Method not allowed", {
        status: 405,
        headers: { Allow: "GET, POST" },
      });
    }

    try {
      const rawBody = await readBoundedBody(request, MAX_EVENT_BYTES);
      const event = parseEvent(rawBody);

      await env.commuter.send(JSON.stringify(event), { contentType: "text" });

      return new Response("EVENT_RECEIVED", { status: 200 });
    } catch (error) {
      // Do not acknowledge: Strava will retry transient failures.
      console.error("Unable to enqueue Strava event", safeErrorDetails(error));
      return new Response("Unable to enqueue event", { status: 503 });
    }
  },
};

function verifySubscription(url: URL, env: Env): Response {
  const mode = url.searchParams.get("hub.mode");
  const token = url.searchParams.get("hub.verify_token");
  const challenge = url.searchParams.get("hub.challenge");

  if (mode !== "subscribe" || challenge === null || token === null) {
    return new Response("Invalid subscription challenge", { status: 400 });
  }
  if (!constantTimeEqual(token, env.STRAVA_VERIFY_TOKEN)) {
    return new Response("Invalid verification token", { status: 403 });
  }
  return Response.json({ "hub.challenge": challenge });
}

async function readBoundedBody(request: Request, maxBytes: number): Promise<string> {
  const declaredLength = Number(request.headers.get("content-length") ?? 0);
  if (declaredLength > maxBytes) throw new Error("event_too_large");
  const body = await request.arrayBuffer();
  if (body.byteLength === 0 || body.byteLength > maxBytes) {
    throw new Error("event_size_invalid");
  }
  return new TextDecoder("utf-8", { fatal: true }).decode(body);
}

function parseEvent(rawBody: string): StravaEvent {
  const value: unknown = JSON.parse(rawBody);
  if (!value || typeof value !== "object") throw new Error("invalid_event");
  const event = value as Partial<StravaEvent>;
  if (
    !["activity", "athlete"].includes(String(event.object_type)) ||
    !["create", "update", "delete"].includes(String(event.aspect_type)) ||
    !isPositiveInteger(event.object_id) ||
    !isPositiveInteger(event.owner_id) ||
    !isPositiveInteger(event.subscription_id) ||
    !isPositiveInteger(event.event_time)
  ) {
    throw new Error("invalid_event");
  }
  return event as StravaEvent;
}

function safeErrorDetails(error: unknown): { name: string; message: string } {
  if (error instanceof Error) {
    return { name: error.name, message: error.message.slice(0, 500) };
  }
  return { name: "UnknownError", message: String(error).slice(0, 500) };
}

function isPositiveInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0;
}

function constantTimeEqual(left: string, right: string): boolean {
  const encoder = new TextEncoder();
  const a = encoder.encode(left);
  const b = encoder.encode(right);
  let difference = a.length ^ b.length;
  const length = Math.max(a.length, b.length);
  for (let index = 0; index < length; index += 1) {
    difference |= (a[index] ?? 0) ^ (b[index] ?? 0);
  }
  return difference === 0;
}
