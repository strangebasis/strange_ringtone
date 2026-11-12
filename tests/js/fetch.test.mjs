// The YouTube link parser and the fetcher client (core/static/fetch.js),
// against a fake fetch: no request leaves the test.
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  FETCH_ERROR_CODES, FetchError, LINK_HOSTS, MAX_POLLS, MAX_START_SECONDS, POLL_INTERVAL_MS, VIDEO_ID_RE, fetchAudio,
  fetchErrorMessage, fetchJob, formatStartTime, parseStartTime, parseTimestamp, parseYouTubeLink, pollJob,
} from "../../core/static/fetch.js";
// The page's byte cap, through the namespace so this file still loads where
// decode.js lacks the export.
import * as decodeModule from "../../core/static/audio/decode.js";

const ID = "dQw4w9WgXcQ";
const JOB = "0123456789abcdef0123456789abcdef";

// --- the link parser --------------------------------------------------------------

test("every link form YouTube shares gives the ID", () => {
  const links = [
    `https://www.youtube.com/watch?v=${ID}`,
    `https://youtube.com/watch?v=${ID}`,
    `https://m.youtube.com/watch?v=${ID}`,
    `https://music.youtube.com/watch?v=${ID}&si=abcdEFGH1234`,
    `http://www.youtube.com/watch?v=${ID}`,
    `https://www.youtube.com/watch?feature=shared&v=${ID}&list=PL123&pp=xyz`,
    `https://youtu.be/${ID}`,
    `https://youtu.be/${ID}?si=AbCdEfGhIjKlMnOp`,
    `https://www.youtube.com/shorts/${ID}`,
    `https://youtube.com/shorts/${ID}?si=x`,
    `https://www.youtube.com/live/${ID}?feature=share`,
    `https://www.youtube.com/embed/${ID}`,
    `https://www.youtube.com/v/${ID}`,
    `https://youtu.be/${ID}/`,
    `HTTPS://WWW.YOUTUBE.COM/watch?v=${ID}`,
    `  https://youtu.be/${ID}\n`,
    ID,
    `\t${ID} `,
    `Never Gonna Give You Up https://youtu.be/${ID}?si=abc`,
    `Look at this:\nhttps://www.youtube.com/watch?v=${ID}`,
  ];
  for (const link of links) {
    assert.deepEqual(parseYouTubeLink(link), { videoId: ID, timestamp: null }, JSON.stringify(link));
  }
});

test("t= and start= are read as seconds, and nothing else is kept", () => {
  const cases = [
    [`https://youtu.be/${ID}?t=90`, 90],
    [`https://youtu.be/${ID}?si=abc&t=90s`, 90],
    [`https://www.youtube.com/watch?v=${ID}&t=1m30s`, 90],
    [`https://www.youtube.com/watch?v=${ID}&t=1h2m3s`, 3723],
    [`https://www.youtube.com/watch?v=${ID}&t=2m`, 120],
    [`https://www.youtube.com/watch?v=${ID}&t=1h`, 3600],
    [`https://www.youtube.com/watch?v=${ID}&t=90.75`, 90],
    [`https://www.youtube.com/watch?v=${ID}&t=1m30.9s`, 90],
    [`https://www.youtube.com/watch?v=${ID}&t=0`, 0],
    [`https://www.youtube.com/embed/${ID}?start=42`, 42],
    // t= wins over start=.
    [`https://www.youtube.com/watch?v=${ID}&start=5&t=7`, 7],
    // A malformed one is no timestamp, but the link still works.
    [`https://youtu.be/${ID}?t=abc`, null],
    [`https://youtu.be/${ID}?t=`, null],
    [`https://youtu.be/${ID}?t=-5`, null],
    [`https://youtu.be/${ID}?t=1m-30s`, null],
    [`https://youtu.be/${ID}?t=1e3`, null],
  ];
  for (const [link, timestamp] of cases) {
    const parsed = parseYouTubeLink(link);
    assert.deepEqual(parsed, { videoId: ID, timestamp }, link);
    // Exactly these two keys: no si=, no list=, nothing to send by accident.
    assert.deepEqual(Object.keys(parsed), ["videoId", "timestamp"]);
  }
});

test("parseTimestamp", () => {
  for (const [value, seconds] of [["90", 90], ["90s", 90], ["1m30s", 90], ["1h2m3s", 3723], ["1H2M3S", 3723],
    ["0", 0], ["3.5", 3], ["3.5s", 3], ["10m", 600], ["1h30", 3630]]) {
    assert.equal(parseTimestamp(value), seconds, value);
  }
  for (const value of ["", "s", "m", "abc", "-1", "+1", " 90", "90 ", "1.2.3", ".5", "1e3", "0x10", "1m1h",
    "1234567", "9999h", "١٢", null, 90, undefined]) {
    assert.equal(parseTimestamp(value), null, String(value));
  }
});

