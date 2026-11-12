// Fetching a clip from a YouTube link (docs/PLAN.md 6, 6.2 and 7.4). The link
// is parsed on this device and only its 11-character video ID is sent: the
// fetcher builds the YouTube address itself, so nothing a user typed or
// shared ever reaches it. A video longer than the server fetches whole comes
// back as "long", with its length; the second request then carries the ID
// and one whole number, the second to start at, and brings a window of the
// audio from there. Every URL here is a same-origin /fetch/ path, which
// the deployment's proxy forwards to the fetcher, so connect-src 'self'
// covers them and the CSP stays exactly as it is.

import { MAX_INPUT_BYTES } from "./audio/decode.js";

/** A YouTube video ID, as the fetcher accepts it. */
export const VIDEO_ID_RE = /^[A-Za-z0-9_-]{11}$/;
// The fetcher's job IDs: 128 random bits as lower-case hex. Checked before an
// ID from the server is built into a path, as share.js does for results.
const JOB_ID_RE = /^[0-9a-f]{32}$/;

/** The latest second a window may start at: 99:59:59, as the fetcher's MAX_START_SECONDS. */
export const MAX_START_SECONDS = 359999;

/** How often the job's status is asked for. */
export const POLL_INTERVAL_MS = 2000;
/**
 * How many times before giving up: 15 minutes at 2 s, which covers a full
 * queue (4 waiting jobs and the running one, each bounded by the fetcher's
 * 120 s timeout) with room to spare.
 */
export const MAX_POLLS = 450;

// The hosts a shared YouTube link comes from. Exact names only: a look-alike
// such as youtube.com.example.internal is not YouTube.
const HOSTS = new Set(["youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"]);
const SHORT_HOST = "youtu.be";
// Every host a link is accepted from, read-only, so a test pins the exact list
// and a host added by mistake fails it.
export const LINK_HOSTS = Object.freeze([...HOSTS, SHORT_HOST]);
// /shorts/<id>, /live/<id>, /embed/<id> and /v/<id>.
const ID_PATHS = new Set(["shorts", "live", "embed", "v"]);
// Only text that starts like a web address is parsed as one.
const WEB_ADDRESS_RE = /^https?:\/\//i;
// Longer than any link a share sheet produces; a page's worth of pasted text
// is not searched for one.
const MAX_LINK_TEXT = 2048;
// t= and start= as YouTube writes them: 90, 90s, 1m30s, 1h2m3s. A fraction
// of a second is allowed and dropped. The digit counts keep the number small.
const TIMESTAMP_RE = /^(?:(\d{1,3})h)?(?:(\d{1,5})m)?(?:(\d{1,6})(?:\.\d{0,6})?s?)?$/i;

// A time as a person types it into the start field: up to three numbers with
// ':', '.', ',' or spaces between them. The iPhone's number pad has a decimal
// point and no colon, so a point separates the parts as a colon does.
const START_PARTS_RE = /^\d{1,6}(?:[:.,\s]+\d{1,6}){0,2}$/;

const STATUSES = new Set(["queued", "running", "done", "failed", "long"]);
// The fetcher's fixed job errors (docs/PLAN.md 6 and 6.2).
const JOB_ERRORS = new Set(["blocked", "live", "not_found", "past_end", "timeout", "failed"]);
const MAX_TITLE_CHARACTERS = 200;

