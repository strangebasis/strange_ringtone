// What the client sends to POST /api/render and reads back from the result
// routes, kept pure so node --test pins every byte of it.

// The server's bounds (server.py FADE_MAX_SECONDS, TARGET_LUFS_MIN/MAX).
// Checked here so a bug shows up as a thrown error in the console instead of a
// 400 bad_params from the server.
const FADE_MAX = 5;
const TARGET_MIN = -24;
const TARGET_MAX = -9;
// names.py MAX_HEADER_BYTES: the encoded header's length in bytes.
const MAX_HEADER_BYTES = 512;

function decimal(value, digits, low, high, what) {
  if (!Number.isFinite(value)) {
    throw new RangeError(`${what} must be a finite number`);
  }
  let text = value.toFixed(digits);
  // toFixed keeps the sign of a tiny negative (-0.0001 -> "-0.000"). The
  // server would take it, but one value with two spellings is not what the
  // fixed format is for.
  if (Number(text) === 0) {
    text = (0).toFixed(digits);
  }
  const rounded = Number(text);
  if (rounded < low || rounded > high) {
    throw new RangeError(`${what} is out of range`);
  }
  return text;
}

/**
 * The render query string, built by concatenation in a fixed order:
 * fade_in=<3dp>&fade_out=<3dp>&normalize=<true|false>&target_lufs=<1dp>.
 * Not URLSearchParams: the server's query check refuses '%' and '+', and a
 * fixed format leaves one spelling per value. Only ASCII digits, '.', '-',
 * letters, '=', '_' and '&' appear.
 * @param {{fadeIn: number, fadeOut: number, normalize: boolean, targetLufs: number}} options
 * @returns {string} without a leading '?'
 */
export function renderQuery({ fadeIn, fadeOut, normalize, targetLufs }) {
  return (
    "fade_in=" + decimal(fadeIn, 3, 0, FADE_MAX, "fadeIn") +
    "&fade_out=" + decimal(fadeOut, 3, 0, FADE_MAX, "fadeOut") +
    "&normalize=" + (normalize === true ? "true" : "false") +
    "&target_lufs=" + decimal(targetLufs, 1, TARGET_MIN, TARGET_MAX, "targetLufs")
  );
}

function wellFormed(text) {
  // encodeURIComponent throws on a lone surrogate (from a pasted, broken
  // string), so each is dropped. Not replaced with U+FFFD: the server keeps
  // that character (its category is So, which names.py lets through with the
  // emoji), so it would end up in the tone's name as a "?" box.
  let out = "";
  for (const point of text) {
    const code = point.codePointAt(0);
    if (code < 0xd800 || code > 0xdfff) out += point;
  }
  return out;
}

/**
 * The X-Tone-Name header value: trimmed, NFC, percent-encoded UTF-8, at most
 * 512 bytes. The cut removes whole code points from the end, never part of
 * one: a cut inside a multi-byte sequence would be invalid UTF-8, which the
 * server refuses as bad_name. The server sanitises the name again; this only
 * makes sure the header is one it accepts.
 * @param {string} name what the user typed
 * @returns {string | null} null when the name is empty (omit the header, and
 *   the server uses its default name)
 */
export function toneNameHeader(name) {
  if (typeof name !== "string") {
    return null;
  }
  // Dropped before the trim, so a name that ends in a broken surrogate
  // after a space does not keep the space.
  const text = wellFormed(name).trim().normalize("NFC");
  let header = "";
  for (const point of text) {
    const encoded = encodeURIComponent(point);
    if (header.length + encoded.length > MAX_HEADER_BYTES) {
      break;
    }
    header += encoded;
  }
  return header === "" ? null : header;
}

// A file extension as people meet them (.mp3, .m4a, .flac, .wav, .opus):
// letters and digits only, so "Mr. Brightside" keeps its words.
const EXTENSION_RE = /\.[A-Za-z0-9]{1,8}$/;

/**
 * The name field's default: the picked file's name without its extension.
 * @param {string} fileName File.name
 * @returns {string} possibly empty
 */
export function displayName(fileName) {
  if (typeof fileName !== "string") {
    return "";
  }
  // File.name has no directory, but strip one defensively in case a browser
  // ever hands over a path.
  const base = fileName.slice(Math.max(fileName.lastIndexOf("/"), fileName.lastIndexOf("\\")) + 1);
  // A name that is all extension (".mp3") leaves the field empty, and an
  // empty name sends no header, so the server uses its default.
  return base.replace(EXTENSION_RE, "").trim();
}