test("anything that is not a YouTube video link is refused", () => {
  const refused = [
    "",
    "   ",
    "not a link",
    // Alone, "Interesting" is a well-formed ID; among other words it is not
    // taken for one.
    "Interesting video",
    `${ID}x`,
    ID.slice(1),
    `${ID.slice(0, 10)}%`,
    `${ID.slice(0, 10)}.`,
    `${ID.slice(0, 10)}/`,
    "١٢٣٤٥٦٧٨٩٠١",
    `https://www.youtube.com/watch?v=${ID}x`,
    `https://www.youtube.com/watch?v=${ID.slice(1)}`,
    `https://www.youtube.com/watch?v=..%2F..%2Fxyzab`,
    `https://www.youtube.com/watch?list=PL123`,
    `https://www.youtube.com/watch/${ID}`,
    `https://www.youtube.com/shorts/${ID}/extra`,
    `https://www.youtube.com/shorts/%64Qw4w9WgXcQ`,
    `https://www.youtube.com/channel/${ID}`,
    `https://www.youtube.com/${ID}`,
    `https://youtu.be/`,
    `https://youtu.be/watch?v=${ID}`,
    `https://youtu.be/${ID}/extra`,
    `https://youtube.com.example.internal/watch?v=${ID}`,
    `https://example.internal/watch?v=${ID}`,
    `https://notyoutube.com/watch?v=${ID}`,
    `https://youtu.be.example.internal/${ID}`,
    `https://www.youtube.com./watch?v=${ID}`,
    `https://user:pass@www.youtube.com/watch?v=${ID}`,
    `https://user@youtu.be/${ID}`,
    `https://www.youtube.com:8443/watch?v=${ID}`,
    `ftp://youtu.be/${ID}`,
    `javascript:alert(1)//youtu.be/${ID}`,
    `//youtu.be/${ID}`,
    // Only text that starts with http(s):// is read as a link (the field's
    // placeholder shows no example link; tests/test_frontend_html.py).
    `youtu.be/${ID}`,
    `www.youtube.com/watch?v=${ID}`,
    `youtube.com/watch?v=${ID}`,
    `Watch this youtu.be/${ID}`,
    // Paths that only look like a video's.
    `https://www.youtube.com/playlist/${ID}`,
    `https://www.youtube.com/playlist?v=${ID}`,
    `https://www.youtube.com/watch/x?v=${ID}`,
    `https://www.youtube.com/x/watch?v=${ID}`,
    // A password alone is still a credential.
    `https://:pass@youtu.be/${ID}`,
    `https://:pass@www.youtube.com/watch?v=${ID}`,
    `https://example.internal/?u=https://youtu.be/${ID}`,
    `https://youtu.be/${ID}`.padEnd(2049, " ") + "x",
  ];
  for (const text of refused) {
    assert.equal(parseYouTubeLink(text), null, JSON.stringify(text));
  }
  for (const value of [null, undefined, 42, {}, [ID]]) {
    assert.equal(parseYouTubeLink(value), null, String(value));
  }
});

test("links are accepted from exactly the five YouTube hosts", () => {
  // Pinned, so a host added to the allowlist fails here: only these names
  // ever turn text into a request to the fetcher.
  assert.deepEqual([...LINK_HOSTS].sort(),
    ["m.youtube.com", "music.youtube.com", "www.youtube.com", "youtu.be", "youtube.com"]);
  assert.ok(Object.isFrozen(LINK_HOSTS));
  for (const host of LINK_HOSTS) {
    const link = host === "youtu.be" ? `https://${host}/${ID}` : `https://${host}/watch?v=${ID}`;
    assert.deepEqual(parseYouTubeLink(link), { videoId: ID, timestamp: null }, host);
  }
  for (const host of ["evil.example.internal", "www.youtube-nocookie.com", "studio.youtube.com", "gaming.youtube.com",
    "youtube.googleapis.com", "www.youtu.be", "m.youtu.be", "youtube.example.internal"]) {
    assert.equal(parseYouTubeLink(`https://${host}/watch?v=${ID}`), null, host);
    assert.equal(parseYouTubeLink(`https://${host}/${ID}`), null, host);
  }
});