// Plain words for every code the fetcher answers with, and for the few this
// module adds itself (bad_link, bad_time, network, bad_response, unavailable,
// expired, gave_up, cancelled, audio_too_large). tests/test_frontend_js.py
// fails when the fetcher gains a code that is missing here.
const MESSAGES = {
  // The job's own outcome.
  blocked: "YouTube would not hand this video to the server. Try again later, or save the audio another way and pick the file.",
  live: "This is a live stream. The server can fetch it once the stream has ended.",
  not_found: "This video could not be found. It may be private, removed or not available where the server is.",
  past_end: "That start time is at or after the end of the video. Choose an earlier one.",
  timeout: "Fetching this video took too long. Try again in a moment.",
  failed: "The server could not fetch this video's audio. Try again, or try another video.",
  // Refusals of the request itself.
  rate_limited: "Too many fetches in the last minute. Wait a moment, then try again.",
  busy: "The server is busy with other fetches. Try again in a moment.",
  bad_video_id: "The server did not accept this video's ID. Check the link and try again.",
  bad_start: "The server did not accept the start time. Enter it as minutes:seconds, for example 12:30, and try again.",
  bad_json: "The fetch request was refused. Reload the page and try again.",
  bad_origin: "The server refused this page's address. Open the app at the address it was set up for.",
  too_large: "The fetch request was larger than the server accepts. Reload the page and try again.",
  unsupported_media_type: "The fetch request was sent in a form the server does not accept. Reload the page and try again.",
  transfer_encoding: "The fetch request was sent in a form the server does not accept. Reload the page and try again.",
  bad_length: "The fetch request's size was not stated correctly. Reload the page and try again.",
  length_required: "The fetch request's size was missing. Reload the page and try again.",
  body_timeout: "The fetch request took too long to arrive. Check the connection and try again.",
  incomplete_body: "The fetch request was cut off. Check the connection and try again.",
  method_not_allowed: "The server refused the request. Reload the page and try again.",
  internal_error: "Something went wrong on the server. Try again.",
  // This page's own.
  bad_link: "That doesn't look like a link to a YouTube video. Copy the link from YouTube's Share button and paste it here.",
  bad_time: "That doesn't look like a time. Enter minutes:seconds or hours:minutes:seconds, for example 12:30 or 1:23:45.",
  network: "The server could not be reached. Check the connection and try again.",
  bad_response: "The server sent an answer this page does not understand. Reload the page and try again.",
  unavailable: "YouTube fetching is not available on this server right now.",
  expired: "The fetched audio is no longer on the server. Fetch the video again.",
  gave_up: "The fetch is taking far longer than it should. Try again later.",
  cancelled: "The fetch was replaced by a newer one.",
  // The page's own byte cap (decode.js), which FETCH_MAX_BYTES may exceed.
  audio_too_large: `This video's audio is larger than the ${MAX_INPUT_BYTES / (1024 * 1024)} MB this page can open. `
    + "Pick a shorter video, or save the audio another way and pick the file.",
};
const GENERIC_MESSAGE = "Something went wrong while fetching the video. Try again.";
const HTTP_CODE_RE = /^http_(\d{3})$/;

/**
 * Plain words for a fetch error code.
 * @param {string} code a fetcher code, one of this module's, or http_<status>
 * @returns {string}
 */
export function fetchErrorMessage(code) {
  if (typeof code === "string" && Object.hasOwn(MESSAGES, code)) return MESSAGES[code];
  const http = typeof code === "string" ? HTTP_CODE_RE.exec(code) : null;
  if (http !== null) return `The server answered with an error (HTTP ${http[1]}). Try again.`;
  return GENERIC_MESSAGE;
}

/** Every code fetchErrorMessage has its own words for (for the tests). */
export const FETCH_ERROR_CODES = Object.freeze(Object.keys(MESSAGES));

export class FetchError extends Error {
  /** @param {string} code see fetchErrorMessage */
  constructor(code) {
    super(fetchErrorMessage(code));
    this.name = "FetchError";
    this.code = code;
  }
}

/**
 * Seconds from a t= or start= value, or null when it is not one.
 * @param {string} value e.g. "90", "90s", "1m30s", "1h2m3s", "90.5"
 * @returns {number | null} whole seconds
 */
export function parseTimestamp(value) {
  if (typeof value !== "string" || value === "") return null;
  const match = TIMESTAMP_RE.exec(value);
  if (match === null) return null;
  const [, hours = "0", minutes = "0", seconds = "0"] = match;
  return Number(hours) * 3600 + Number(minutes) * 60 + Number(seconds);
}

/**
 * Seconds from a start time as a person types it, or null when it is not one.
 * 1:23:45, 83:45 and a bare number of seconds, with ':', '.', ',' or spaces
 * between the parts, and YouTube's own 1h23m45s.
 * @param {string} text
 * @returns {number | null} whole seconds, at most MAX_START_SECONDS
 */
