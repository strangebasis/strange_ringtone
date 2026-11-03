// What the client sends to and reads from the server (core/static/audio/request.js).
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  ERROR_CODES,
  displayName,
  errorMessage,
  fileNameFromDisposition,
  renderQuery,
  toneNameHeader,
} from "../../core/static/audio/request.js";

// server.py _QUERY_RE: no '%' or '+'.
const SERVER_QUERY_RE = /^[A-Za-z0-9_.=&-]*$/;
// names.py _HEADER_RE: visible ASCII only.
const SERVER_HEADER_RE = /^[\x21-\x7e]*$/;

test("the query is in fixed order with fixed decimals", () => {
  const query = renderQuery({ fadeIn: 0.2, fadeOut: 1.5, normalize: true, targetLufs: -14 });
  assert.equal(query, "fade_in=0.200&fade_out=1.500&normalize=true&target_lufs=-14.0");
  assert.match(query, SERVER_QUERY_RE);
  assert.equal(
    renderQuery({ fadeIn: 5, fadeOut: 0, normalize: false, targetLufs: -9 }),
    "fade_in=5.000&fade_out=0.000&normalize=false&target_lufs=-9.0",
  );
  assert.equal(
    renderQuery({ fadeIn: 0.12345, fadeOut: 4.9996, normalize: true, targetLufs: -23.96 }),
    "fade_in=0.123&fade_out=5.000&normalize=true&target_lufs=-24.0",
  );
});

test("negative zero and tiny negatives are written as 0.000", () => {
  assert.equal((-0).toFixed(3), "0.000");
  const query = renderQuery({ fadeIn: -0, fadeOut: -0.0001, normalize: true, targetLufs: -14 });
  assert.equal(query, "fade_in=0.000&fade_out=0.000&normalize=true&target_lufs=-14.0");
});

test("normalize is the literal true only for true", () => {
  for (const value of [false, 1, "true", undefined, null]) {
    assert.match(renderQuery({ fadeIn: 0, fadeOut: 0, normalize: value, targetLufs: -14 }), /normalize=false/);
  }
});

test("values the server would refuse throw instead", () => {
  const ok = { fadeIn: 0.2, fadeOut: 1.5, normalize: true, targetLufs: -14 };
  for (const bad of [
    { fadeIn: -0.01 }, { fadeIn: 5.001 }, { fadeOut: NaN }, { fadeOut: Infinity },
    { targetLufs: -8.9 }, { targetLufs: -24.1 }, { targetLufs: "-14" }, { fadeIn: undefined },
  ]) {
    assert.throws(() => renderQuery({ ...ok, ...bad }), RangeError, JSON.stringify(bad));
  }
});

test("the tone name header is trimmed, NFC and percent-encoded UTF-8", () => {
  assert.equal(toneNameHeader("  My Tone  "), "My%20Tone");
  assert.equal(toneNameHeader("Café"), "Caf%C3%A9");
  assert.equal(toneNameHeader("a+b/c?d"), "a%2Bb%2Fc%3Fd");
  assert.equal(toneNameHeader("\u{1F600}"), "%F0%9F%98%80");
  assert.match(toneNameHeader("x\u0000‮\ny"), SERVER_HEADER_RE);
});

test("an empty name sends no header", () => {
  assert.equal(toneNameHeader(""), null);
  assert.equal(toneNameHeader("   \t\n"), null);
  assert.equal(toneNameHeader(null), null);
  assert.equal(toneNameHeader(undefined), null);
  assert.equal(toneNameHeader(42), null);
});

test("a lone surrogate is dropped instead of throwing", () => {
  // Not U+FFFD: the server keeps that character (category So), so it would
  // reach the tone's name.
  assert.equal(toneNameHeader("a\ud800b"), "ab");
  assert.equal(toneNameHeader("a\udc00\ud800b"), "ab");
  // A pair is a real character and stays.
  assert.equal(toneNameHeader("a\ud83d\ude00b"), "a%F0%9F%98%80b");
  // Nothing else left: no header, so the server uses its default name.
  assert.equal(toneNameHeader("\udc00"), null);
  assert.equal(toneNameHeader("My Tone \ud800"), "My%20Tone");
});