test("the ID pattern is exactly 11 of A-Z, a-z, 0-9, _ and -", () => {
  assert.equal(VIDEO_ID_RE.source, "^[A-Za-z0-9_-]{11}$");
  assert.equal(VIDEO_ID_RE.flags, "");
  assert.ok(VIDEO_ID_RE.test("_-_-_-_-_-_"));
});

// --- talking to the fetcher -----------------------------------------------------

function json(body, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

const REAL_FETCH = globalThis.fetch;

// Replaces globalThis.fetch until the test ends; answers come from
// `answer(url, init, n)`, n counting the calls from 1.
function fakeFetch(t, answer) {
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    return answer(url, init, calls.length);
  };
  t.after(() => {
    globalThis.fetch = REAL_FETCH;
  });
  return calls;
}

async function rejectsWith(promise, code) {
  await assert.rejects(promise, (error) => {
    assert.ok(error instanceof FetchError, String(error));
    assert.equal(error.code, code);
    assert.equal(error.message, fetchErrorMessage(code));
    return true;
  });
}

test("fetchJob posts exactly {video_id} as JSON and returns the job ID", async (t) => {
  const calls = fakeFetch(t, () => json({ job_id: JOB }, 202));
  assert.equal(await fetchJob(ID), JOB);
  assert.equal(calls.length, 1);
  const [{ url, init }] = calls;
  assert.equal(url, "/fetch/api/jobs");
  assert.deepEqual(init, {
    method: "POST",
    credentials: "same-origin",
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
    body: `{"video_id":"${ID}"}`,
  });
});

test("fetchJob never sends anything but a valid ID", async (t) => {
  const calls = fakeFetch(t, () => json({ job_id: JOB }, 202));
  for (const id of [`https://youtu.be/${ID}`, ID.slice(1), `${ID} `, "../../../x", "", null, 12345678901]) {
    await rejectsWith(fetchJob(id), "bad_link");
  }
  assert.equal(calls.length, 0);
});

test("fetchJob: refusals keep the fetcher's code, a 404 means no fetcher", async (t) => {
  const answers = [
    [json({ error: "rate_limited" }, 429), "rate_limited"],
    [json({ error: "busy" }, 503), "busy"],
    [json({ error: "bad_origin" }, 403), "bad_origin"],
    [json({ error: "bad_video_id" }, 400), "bad_video_id"],
    [json({ error: "not_found" }, 404), "unavailable"],
    [new Response("<html>proxy</html>", { status: 502 }), "http_502"],
    [json({ error: "<b>odd</b>" }, 400), "http_400"],
    [json({ error: "zz_unknown" }, 400), "http_400"],
    // Success, but not the documented answer.
    [json({ job_id: JOB }, 200), "bad_response"],
    [json({ job_id: JOB.toUpperCase() }, 202), "bad_response"],
    [json({ job_id: "../../x" }, 202), "bad_response"],
    [json(null, 202), "bad_response"],
    [new Response("not json", { status: 202 }), "bad_response"],
  ];
  for (const [response, code] of answers) {
    const calls = fakeFetch(t, () => response);
    await rejectsWith(fetchJob(ID), code);
    assert.equal(calls.length, 1);
  }
  fakeFetch(t, () => Promise.reject(new TypeError("Failed to fetch")));
  await rejectsWith(fetchJob(ID), "network");
});

const DONE = {
  status: "done",
  title: "A Title",
  duration: 215.5,
  heatmap: [{ start: 0, end: 2.155, value: 1 }],
};

test("pollJob asks at once the first time, before any wait", async (t) => {
  const calls = fakeFetch(t, () => json(DONE));
  // With 5 s between polls, a first look that waited loses the race.
  let timer;
  const late = new Promise((resolve) => {
    timer = setTimeout(() => resolve("late"), 1000);
  });
  const done = await Promise.race([pollJob(JOB, { intervalMs: 5000 }), late]);
  clearTimeout(timer);
  assert.notEqual(done, "late", "the first poll waited for the interval");
  assert.equal(done.status, "done");
  assert.equal(calls.length, 1);
});

test("pollJob asks every interval until done and reports each state", async (t) => {
  const answers = [{ status: "queued" }, { status: "queued" }, { status: "running" }, DONE];
  const calls = fakeFetch(t, (url, init, n) => json(answers[n - 1]));
  const heard = [];
  const started = Date.now();
  const done = await pollJob(JOB, { intervalMs: 20, onStatus: (status) => heard.push(status) });
  assert.ok(Date.now() - started >= 55, "three waits between four polls");
  assert.deepEqual(done, { status: "done", title: "A Title", duration: 215.5, heatmap: DONE.heatmap, windowStart: null });
  assert.deepEqual(heard, ["queued", "queued", "running"]);
  assert.equal(calls.length, 4);
  for (const { url, init } of calls) {
    assert.equal(url, `/fetch/api/jobs/${JOB}`);
    assert.deepEqual(init, { credentials: "same-origin", cache: "no-store" });
  }
});