export function parseStartTime(text) {
  if (typeof text !== "string") return null;
  const trimmed = text.trim();
  if (trimmed === "") return null;
  let seconds;
  if (START_PARTS_RE.test(trimmed)) {
    seconds = 0;
    // 83:45 is 83 minutes: a part past 59 is taken as it is, not refused.
    for (const part of trimmed.split(/[:.,\s]+/)) seconds = seconds * 60 + Number(part);
  } else {
    seconds = parseTimestamp(trimmed);
    if (seconds === null) return null;
  }
  return Number.isSafeInteger(seconds) && seconds <= MAX_START_SECONDS ? seconds : null;
}

/**
 * A number of seconds as the start field shows it: 0:00, 12:30, 1:23:45.
 * @param {number} seconds
 * @returns {string}
 */
export function formatStartTime(seconds) {
  const whole = Number.isFinite(seconds) ? Math.max(0, Math.floor(seconds)) : 0;
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  const rest = String(whole % 60).padStart(2, "0");
  return hours > 0 ? hours + ":" + String(minutes).padStart(2, "0") + ":" + rest : minutes + ":" + rest;
}

function parseWebAddress(text) {
  if (!WEB_ADDRESS_RE.test(text)) return null;
  let url;
  try {
    url = new URL(text);
  } catch {
    return null;
  }
  // A user name, password or port has no place in a link someone shared from
  // YouTube; such a link is not trusted to mean the video it seems to name.
  if (url.username !== "" || url.password !== "" || url.port !== "") return null;
  // hostname comes lower-cased from the URL parser; pathname keeps any %XX
  // escapes, which the ID pattern then refuses.
  const segments = url.pathname.split("/").filter((segment) => segment !== "");
  let videoId = null;
  if (url.hostname === SHORT_HOST) {
    if (segments.length === 1) videoId = segments[0];
  } else if (HOSTS.has(url.hostname)) {
    if (segments.length === 1 && segments[0] === "watch") {
      videoId = url.searchParams.get("v");
    } else if (segments.length === 2 && ID_PATHS.has(segments[0])) {
      videoId = segments[1];
    }
  }
  if (videoId === null || !VIDEO_ID_RE.test(videoId)) return null;
  // Everything else in the query (si=, feature=, list=, pp=) is dropped here:
  // only the ID ever leaves the device, and the timestamp stays on it.
  const timestamp = url.searchParams.get("t") ?? url.searchParams.get("start");
  return { videoId, timestamp: timestamp === null ? null : parseTimestamp(timestamp) };
}

/**
 * The video ID and t= timestamp from a pasted or shared YouTube link.
 * Accepts watch?v=, youtu.be/, /shorts/, /live/, /embed/ and /v/ links on
 * youtube.com, www., m. and music.youtube.com and youtu.be, a bare
 * 11-character ID, and shared text with such a link in it.
 * @param {string} text
 * @returns {{videoId: string, timestamp: number | null} | null}
 */
export function parseYouTubeLink(text) {
  if (typeof text !== "string") return null;
  const trimmed = text.trim();
  if (trimmed === "" || trimmed.length > MAX_LINK_TEXT) return null;
  if (VIDEO_ID_RE.test(trimmed)) return { videoId: trimmed, timestamp: null };
  const whole = parseWebAddress(trimmed);
  if (whole !== null) return whole;
  // Some apps share "<title> <link>": the first word that is a YouTube link.
  // A bare ID is not looked for among words, where any 11-letter word
  // ("Interesting") would pass for one.
  for (const word of trimmed.split(/\s+/)) {
    const found = parseWebAddress(word);
    if (found !== null) return found;
  }
  return null;
}

async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

// The error for a refused request. A 404 means something different on each
// route (no fetcher behind the proxy, or a job that expired), so the caller
// names it; any other code the fetcher sent is kept when this page knows it.
async function refusal(response, notFoundCode) {
  if (response.status === 404) return new FetchError(notFoundCode);
  const payload = await readJson(response);
  const code = payload !== null && typeof payload === "object" ? payload.error : undefined;
  if (typeof code === "string" && Object.hasOwn(MESSAGES, code)) return new FetchError(code);
  return new FetchError("http_" + response.status);
}

