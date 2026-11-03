// Talking to the server and handing the result to iOS. Every URL here is a
// same-origin path, so connect-src 'self' covers the fetches and the CSP needs
// no blob: or other source.
import { errorMessage, fileNameFromDisposition } from "./audio/request.js";

const RESULT_ID_RE = /^[0-9a-f]{32}$/;

// Failures that happen before the server answers, or after it answered with
// something this page did not expect; the server's own codes go through
// errorMessage().
const NETWORK_MESSAGE = "The server could not be reached. Check the connection and try again.";
const BAD_RESPONSE_MESSAGE = "The server sent an answer this page does not understand. Reload the page and try again.";

export class ShareError extends Error {
  constructor(message, status = 0, code = "") {
    super(message);
    this.name = "ShareError";
    this.status = status;
    this.code = code;
  }
}

export function resultUrl(resultId, extension) {
  // The ID came from the server, but it still goes into a URL: anything but
  // the documented 32 hex digits is refused rather than built into a path.
  if (!RESULT_ID_RE.test(resultId)) throw new ShareError(BAD_RESPONSE_MESSAGE);
  return "/api/result/" + resultId + "." + extension;
}

async function readError(response) {
  let code = "";
  let detail = "";
  try {
    const payload = await response.json();
    if (payload !== null && typeof payload === "object") {
      if (typeof payload.error === "string") code = payload.error;
      if (typeof payload.detail === "string") detail = payload.detail;
    }
  } catch {
    // Not JSON (a proxy's error page, say): the status alone has to do.
  }
  return new ShareError(errorMessage(response.status, code, detail), response.status, code);
}

/**
 * POST the WAV to /api/render and return its JSON. The body is a Uint8Array,
 * so the browser sends a Content-Length and never chunked encoding, which the
 * server refuses. The browser adds the Origin header the server checks.
 */
export async function render(wavBytes, query, header) {
  const headers = { "Content-Type": "audio/wav" };
  if (header !== null) headers["X-Tone-Name"] = header;
  let response;
  try {
    response = await fetch("/api/render?" + query, {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers,
      body: wavBytes,
    });
  } catch {
    throw new ShareError(NETWORK_MESSAGE);
  }
  if (!response.ok) throw await readError(response);
  let payload;
  try {
    payload = await response.json();
  } catch {
    throw new ShareError(BAD_RESPONSE_MESSAGE);
  }
  const valid = payload !== null && typeof payload === "object"
    && typeof payload.result_id === "string" && RESULT_ID_RE.test(payload.result_id)
    && ["duration", "measured_lufs", "applied_gain_db"].every((key) => Number.isFinite(payload[key]));
  if (!valid) throw new ShareError(BAD_RESPONSE_MESSAGE);
  return payload;
}

/**
 * Fetch the rendered .m4a once and keep its bytes: they are both what "Hear
 * final" decodes and plays and what the share sheet gets. Fetching instead of
 * an <audio> element matters because Safari's media loader sends Range and
 * HEAD requests, which the result route does not support. The name is the
 * server's sanitised one from Content-Disposition, or null.
 */
export async function fetchResult(resultId) {
  let response;
  try {
    response = await fetch(resultUrl(resultId, "m4a"), { credentials: "same-origin", cache: "no-store" });
  } catch (error) {
    if (error instanceof ShareError) throw error;
    throw new ShareError(NETWORK_MESSAGE);
  }
  if (!response.ok) throw await readError(response);
  const bytes = new Uint8Array(await response.arrayBuffer());
  const name = fileNameFromDisposition(response.headers.get("Content-Disposition"));
  return { bytes, name };
}

/** Whether this browser can put this exact file on the share sheet. */
export function canShareFile(file) {
  if (typeof navigator.share !== "function" || typeof navigator.canShare !== "function") return false;
  try {
    return navigator.canShare({ files: [file] });
  } catch {
    return false;
  }
}

/**
 * Open the share sheet. Call it straight from the click handler with no await
 * before it: WebKit grants navigator.share only during the click's user
 * activation, which an awaited fetch or decode would use up.
 */
export function shareFile(file) {
  return navigator.share({ files: [file] });
}

/** The user closed the share sheet; that is not an error worth showing. */
export function isCancel(error) {
  return error !== null && typeof error === "object" && error.name === "AbortError";
}