test("the poll defaults: every 2 s, for 15 minutes", () => {
  assert.equal(POLL_INTERVAL_MS, 2000);
  assert.equal(MAX_POLLS, 450);
  // A full queue: 4 waiting and 1 running, each up to 120 s.
  assert.ok(MAX_POLLS * POLL_INTERVAL_MS > 5 * 120 * 1000);
});

test("pollJob rejects with the job's own error", async (t) => {
  for (const error of ["blocked", "live", "not_found", "past_end", "timeout", "failed"]) {
    fakeFetch(t, () => json({ status: "failed", error }));
    await rejectsWith(pollJob(JOB, { intervalMs: 0 }), error);
  }
});

test("pollJob refuses answers that are not a documented status", async (t) => {
  const bodies = [
    { status: "failed" },
    { status: "failed", error: "rate_limited" },
    { status: "failed", error: "<script>" },
    { status: "collecting" },
    { status: "DONE" },
    {},
    [],
    null,
    "done",
  ];
  for (const body of bodies) {
    fakeFetch(t, () => json(body));
    await rejectsWith(pollJob(JOB, { intervalMs: 0 }), "bad_response");
  }
  fakeFetch(t, () => new Response("<html>", { status: 200 }));
  await rejectsWith(pollJob(JOB, { intervalMs: 0 }), "bad_response");
});

test("pollJob: a 404 is an expired job, other refusals keep their code", async (t) => {
  fakeFetch(t, () => json({ error: "not_found" }, 404));
  await rejectsWith(pollJob(JOB, { intervalMs: 0 }), "expired");
  fakeFetch(t, () => json({ error: "rate_limited" }, 429));
  await rejectsWith(pollJob(JOB, { intervalMs: 0 }), "rate_limited");
  fakeFetch(t, () => new Response("", { status: 500 }));
  await rejectsWith(pollJob(JOB, { intervalMs: 0 }), "http_500");
  fakeFetch(t, () => Promise.reject(new TypeError("offline")));
  await rejectsWith(pollJob(JOB, { intervalMs: 0 }), "network");
});

test("pollJob gives up after maxPolls and stops when aborted", async (t) => {
  const calls = fakeFetch(t, () => json({ status: "running" }));
  await rejectsWith(pollJob(JOB, { intervalMs: 0, maxPolls: 3 }), "gave_up");
  assert.equal(calls.length, 3);

  const controller = new AbortController();
  const more = fakeFetch(t, () => json({ status: "queued" }));
  const polling = pollJob(JOB, { intervalMs: 10, signal: controller.signal, onStatus: () => controller.abort() });
  await rejectsWith(polling, "cancelled");
  assert.equal(more.length, 1, "no poll after the abort");
});

test("pollJob never builds a URL from a bad job ID", async (t) => {
  const calls = fakeFetch(t, () => json(DONE));
  for (const id of ["../../x", JOB.toUpperCase(), JOB.slice(1), `${JOB}0`, `${JOB}/audio`, "", null]) {
    await rejectsWith(pollJob(id, { intervalMs: 0 }), "bad_response");
    await rejectsWith(fetchAudio(id), "bad_response");
  }
  assert.equal(calls.length, 0);
});

test("pollJob keeps only the documented metadata, checked", async (t) => {
  const long = "é".repeat(150) + "😀".repeat(100);
  const cases = [
    [{ ...DONE, extra: "x", uploader: "someone", url: "x" }, { title: "A Title", duration: 215.5, heatmap: DONE.heatmap }],
    [{ status: "done" }, { title: null, duration: null, heatmap: null }],
    [{ status: "done", title: "  ", duration: 0, heatmap: "x" }, { title: null, duration: null, heatmap: null }],
    [{ status: "done", title: 7, duration: "215", heatmap: {} }, { title: null, duration: null, heatmap: null }],
    [{ status: "done", title: " Padded ", duration: -1 }, { title: "Padded", duration: null, heatmap: null }],
    // Cut to 200 code points, never inside a surrogate pair.
    [{ status: "done", title: long }, { title: "é".repeat(150) + "😀".repeat(50), duration: null, heatmap: null }],
  ];
  for (const [body, expected] of cases) {
    fakeFetch(t, () => json(body));
    assert.deepEqual(await pollJob(JOB, { intervalMs: 0 }), { status: "done", ...expected, windowStart: null });
  }
});