function checkJobId(jobId) {
  if (typeof jobId !== "string" || !JOB_ID_RE.test(jobId)) throw new FetchError("bad_response");
}

/**
 * Ask the fetcher for a video's audio. Only the ID is sent, and with a start
 * that one whole number, as JSON with a fixed Content-Type, which the fetcher
 * requires exactly; the browser adds the Origin header the fetcher checks.
 * @param {string} videoId an 11-character ID (VIDEO_ID_RE)
 * @param {number | null} [start] the second a window of a longer video
 *   starts at (0 to MAX_START_SECONDS); null asks for the whole audio
 * @returns {Promise<string>} the job's 32-hex ID
 */
export async function fetchJob(videoId, start = null) {
  if (typeof videoId !== "string" || !VIDEO_ID_RE.test(videoId)) throw new FetchError("bad_link");
  const request = { video_id: videoId };
  if (start !== null) {
    // A whole number in the fetcher's range, or nothing is sent at all.
    if (!Number.isInteger(start) || start < 0 || start > MAX_START_SECONDS) throw new FetchError("bad_time");
    request.start = start;
  }
  let response;
  try {
    response = await fetch("/fetch/api/jobs", {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(request),
    });
  } catch {
    throw new FetchError("network");
  }
  if (!response.ok) throw await refusal(response, "unavailable");
  if (response.status !== 202) throw new FetchError("bad_response");
  const payload = await readJson(response);
  if (payload === null || typeof payload !== "object") throw new FetchError("bad_response");
  checkJobId(payload.job_id);
  return payload.job_id;
}

// Only the fields the fetcher documents, each checked; the title is cut to
// 200 code points again, so a bad answer cannot flood the name field.
function readStatus(payload) {
  if (payload === null || typeof payload !== "object" || Array.isArray(payload) || !STATUSES.has(payload.status)) {
    throw new FetchError("bad_response");
  }
  if (payload.status === "failed") {
    if (!JOB_ERRORS.has(payload.error)) throw new FetchError("bad_response");
    throw new FetchError(payload.error);
  }
  if (payload.status !== "done" && payload.status !== "long") return { status: payload.status };
  const title = typeof payload.title === "string" ? [...payload.title.trim()].slice(0, MAX_TITLE_CHARACTERS).join("") : "";
  const duration = payload.duration;
  const metadata = {
    title: title === "" ? null : title,
    duration: typeof duration === "number" && Number.isFinite(duration) && duration > 0 ? duration : null,
    // suggestSelection checks every point and ignores a bad list.
    heatmap: Array.isArray(payload.heatmap) ? payload.heatmap : null,
  };
  if (payload.status === "long") {
    // "Long" is a statement about the length: without one it says nothing.
    if (metadata.duration === null) throw new FetchError("bad_response");
    return { status: "long", ...metadata };
  }
  const windowStart = payload.window_start;
  return {
    status: "done",
    ...metadata,
    // Where a window's audio begins in the video; null for a whole download.
    windowStart: typeof windowStart === "number" && Number.isFinite(windowStart) && windowStart >= 0 ? windowStart : null,
  };
}

function delay(ms) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

/**
 * Poll a job until it is done, or until the fetcher says the video is long.
 * @param {string} jobId from fetchJob
 * @param {{intervalMs?: number, maxPolls?: number, onStatus?: (status: string) => void,
 *   signal?: AbortSignal}} [options] onStatus hears "queued" and "running";
 *   an aborted signal stops the polling with the code "cancelled"
 * @returns {Promise<{status: "done" | "long", title: string | null, duration: number | null,
 *   heatmap: unknown[] | null, windowStart?: number | null}>} "long" (always
 *   with a duration) has no audio to fetch; "done" has windowStart, null
 *   unless the audio is a window. Rejects with a FetchError: the job's error
 *   when it failed, "expired" when the fetcher no longer knows it,
 *   "bad_response" for an answer that is not one, "gave_up" after maxPolls
 */
