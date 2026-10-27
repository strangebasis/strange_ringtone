// Fetching a clip from a YouTube link (docs/PLAN.md 6 and 7.4). The link is
// parsed on this device and only its 11-character video ID is sent: the
// fetcher builds the YouTube address itself, so nothing a user typed or
// shared ever reaches it. Every URL here is a same-origin /fetch/ path, which
// the deployment's proxy forwards to the fetcher, so connect-src 'self'
// covers them and the CSP stays exactly as it is.

/** A YouTube video ID, as the fetcher accepts it. */
export const VIDEO_ID_RE = /^[A-Za-z0-9_-]{11}$/;
// The fetcher's job IDs: 128 random bits as lower-case hex. Checked before an
// ID from the server is built into a path, as share.js does for results.
const JOB_ID_RE = /^[0-9a-f]{32}$/;

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

const STATUSES = new Set(["queued", "running", "done", "failed"]);
// The fetcher's fixed job errors (docs/PLAN.md 6).
const JOB_ERRORS = new Set(["blocked", "too_long", "not_found", "timeout", "failed"]);
const MAX_TITLE_CHARACTERS = 200;

// Plain words for every code the fetcher answers with, and for the few this
// module adds itself (bad_link, network, bad_response, unavailable, expired,
// gave_up, cancelled). tests/test_frontend_js.py fails when the fetcher gains
// a code that is missing here.
const MESSAGES = {
  // The job's own outcome.
  blocked: "YouTube would not hand this video to the server. Try again later, or save the audio another way and pick the file.",
  // FETCH_MAX_SECONDS may be set lower, never above the page's 10-minute
  // decode limit, so "at most" stays true either way.
  too_long: "This video is longer than the server will fetch (at most 10 minutes). Pick a shorter video, or trim the audio first and pick the file.",
  not_found: "This video could not be found. It may be private, removed or not available where the server is.",
  timeout: "Fetching this video took too long. Try again in a moment.",
  failed: "The server could not fetch this video's audio. Try again, or try another video.",
  // Refusals of the request itself.
  rate_limited: "Too many fetches in the last minute. Wait a moment, then try again.",
  busy: "The server is busy with other fetches. Try again in a moment.",
  bad_video_id: "The server did not accept this video's ID. Check the link and try again.",
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
  network: "The server could not be reached. Check the connection and try again.",
  bad_response: "The server sent an answer this page does not understand. Reload the page and try again.",
  unavailable: "YouTube fetching is not available on this server right now.",
  expired: "The fetched audio is no longer on the server. Fetch the video again.",
  gave_up: "The fetch is taking far longer than it should. Try again later.",
  cancelled: "The fetch was replaced by a newer one.",
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
 * Ask the fetcher for a video's audio. Only the ID is sent, as JSON with a
 * fixed Content-Type, which the fetcher requires exactly; the browser adds
 * the Origin header the fetcher checks.
 * @param {string} videoId an 11-character ID (VIDEO_ID_RE)
 * @returns {Promise<string>} the job's 32-hex ID
 */
export async function fetchJob(videoId) {
  if (typeof videoId !== "string" || !VIDEO_ID_RE.test(videoId)) throw new FetchError("bad_link");
  let response;
  try {
    response = await fetch("/fetch/api/jobs", {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ video_id: videoId }),
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
  if (payload.status !== "done") return { status: payload.status };
  const title = typeof payload.title === "string" ? [...payload.title.trim()].slice(0, MAX_TITLE_CHARACTERS).join("") : "";
  const duration = payload.duration;
  return {
    status: "done",
    title: title === "" ? null : title,
    duration: typeof duration === "number" && Number.isFinite(duration) && duration > 0 ? duration : null,
    // suggestSelection checks every point and ignores a bad list.
    heatmap: Array.isArray(payload.heatmap) ? payload.heatmap : null,
  };
}

function delay(ms) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

/**
 * Poll a job until it is done.
 * @param {string} jobId from fetchJob
 * @param {{intervalMs?: number, maxPolls?: number, onStatus?: (status: string) => void,
 *   signal?: AbortSignal}} [options] onStatus hears "queued" and "running";
 *   an aborted signal stops the polling with the code "cancelled"
 * @returns {Promise<{status: "done", title: string | null, duration: number | null, heatmap: unknown[] | null}>}
 *   rejects with a FetchError: the job's error when it failed, "expired"
 *   when the fetcher no longer knows it, "bad_response" for an answer that
 *   is not one, "gave_up" after maxPolls
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
    if (status.status === "done") return status;
    if (onStatus !== null) onStatus(status.status);
  }
  throw new FetchError("gave_up");
}

/**
 * Fetch a finished job's audio, once: the fetcher deletes it as it sends it,
 * so there is no retry. The bytes stay in memory and are decoded like a
 * picked file.
 * @param {string} jobId
 * @returns {Promise<Uint8Array>}
 */
export async function fetchAudio(jobId) {
  checkJobId(jobId);
  let response;
  try {
    response = await fetch("/fetch/api/jobs/" + jobId + "/audio", { credentials: "same-origin", cache: "no-store" });
  } catch {
    throw new FetchError("network");
  }
  if (!response.ok) throw await refusal(response, "expired");
  try {
    return new Uint8Array(await response.arrayBuffer());
  } catch {
    // The body broke off part-way.
    throw new FetchError("network");
  }
}