test("fetchAudio fetches the bytes once", async (t) => {
  const calls = fakeFetch(t, () => new Response(new Uint8Array([1, 2, 3, 4]), { headers: { "Content-Type": "audio/mp4" } }));
  const bytes = await fetchAudio(JOB);
  assert.ok(bytes instanceof Uint8Array);
  assert.deepEqual([...bytes], [1, 2, 3, 4]);
  assert.deepEqual(calls, [{ url: `/fetch/api/jobs/${JOB}/audio`, init: { credentials: "same-origin", cache: "no-store" } }]);
});

test("fetchAudio: a 404 is gone, nothing is retried", async (t) => {
  const calls = fakeFetch(t, () => json({ error: "not_found" }, 404));
  await rejectsWith(fetchAudio(JOB), "expired");
  assert.equal(calls.length, 1);
  fakeFetch(t, () => Promise.reject(new TypeError("offline")));
  await rejectsWith(fetchAudio(JOB), "network");
  const broken = new Response(new ReadableStream({
    pull(controller) {
      controller.error(new TypeError("reset"));
    },
  }));
  fakeFetch(t, () => broken);
  await rejectsWith(fetchAudio(JOB), "network");
});

test("every error code has its own words", () => {
  const generic = fetchErrorMessage("zz_unknown");
  for (const code of FETCH_ERROR_CODES) {
    const message = fetchErrorMessage(code);
    assert.notEqual(message, generic, code);
    assert.ok(message.length > 10, code);
    assert.equal(new FetchError(code).message, message);
  }
  for (const code of ["blocked", "live", "not_found", "past_end", "timeout", "failed", "rate_limited", "busy",
    "bad_start", "bad_time"]) {
    assert.ok(FETCH_ERROR_CODES.includes(code), code);
  }
  // A long video is no error any more: the page asks where to start.
  assert.ok(!FETCH_ERROR_CODES.includes("too_long"));
  assert.match(fetchErrorMessage("live"), /live stream/);
  assert.match(fetchErrorMessage("past_end"), /end of the video/);
  assert.match(fetchErrorMessage("bad_time"), /1:23:45/);
  assert.equal(fetchErrorMessage("http_502"), "The server answered with an error (HTTP 502). Try again.");
  // Only http_ and three digits is shown; anything else gets the plain words.
  for (const code of ["http_5023", "http_abc", "<b>", null, undefined, 42, "toString", "__proto__"]) {
    assert.equal(fetchErrorMessage(code), generic, String(code));
  }
});

// --- the size cap on fetched audio ----------------------------------------------

// The fetched audio goes to the same decoder as a picked file, so it gets the
// same cap, checked before the bytes pile up rather than after. The tests pass
// a small cap instead of allocating 64 MiB.
const MIB = 1024 * 1024;
const TOO_LARGE = "audio_too_large";
const CAP = 10_000;
const CHUNK = 1000;
// The tests' own arrays, whatever a test puts in place of the global to count
// what fetch.js allocates.
const Bytes = Uint8Array;

// A body handed out one CHUNK per read and never ahead of the reader
// (highWaterMark 0), so `seen.bytes` is exactly what fetchAudio was given.
// It ends after `total` bytes; a cap that does not work then shows as a
// fetch that succeeds rather than one that never ends.
function countedBody(total) {
  const seen = { bytes: 0, cancelled: false };
  const stream = new ReadableStream({
    pull(controller) {
      const size = Math.min(CHUNK, total - seen.bytes);
      if (size <= 0) {
        controller.close();
        return;
      }
      seen.bytes += size;
      controller.enqueue(new Bytes(size).fill(7));
    },
    cancel() {
      seen.cancelled = true;
    },
  }, { highWaterMark: 0 });
  return { stream, seen };
}

function audioResponse(stream, length = null) {
  const headers = { "Content-Type": "audio/mp4" };
  if (length !== null) headers["Content-Length"] = String(length);
  return new Response(stream, { headers });
}

// A response as a browser without a readable body stream gives it: no `body`
// to read from, only arrayBuffer().
function bufferedResponse(size, { length = null, body = null } = {}) {
  const seen = { reads: 0 };
  const headers = new Headers({ "Content-Type": "audio/mp4" });
  if (length !== null) headers.set("Content-Length", String(length));
  const response = {
    ok: true,
    status: 200,
    headers,
    body,
    arrayBuffer: async () => {
      seen.reads += 1;
      return new Uint8Array(size).fill(7).buffer;
    },
  };
  return { response, seen };
}