// filename*=UTF-8''<pct-encoded> (RFC 8187), as names.content_disposition
// writes it. The value is attr-chars and %XX only, so it ends at ';' or space.
const FILENAME_STAR_RE = /(?:^|;)\s*filename\*\s*=\s*UTF-8''([^;\s]+)/i;

/**
 * The server's sanitised tone name from a result's Content-Disposition.
 * @param {string | null} header the Content-Disposition value
 * @returns {string | null} the name without its extension, or null when the
 *   header has no filename* or it is not valid percent-encoded UTF-8
 */
export function fileNameFromDisposition(header) {
  if (typeof header !== "string") {
    return null;
  }
  const match = FILENAME_STAR_RE.exec(header);
  if (match === null) {
    return null;
  }
  let name;
  try {
    name = decodeURIComponent(match[1]);
  } catch {
    return null;
  }
  name = name.replace(EXTENSION_RE, "");
  return name === "" ? null : name;
}

// Plain words for every error the core answers with. The keys are the codes in
// server.py and http_guard.py; tests/test_frontend_js.py fails when the server
// gains a code that is missing here.
const MESSAGES = {
  bad_origin: "The server refused this page's address. Open the app at the address it was set up for.",
  unsupported_media_type: "The upload was not sent as a WAV. Reload the page and try again.",
  transfer_encoding: "The upload was sent in a form the server does not accept. Reload the page and try again.",
  bad_length: "The upload's size was not stated correctly. Reload the page and try again.",
  length_required: "The upload's size was missing. Reload the page and try again.",
  too_large: "The upload is larger than the server accepts. Choose a shorter selection.",
  bad_params: "The fade or loudness settings were refused. Reload the page and try again.",
  bad_name: "The tone name was refused. Try a shorter or simpler name.",
  bad_wav: "The audio the page built was refused. Reload the page and try again.",
  rate_limited: "Too many renders in the last minute. Wait a moment, then try again.",
  busy: "The server is busy with other renders. Try again in a moment.",
  results_full: "The server is holding too many finished tones. Try again in a few seconds.",
  body_timeout: "The upload took too long. Check the connection and try again.",
  incomplete_body: "The upload was cut off. Check the connection and try again.",
  render_timeout: "Rendering took too long. Try again, or choose a shorter selection.",
  render_failed: "The server could not render this audio. Try another selection or another file.",
  internal_error: "Something went wrong on the server. Try again.",
  not_found: "This tone has expired. Render it again.",
  method_not_allowed: "The server refused the request. Reload the page and try again.",
};

// The details that come with bad_wav (wav.py CODES) and too_large. Only the
// ones a user can act on get their own words; the rest mean the page built a
// file the server does not accept, which a reload may fix.
const WAV_DETAILS = {
  too_short: "The selection is shorter than half a second. Make it longer.",
  too_long: "The selection is longer than the server allows. Make it shorter.",
};

// Codes are shown only when they look like one, so a proxy's error page or
// an odd body never ends up in the message.
const CODE_RE = /^[a-z0-9_]{1,40}$/;

/**
 * A plain-words message for a failed request.
 * @param {number} status HTTP status
 * @param {string | null | undefined} code the JSON body's "error"
 * @param {string | null | undefined} detail the JSON body's "detail"
 * @returns {string}
 */
export function errorMessage(status, code, detail) {
  if (typeof detail === "string" && Object.hasOwn(WAV_DETAILS, detail) && (code === "bad_wav" || code === "too_large")) {
    return WAV_DETAILS[detail];
  }
  if (typeof code === "string" && Object.hasOwn(MESSAGES, code)) {
    if (code === "bad_wav" && typeof detail === "string" && CODE_RE.test(detail)) {
      return `${MESSAGES.bad_wav} (${detail})`;
    }
    return MESSAGES[code];
  }
  const shown = Number.isInteger(status) ? ` (HTTP ${status})` : "";
  if (typeof code === "string" && CODE_RE.test(code)) {
    return `The server refused the request: ${code}${shown}. Try again.`;
  }
  return `The server answered with an error${shown}. Try again.`;
}

/** Every error code errorMessage has its own words for (for the tests). */
export const ERROR_CODES = Object.freeze(Object.keys(MESSAGES));
/** Every bad_wav detail with its own words (for the tests). */
export const WAV_DETAIL_CODES = Object.freeze(Object.keys(WAV_DETAILS));