export async function pollJob(jobId, { intervalMs = POLL_INTERVAL_MS, maxPolls = MAX_POLLS, onStatus = null,
  signal = null } = {}) {
  checkJobId(jobId);
  for (let poll = 0; poll < maxPolls; poll += 1) {
    // The first look is at once: a short video may already be running.
    if (poll > 0) await delay(intervalMs);
    if (signal !== null && signal.aborted) throw new FetchError("cancelled");
    let response;
    try {
      response = await fetch("/fetch/api/jobs/" + jobId, { credentials: "same-origin", cache: "no-store" });
    } catch {
      throw new FetchError("network");
    }
    if (!response.ok) throw await refusal(response, "expired");
    const status = readStatus(await readJson(response));
    if (status.status === "done" || status.status === "long") return status;
    if (onStatus !== null) onStatus(status.status);
  }
  throw new FetchError("gave_up");
}

/**
 * Fetch a finished job's audio, once: the fetcher deletes it as it sends it,
 * so there is no retry. The bytes stay in memory and are decoded like a
 * picked file.
 *
 * The page's byte cap applies before the bytes pile up, whatever
 * FETCH_MAX_BYTES lets the fetcher hand over: a Content-Length over it is
 * refused before the body is read, and because that header can be missing or
 * wrong, the body is counted as it arrives and dropped once past the cap, at
 * most one chunk after it. What arrives goes straight into one buffer, so
 * the page holds the audio once, not once in pieces and once joined.
 * @param {string} jobId
 * @param {{maxBytes?: number}} [options] the cap, MAX_INPUT_BYTES unless a
 *   test passes a smaller one
 * @returns {Promise<Uint8Array>} rejects with "audio_too_large" over the cap
 */
export async function fetchAudio(jobId, { maxBytes = MAX_INPUT_BYTES } = {}) {
  checkJobId(jobId);
  let response;
  try {
    response = await fetch("/fetch/api/jobs/" + jobId + "/audio", { credentials: "same-origin", cache: "no-store" });
  } catch {
    throw new FetchError("network");
  }
  if (!response.ok) throw await refusal(response, "expired");
  const body = response.body;
  const declared = response.headers.get("Content-Length");
  if (declared !== null && Number(declared) > maxBytes) {
    if (body !== null && body !== undefined) body.cancel().catch(() => {});
    throw new FetchError("audio_too_large");
  }
  if (body === null || body === undefined || typeof body.getReader !== "function") {
    // A browser without a readable body stream: the size is checked before
    // anything uses the bytes.
    let buffer;
    try {
      buffer = await response.arrayBuffer();
    } catch {
      throw new FetchError("network");
    }
    if (!(buffer.byteLength <= maxBytes)) throw new FetchError("audio_too_large");
    return new Uint8Array(buffer);
  }
  const reader = body.getReader();
  // One buffer for the whole body, filled as it arrives, so the audio is
  // never held twice: as long as the Content-Length says, or as long as the
  // cap where that header is missing or no plain number. Keeping the pieces
  // to join them at the end would hold a download at the cap twice over.
  const announced = declared !== null && /^[0-9]+$/.test(declared);
  let bytes = new Uint8Array(announced ? Number(declared) : maxBytes);
  let total = 0;
  for (;;) {
    let chunk;
    try {
      chunk = await reader.read();
    } catch {
      // The body broke off part-way.
      throw new FetchError("network");
    }
    if (chunk.done) break;
    const end = total + chunk.value.byteLength;
    // Written so that a count that is not a number is refused too.
    if (!(end <= maxBytes)) {
      reader.cancel().catch(() => {});
      throw new FetchError("audio_too_large");
    }
    if (end > bytes.length) {
      // More than the header announced, as after a proxy changed the body on
      // the way: the bytes so far move to a buffer of the cap's size, once.
      const larger = new Uint8Array(maxBytes);
      larger.set(bytes.subarray(0, total));
      bytes = larger;
    }
    bytes.set(chunk.value, total);
    total = end;
  }
  // A view where the body was shorter than its buffer. The caller copies
  // what it is given into a File, and the buffer goes with this array.
  return total === bytes.length ? bytes : bytes.subarray(0, total);
}