test("fetched audio is capped at the page's 64 MiB, with words of its own", () => {
  assert.equal(decodeModule.MAX_INPUT_BYTES, 64 * MIB, "decode.js exports MAX_INPUT_BYTES, 64 MiB");
  assert.ok(FETCH_ERROR_CODES.includes(TOO_LARGE), "fetch.js has a message for " + TOO_LARGE);
  assert.notEqual(fetchErrorMessage(TOO_LARGE), fetchErrorMessage("zz_unknown"));
  assert.match(fetchErrorMessage(TOO_LARGE), /64 MB/);
  assert.equal(new FetchError(TOO_LARGE).message, fetchErrorMessage(TOO_LARGE));
});

test("fetchAudio refuses a Content-Length over the cap without reading the body", async (t) => {
  // The default cap is the page's own.
  const big = countedBody(50 * CAP);
  const calls = fakeFetch(t, () => audioResponse(big.stream, 64 * MIB + 1));
  await rejectsWith(fetchAudio(JOB), TOO_LARGE);
  assert.equal(big.seen.bytes, 0, "the body was read");
  assert.equal(calls.length, 1, "nothing is retried");

  const small = countedBody(50 * CAP);
  fakeFetch(t, () => audioResponse(small.stream, CAP + 1));
  await rejectsWith(fetchAudio(JOB, { maxBytes: CAP }), TOO_LARGE);
  assert.equal(small.seen.bytes, 0, "the body was read");
});

test("a body refused by its Content-Length is cancelled, not left downloading", async (t) => {
  // Refusing without reading would otherwise leave the response open, and the
  // rest of the audio still coming in, after the page has given up on it.
  for (const [length, options] of [[64 * MIB + 1, undefined], [CAP + 1, { maxBytes: CAP }]]) {
    const body = countedBody(50 * CAP);
    fakeFetch(t, () => audioResponse(body.stream, length));
    await rejectsWith(fetchAudio(JOB, options), TOO_LARGE);
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(body.seen.bytes, 0, "the body was read");
    assert.ok(body.seen.cancelled, "the refused body was not cancelled");
  }
});

test("a body that keeps coming is cut off past the cap, holding at most one chunk more", async (t) => {
  // No Content-Length, and one that understates the body: the count of the
  // bytes as they arrive is what holds.
  for (const length of [null, 5, CAP]) {
    const body = countedBody(50 * CAP);
    fakeFetch(t, () => audioResponse(body.stream, length));
    await rejectsWith(fetchAudio(JOB, { maxBytes: CAP }), TOO_LARGE);
    assert.ok(body.seen.bytes > CAP, `refused at ${body.seen.bytes} bytes, before passing the cap`);
    assert.ok(body.seen.bytes <= CAP + CHUNK, `read ${body.seen.bytes} bytes of a ${CAP}-byte cap`);
    await new Promise((resolve) => setImmediate(resolve));
    assert.ok(body.seen.cancelled, "the rest of the body was not cancelled");
  }
});

test("a body of exactly the cap is returned whole, and one byte more is refused", async (t) => {
  for (const length of [null, CAP]) {
    const exact = countedBody(CAP);
    fakeFetch(t, () => audioResponse(exact.stream, length));
    const bytes = await fetchAudio(JOB, { maxBytes: CAP });
    assert.ok(bytes instanceof Uint8Array);
    assert.equal(bytes.length, CAP);
    assert.ok(bytes.every((byte) => byte === 7));
  }
  const over = countedBody(CAP + 1);
  fakeFetch(t, () => audioResponse(over.stream));
  await rejectsWith(fetchAudio(JOB, { maxBytes: CAP }), TOO_LARGE);
});

test("without a readable body stream the size is checked before the bytes are used", async (t) => {
  for (const body of [null, undefined]) {
    const exact = bufferedResponse(CAP, { body });
    fakeFetch(t, () => exact.response);
    const bytes = await fetchAudio(JOB, { maxBytes: CAP });
    assert.equal(bytes.length, CAP, String(body));

    const over = bufferedResponse(CAP + 1, { body });
    fakeFetch(t, () => over.response);
    await rejectsWith(fetchAudio(JOB, { maxBytes: CAP }), TOO_LARGE);

    // The header still refuses before anything is read.
    const declared = bufferedResponse(CAP + 1, { body, length: CAP + 1 });
    fakeFetch(t, () => declared.response);
    await rejectsWith(fetchAudio(JOB, { maxBytes: CAP }), TOO_LARGE);
    assert.equal(declared.seen.reads, 0, "arrayBuffer was called");
  }
  // A body that breaks off is still a network error there.
  const broken = bufferedResponse(CAP);
  broken.response.arrayBuffer = () => Promise.reject(new TypeError("reset"));
  fakeFetch(t, () => broken.response);
  await rejectsWith(fetchAudio(JOB, { maxBytes: CAP }), "network");
});