test("the header is cut at a code point boundary to at most 512 bytes", () => {
  for (const [name, unit] of [
    ["a".repeat(600), 1],
    ["é".repeat(300), 6],
    ["\u{1F600}".repeat(100), 12],
    ["ab" + "\u{1F600}".repeat(100), 12],
    ["abc" + "€".repeat(200), 9],
  ]) {
    const header = toneNameHeader(name);
    assert.ok(header.length <= 512, `${header.length}`);
    // Within one code point of the limit: nothing more was cut than needed.
    assert.ok(header.length > 512 - unit, `${header.length}`);
    assert.match(header, SERVER_HEADER_RE);
    // decodeURIComponent throws on a split UTF-8 sequence.
    const decoded = decodeURIComponent(header);
    assert.ok(name.trim().normalize("NFC").startsWith(decoded));
  }
  assert.equal(toneNameHeader("a".repeat(512)).length, 512);
});

test("the cut stops at the first code point that does not fit", () => {
  // 505 + 9 bytes for the euro sign is over 512; the "b" after it would fit,
  // but a name with a character missing from its middle is not a cut.
  assert.equal(toneNameHeader("a".repeat(505) + "\u20ac" + "b"), "a".repeat(505));
  assert.equal(toneNameHeader("a".repeat(501) + "\u{1F600}" + "bc"), "a".repeat(501));
});

test("displayName strips the extension and keeps the rest", () => {
  assert.equal(displayName("Song.mp3"), "Song");
  assert.equal(displayName("My.Song.flac"), "My.Song");
  assert.equal(displayName("Mr. Brightside"), "Mr. Brightside");
  assert.equal(displayName("noext"), "noext");
  assert.equal(displayName(" spaced .m4a"), "spaced");
  assert.equal(displayName(".mp3"), "");
  assert.equal(displayName("dir/sub\\Tone.wav"), "Tone");
  assert.equal(displayName("dir/song.mp3"), "song");
  assert.equal(displayName("dir\\song.mp3"), "song");
  assert.equal(displayName("Café \u{1F600}.opus"), "Café \u{1F600}");
  assert.equal(displayName(undefined), "");
});

test("fileNameFromDisposition reads the server's filename*", () => {
  // names.content_disposition's exact shape.
  assert.equal(
    fileNameFromDisposition("attachment; filename=\"Cafe.m4a\"; filename*=UTF-8''Caf%C3%A9.m4a"),
    "Café",
  );
  assert.equal(fileNameFromDisposition("attachment; filename=\"x.m4r\"; filename*=UTF-8''Don%27t%20Stop.m4r"), "Don't Stop");
  assert.equal(fileNameFromDisposition("attachment; filename*=utf-8''ringtone.m4a"), "ringtone");
  assert.equal(fileNameFromDisposition("attachment;filename*=UTF-8''%F0%9F%98%80.m4a"), "\u{1F600}");
});

test("fileNameFromDisposition is null when filename* is absent or malformed", () => {
  assert.equal(fileNameFromDisposition(null), null);
  assert.equal(fileNameFromDisposition(""), null);
  assert.equal(fileNameFromDisposition("attachment; filename=\"Song.m4a\""), null);
  assert.equal(fileNameFromDisposition("attachment; filename*=UTF-8''%E9.m4a"), null);
  assert.equal(fileNameFromDisposition("attachment; filename*=UTF-8''%ZZ.m4a"), null);
  assert.equal(fileNameFromDisposition("attachment; filename*=UTF-8''.m4a"), null);
  assert.equal(fileNameFromDisposition("attachment; xfilename*=UTF-8''a.m4a"), null);
});