// Every new buffer fetch.js asks for while a test runs, in bytes. A view on a
// buffer that exists holds nothing more, so it is not counted.
function countAllocations(t) {
  const counted = { bytes: 0 };
  globalThis.Uint8Array = class extends Bytes {
    constructor(...args) {
      super(...args);
      if (typeof args[0] === "number") counted.bytes += args[0];
    }
  };
  t.after(() => {
    globalThis.Uint8Array = Bytes;
  });
  return counted;
}

// A body whose producer overwrites each piece once the next is asked for, as
// one that reuses its buffers would. Only a reader that has copied a piece
// by then still has its bytes: one that keeps the pieces to join them at the
// end finds them overwritten.
function recycledBody(total) {
  let sent = 0;
  let last = null;
  return new ReadableStream({
    pull(controller) {
      if (last !== null) last.fill(9);
      const size = Math.min(CHUNK, total - sent);
      if (size <= 0) {
        controller.close();
        return;
      }
      sent += size;
      last = new Bytes(size).fill(7);
      controller.enqueue(last);
    },
  }, { highWaterMark: 0 });
}

test("a fetched audio is gathered in one buffer, never held twice", async (t) => {
  // Keeping the pieces of a body until its end and then copying them into a
  // buffer of their sum holds the audio twice for a moment: 128 MiB for a
  // download at the cap. The body goes into one buffer as it arrives, as long
  // as its Content-Length says, or as long as the cap where that header is
  // missing or no plain number.
  const allocated = countAllocations(t);
  for (const [size, length, buffer] of [
    [CAP - 500, CAP - 500, CAP - 500],
    [CAP, CAP, CAP],
    [3 * CHUNK + 1, null, CAP],
    [3 * CHUNK + 1, "soon", CAP],
    [0, 0, 0],
  ]) {
    fakeFetch(t, () => audioResponse(recycledBody(size), length));
    allocated.bytes = 0;
    const bytes = await fetchAudio(JOB, { maxBytes: CAP });
    assert.equal(bytes.length, size, `Content-Length ${length}`);
    assert.ok(bytes.every((byte) => byte === 7), `Content-Length ${length}: a piece was kept, not copied, until the end`);
    assert.equal(allocated.bytes, buffer, `Content-Length ${length}: bytes allocated for a body of ${size}`);
  }
});

test("a body longer than its Content-Length is returned whole, inside the cap", async (t) => {
  // A header that understates the body, as after a proxy changed it on the
  // way: the bytes so far move to a buffer of the cap's size, once.
  const allocated = countAllocations(t);
  const body = countedBody(5 * CHUNK);
  fakeFetch(t, () => audioResponse(body.stream, 2 * CHUNK));
  const bytes = await fetchAudio(JOB, { maxBytes: CAP });
  assert.equal(bytes.length, 5 * CHUNK);
  assert.ok(bytes.every((byte) => byte === 7));
  assert.equal(allocated.bytes, 2 * CHUNK + CAP);
  // A body shorter than its header says is what arrived, and no more.
  const short = countedBody(2 * CHUNK);
  fakeFetch(t, () => audioResponse(short.stream, 5 * CHUNK));
  const fewer = await fetchAudio(JOB, { maxBytes: CAP });
  assert.equal(fewer.length, 2 * CHUNK);
  assert.ok(fewer.every((byte) => byte === 7));
});

// --- long videos and windows of them (docs/PLAN.md 6.2) ------------------------------------

test("fetchJob with a start posts exactly {video_id, start}", async (t) => {
  const calls = fakeFetch(t, () => json({ job_id: JOB }, 202));
  assert.equal(await fetchJob(ID, 4500), JOB);
  assert.equal(await fetchJob(ID, 0), JOB);
  assert.equal(await fetchJob(ID, MAX_START_SECONDS), JOB);
  assert.deepEqual(calls.map((call) => call.init.body), [
    `{"video_id":"${ID}","start":4500}`, `{"video_id":"${ID}","start":0}`, `{"video_id":"${ID}","start":359999}`,
  ]);
  // Without one, the body is what it always was.
  assert.equal(await fetchJob(ID, null), JOB);
  assert.equal(calls[3].init.body, `{"video_id":"${ID}"}`);
});