// Every code the core answers with (server.py, http_guard.py). The Python
// wrapper also checks this list against the server's source.
const SERVER_CODES = [
  "bad_origin", "unsupported_media_type", "transfer_encoding", "bad_length", "length_required",
  "too_large", "bad_params", "bad_name", "bad_wav", "rate_limited", "busy", "results_full",
  "body_timeout", "incomplete_body", "render_timeout", "render_failed", "internal_error",
  "not_found", "method_not_allowed",
];

test("every server error code has its own plain words", () => {
  const fallback = errorMessage(500, "no_such_code_xyz", null);
  for (const code of SERVER_CODES) {
    assert.ok(ERROR_CODES.includes(code), code);
    const message = errorMessage(400, code, null);
    assert.notEqual(message, fallback, code);
    // Plain words: no code_like_this in what the user reads.
    assert.doesNotMatch(message, /[a-z]+_[a-z]+/, code);
    assert.ok(message.length > 10);
  }
  assert.deepEqual([...ERROR_CODES].sort(), [...SERVER_CODES].sort());
});

test("not_found says the tone expired", () => {
  assert.match(errorMessage(404, "not_found"), /expired/);
  assert.match(errorMessage(404, "not_found"), /[Rr]ender it again/);
});

test("the wav.py details users can act on get their own words", () => {
  assert.match(errorMessage(400, "bad_wav", "too_short"), /half a second/);
  assert.match(errorMessage(413, "too_large", "too_long"), /longer than the server allows/);
  assert.notEqual(errorMessage(413, "too_large", "too_long"), errorMessage(413, "too_large"));
});

test("those words belong to bad_wav and too_large only", () => {
  // A detail is the wav.py reason only under those two codes; under any
  // other code it must not turn "the server is busy" into "make it longer".
  for (const code of ["busy", "bad_params", "rate_limited", "teapot_mode", null]) {
    for (const detail of ["too_short", "too_long"]) {
      assert.equal(errorMessage(400, code, detail), errorMessage(400, code), `${code} ${detail}`);
    }
  }
});

test("other bad_wav details are named after the plain words", () => {
  for (const detail of ["not_riff", "truncated", "bad_size", "unexpected_chunk", "duplicate_chunk", "missing_chunk",
    "unsupported_format", "bad_bits", "bad_channels", "bad_rate", "bad_block_align", "bad_byte_rate",
    "bad_data_size", "trailing_data", "wave_mismatch"]) {
    const message = errorMessage(400, "bad_wav", detail);
    assert.ok(message.startsWith(errorMessage(400, "bad_wav")), detail);
    assert.ok(message.endsWith(`(${detail})`), detail);
  }
  // A detail that does not look like a code is not echoed.
  assert.equal(errorMessage(400, "bad_wav", "<b>x</b>"), errorMessage(400, "bad_wav"));
});

test("an unknown code is named; anything else falls back to the status", () => {
  assert.match(errorMessage(418, "teapot_mode"), /teapot_mode/);
  assert.match(errorMessage(418, "teapot_mode"), /HTTP 418/);
  assert.equal(errorMessage(502, "<html>"), errorMessage(502, null));
  assert.match(errorMessage(502, null), /HTTP 502/);
  assert.match(errorMessage(undefined, undefined), /error/);
  // A status that is not an integer (a network failure has none) is left
  // out rather than shown as "HTTP undefined".
  for (const status of [undefined, null, NaN, 404.5, "500"]) {
    for (const message of [errorMessage(status, undefined), errorMessage(status, null), errorMessage(status, "teapot_mode")]) {
      assert.doesNotMatch(message, /HTTP|undefined|null|NaN/, String(status));
    }
  }
  // Inherited properties are not codes.
  assert.match(errorMessage(400, "constructor"), /constructor/);
  assert.match(errorMessage(400, "__proto__"), /HTTP 400/);
});