test("fetchJob never sends a start that is no whole number of seconds in range", async (t) => {
  const calls = fakeFetch(t, () => json({ job_id: JOB }, 202));
  for (const start of [-1, MAX_START_SECONDS + 1, 1.5, Number.NaN, Infinity, "90", true, [90], {}]) {
    await rejectsWith(fetchJob(ID, start), "bad_time");
  }
  assert.equal(calls.length, 0);
  assert.equal(MAX_START_SECONDS, 99 * 3600 + 59 * 60 + 59);
});

test("the fetcher's refusal of a start keeps its code", async (t) => {
  fakeFetch(t, () => json({ error: "bad_start" }, 400));
  await rejectsWith(fetchJob(ID, 5), "bad_start");
});

test("pollJob ends at long with the video's length, and there is no audio to fetch", async (t) => {
  const long = { status: "long", title: " A Long One ", duration: 3600, heatmap: DONE.heatmap, window_start: 5 };
  const calls = fakeFetch(t, (url, init, n) => json(n === 1 ? { status: "running" } : long));
  const heard = [];
  const result = await pollJob(JOB, { intervalMs: 0, onStatus: (status) => heard.push(status) });
  // The same cleaned metadata as a finished job's, and nothing about a window.
  assert.deepEqual(result, { status: "long", title: "A Long One", duration: 3600, heatmap: DONE.heatmap });
  assert.deepEqual(heard, ["running"]);
  assert.equal(calls.length, 2);
});

test("long without a length is no answer", async (t) => {
  for (const body of [{ status: "long" }, { status: "long", duration: "3600" }, { status: "long", duration: 0 },
    { status: "long", duration: null, title: "T" }, { status: "LONG", duration: 3600 }]) {
    fakeFetch(t, () => json(body));
    await rejectsWith(pollJob(JOB, { intervalMs: 0 }), "bad_response");
  }
});

test("a window's done says where its audio begins", async (t) => {
  fakeFetch(t, () => json({ ...DONE, window_start: 1229.875 }));
  assert.equal((await pollJob(JOB, { intervalMs: 0 })).windowStart, 1229.875);
  fakeFetch(t, () => json({ ...DONE, window_start: 0 }));
  assert.equal((await pollJob(JOB, { intervalMs: 0 })).windowStart, 0);
  // Anything that is no place in a video is no beginning.
  for (const value of ["1230", -1, true, null, [1230], {}]) {
    fakeFetch(t, () => json({ ...DONE, window_start: value }));
    assert.equal((await pollJob(JOB, { intervalMs: 0 })).windowStart, null, JSON.stringify(value));
  }
});

test("parseStartTime reads a time as a person types it", () => {
  const cases = {
    "0": 0, "90": 90, "0:00": 0, "12:30": 750, "1:23:45": 5025, "83:45": 5025, "01:02:03": 3723,
    // The iPhone's number pad has a point and no colon; a comma or a space works too.
    "12.30": 750, "1.23.45": 5025, "12,30": 750, "12 30": 750, "1 23 45": 5025, "1:23.45": 5025,
    " 1:02:03 ": 3723, "1::02": 62,
    // YouTube's own way of writing it.
    "1h23m45s": 5025, "45s": 45, "2m": 120, "3h": 10800, "1H2M3S": 3723, "1m30": 90,
    "99:59:59": 359999, "359999": 359999,
  };
  for (const [text, seconds] of Object.entries(cases)) {
    assert.equal(parseStartTime(text), seconds, JSON.stringify(text));
  }
});

test("parseStartTime refuses what is no time", () => {
  for (const text of ["", " ", "abc", "1:2:3:4", "-1", "1:-2", ":30", "30:", "1e3", "0x10", "1/2", "１２:３０",
    "100:00:00", "360000", "1234567", "12:30pm", "half past", "30m1h", null, undefined, 90, 12.5, ["1:00"], {}]) {
    assert.equal(parseStartTime(text), null, JSON.stringify(text));
  }
});

test("formatStartTime writes what the start field shows", () => {
  const cases = [[0, "0:00"], [5, "0:05"], [59, "0:59"], [60, "1:00"], [750, "12:30"], [3599, "59:59"],
    [3600, "1:00:00"], [5025, "1:23:45"], [36000, "10:00:00"], [359999, "99:59:59"], [12.9, "0:12"],
    [-5, "0:00"], [Number.NaN, "0:00"]];
  for (const [seconds, text] of cases) assert.equal(formatStartTime(seconds), text, String(seconds));
  // What the page writes into the field, it reads back as the same second.
  for (const seconds of [0, 1, 59, 60, 61, 3599, 3600, 3661, 86399, 359999]) {
    assert.equal(parseStartTime(formatStartTime(seconds)), seconds);
  }
});
