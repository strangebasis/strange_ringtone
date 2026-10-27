// The trimmer's wiring (core/static/app.js), run against a fake DOM built from
// the real index.html. The pure modules have their own tests; these pin what
// app.js adds on top: the caps it hands them, the loudness controls and the
// preview gain they drive, the one playback context, the render request built
// from the controls, the share click, the YouTube box and the #yt= fragment,
// and what a render or fetch that went stale shows.
import { test } from "node:test";
import assert from "node:assert/strict";

import { integratedLufs } from "../../core/static/audio/loudness.js";
import { fetchErrorMessage } from "../../core/static/fetch.js";
import { dbToGain } from "../../core/static/player.js";
import { resultUrl, ShareError } from "../../core/static/share.js";
import { loadApp, pickFile, setDecoded, settle, sleep, wavShape } from "./fake_dom.mjs";

const RATE = 44100;
const RESULT_ID = "0123456789abcdef0123456789abcdef";
const DISPOSITION = "attachment; filename=\"Server Name.m4a\"; filename*=UTF-8''Server%20Name.m4a";

// A mono 440 Hz sine in [seconds, amplitude] sections.
function sections(...parts) {
  const total = parts.reduce((sum, [seconds]) => sum + Math.round(seconds * RATE), 0);
  const out = new Float32Array(total);
  let at = 0;
  for (const [seconds, amplitude] of parts) {
    const n = Math.round(seconds * RATE);
    for (let i = 0; i < n; i += 1) out[at + i] = amplitude * Math.sin((2 * Math.PI * 440 * (at + i)) / RATE);
    at += n;
  }
  return out;
}

// 20 s at -8 dB, 20 s loud, 20 s at -8 dB: the loud part is longer than a
// 15 s cap, and the quiet parts are within 10 LU, so they count in a
// whole-file measurement and move it well away from the selection's.
const SONG = sections([20, 0.2], [20, 0.5], [20, 0.2]);

function json(body, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function m4a() {
  return new Response(new Uint8Array([0, 1, 2, 3]), { headers: { "Content-Disposition": DISPOSITION } });
}

// The server, answering at once unless a test holds the result (hold) or the
// render's answer (holdRender) back.
function server(hold = null, { holdRender = null, resultId = RESULT_ID } = {}) {
  return (url) => {
    if (url.startsWith("/api/render?")) {
      if (holdRender !== null) return holdRender;
      return Promise.resolve(json({ result_id: resultId, duration: 15, measured_lufs: -9.5, applied_gain_db: -4.5 }));
    }
    if (url === `/api/result/${RESULT_ID}.m4a`) return hold === null ? Promise.resolve(m4a()) : hold;
    return Promise.resolve(json({ error: "not_found" }, 404));
  };
}

async function renderNow(page) {
  page.el("render").dispatch("click");
  await settle();
}

function postedFrames(page) {
  const render = page.fetchCalls.find((call) => call.url.startsWith("/api/render?"));
  assert.ok(render, "no render request");
  return wavShape(render.init.body).frames;
}

test("a nudge past the cap pulls the other edge along, and the WAV has exactly the cap's frames", async () => {
  const page = await loadApp({ maxClipSeconds: "15", fetch: server() });
  await pickFile(page, "My Song.mp3", [SONG]);
  const before = page.el("selection-readout").textContent;
  await page.nudge("end", "1");
  const after = page.el("selection-readout").textContent;
  assert.notEqual(after, before);
  assert.match(after, /15\.0 s \(the maximum\)$/);
  await renderNow(page);
  assert.equal(postedFrames(page), 15 * RATE);
});

test("the selection never gets shorter than half a second", async () => {
  const page = await loadApp({ maxClipSeconds: "15", fetch: server() });
  await pickFile(page, "My Song.mp3", [SONG]);
  for (let i = 0; i < 20; i += 1) await page.nudge("end", "-1");
  assert.match(page.el("selection-readout").textContent, /, 0\.5 s$/);
  await renderNow(page);
  assert.equal(postedFrames(page), RATE / 2);
});

test("the meter measures the selection, not the whole file", async () => {
  const page = await loadApp({ maxClipSeconds: "15", fetch: server() });
  await pickFile(page, "My Song.mp3", [SONG]);
  await sleep(300);
  const match = /measures (-?\d+\.\d) LUFS/.exec(page.el("meter-readout").textContent);
  assert.ok(match, page.el("meter-readout").textContent);
  const loudPart = integratedLufs([SONG.subarray(20 * RATE, 35 * RATE)], RATE);
  const wholeFile = integratedLufs([SONG], RATE);
  // The suggestion may start up to 1 s into the quieter lead-in.
  assert.ok(Math.abs(Number(match[1]) - loudPart) <= 0.5, `${match[1]} vs ${loudPart}`);
  assert.ok(Math.abs(wholeFile - loudPart) > 2, "the test signal does not tell the two apart");
});

test("the loudness warning shows only above -11 LUFS with levelling on", async () => {
  const page = await loadApp({ fetch: server() });
  const target = page.el("target");
  const warning = page.el("target-warning");
  assert.equal(warning.hidden, true);
  for (const [value, hidden] of [["-11", true], ["-10.9", false], ["-9", false], ["-14", true]]) {
    target.value = value;
    target.dispatch("input");
    assert.equal(warning.hidden, hidden, `target ${value}`);
  }
  target.value = "-9";
  page.el("normalize").checked = false;
  page.el("normalize").dispatch("input");
  assert.equal(warning.hidden, true, "levelling off");
});

test("Reset puts the slider back on data-default-target-lufs", async () => {
  const page = await loadApp({ defaultTarget: "-16.0", fetch: server() });
  const target = page.el("target");
  assert.equal(target.value, "-16");
  assert.equal(page.el("target-value").textContent, "-16.0 LUFS (baseline)");
  assert.equal(page.el("target-reset").disabled, true);
  target.value = "-12";
  target.dispatch("input");
  assert.equal(page.el("target-reset").disabled, false);
  page.el("target-reset").dispatch("click");
  assert.equal(target.value, "-16");
  assert.equal(page.el("target-value").textContent, "-16.0 LUFS (baseline)");
  assert.equal(page.el("target-reset").disabled, true);
});

test("a baseline with more than one decimal is rounded to the slider's step", async () => {
  // The server accepts DEFAULT_TARGET_LUFS=-14.25; the 0.1 LU slider cannot.
  const page = await loadApp({ defaultTarget: "-14.25", fetch: server() });
  const target = page.el("target");
  assert.equal(target.value, "-14.2");
  assert.equal(page.el("target-baseline").value, "-14.2");
  assert.equal(page.el("target-value").textContent, "-14.2 LUFS (baseline)");
  assert.equal(page.el("target-reset").disabled, true);
  assert.equal(Number(page.el("target-marker").style["--at"]), (-14.2 + 24) / 15);
  target.value = "-12";
  target.dispatch("input");
  page.el("target-reset").dispatch("click");
  assert.equal(page.el("target-value").textContent, "-14.2 LUFS (baseline)");
  assert.equal(page.el("target-reset").disabled, true);
});

test("Hear final posts the selection as a Uint8Array WAV with the fixed query and the name header", async () => {
  const page = await loadApp({ maxClipSeconds: "15", fetch: server(), navigator: { canShare: () => true, share: () => Promise.resolve() } });
  await pickFile(page, "My Song.mp3", [SONG]);
  await renderNow(page);
  const [render, result] = page.fetchCalls;
  assert.equal(render.url, "/api/render?fade_in=0.200&fade_out=1.500&normalize=true&target_lufs=-14.0");
  assert.equal(render.init.method, "POST");
  // A fixed body gets a Content-Length; a stream would be sent chunked, which
  // the server refuses.
  assert.ok(render.init.body instanceof Uint8Array);
  assert.equal(render.init.duplex, undefined);
  assert.equal(render.init.credentials, "same-origin");
  assert.deepEqual(render.init.headers, { "Content-Type": "audio/wav", "X-Tone-Name": "My%20Song" });
  assert.equal(result.url, `/api/result/${RESULT_ID}.m4a`);
  assert.equal(page.fetchCalls.length, 2, "the result is fetched once");
  assert.equal(page.el("result-info").hidden, false);
  assert.equal(page.el("result-duration").textContent, "15.00 s");
  assert.equal(page.el("result-lufs").textContent, "-9.5 LUFS");
  assert.equal(page.el("result-gain").textContent, "-4.5 dB");
  assert.equal(page.el("deliver").hidden, false);
  assert.equal(page.el("download-m4r").href, `/api/result/${RESULT_ID}.m4r`);
  assert.equal(page.el("fallback").href, `/api/result/${RESULT_ID}.m4a`);
  assert.equal(page.el("fallback").hidden, true);
  assert.equal(page.el("share").disabled, false);
  assert.equal(page.el("share-status").textContent, "Share sheet: available");
});

test("Get on this iPhone calls navigator.share inside the click, before anything is awaited", async () => {
  const shared = [];
  const navigator = { canShare: () => true, share: (data) => { shared.push(data); return Promise.resolve(); } };
  const page = await loadApp({ maxClipSeconds: "15", fetch: server(), navigator });
  await pickFile(page, "My Song.mp3", [SONG]);
  await renderNow(page);
  page.el("share").dispatch("click");
  // Checked before yielding even once: a call after an await would come later.
  assert.equal(shared.length, 1);
  // Only the file: a title or text would be shared alongside it, and some
  // targets then take the text instead of the tone.
  assert.deepEqual(Object.keys(shared[0]), ["files"]);
  assert.equal(shared[0].files.length, 1);
  const [file] = shared[0].files;
  assert.equal(file.name, "Server Name.m4a");
  assert.equal(file.type, "audio/mp4");
  assert.equal(file.size, 4);
});

test("a cancelled share is silent; any other failure reveals the download link", async () => {
  for (const [name, fallbackHidden] of [["AbortError", true], ["NotAllowedError", false]]) {
    const navigator = {
      canShare: () => true,
      share: () => Promise.reject(Object.assign(new Error("share"), { name })),
    };
    const page = await loadApp({ maxClipSeconds: "15", fetch: server(), navigator });
    await pickFile(page, "My Song.mp3", [SONG]);
    await renderNow(page);
    page.el("share").dispatch("click");
    await settle();
    assert.equal(page.el("fallback").hidden, fallbackHidden, name);
    if (!fallbackHidden) assert.match(page.el("share-status").textContent, /failed \(NotAllowedError\)/);
  }
});

test("without a share sheet for the file, the download link stands in", async () => {
  const page = await loadApp({ maxClipSeconds: "15", fetch: server(), navigator: { canShare: () => false, share: () => Promise.resolve() } });
  await pickFile(page, "My Song.mp3", [SONG]);
  await renderNow(page);
  assert.equal(page.el("share").hidden, true);
  assert.equal(page.el("fallback").hidden, false);
  assert.equal(page.el("share-status").textContent, "Share sheet: not available");
});

test("a change while the result downloads leaves no stale numbers on the page", async () => {
  let release;
  const hold = new Promise((resolve) => { release = resolve; });
  const page = await loadApp({ maxClipSeconds: "15", fetch: server(hold), navigator: { canShare: () => true, share: () => Promise.resolve() } });
  await pickFile(page, "My Song.mp3", [SONG]);
  page.el("render").dispatch("click");
  await settle();
  assert.equal(page.fetchCalls.length, 2, "the result fetch should be in flight");
  page.el("name").value = "Another name";
  page.el("name").dispatch("input");
  release(m4a());
  await settle();
  assert.equal(page.el("render-status").textContent, "Changed while rendering. Tap Hear final to render it again.");
  assert.equal(page.el("result-info").hidden, true);
  assert.equal(page.el("deliver").hidden, true);
});

const SHARE_SHEET = { canShare: () => true, share: () => Promise.resolve() };

test("Hear final without a Play first creates and resumes the one AudioContext and plays the tone", async () => {
  const page = await loadApp({ maxClipSeconds: "15", fetch: server(), navigator: SHARE_SHEET });
  await pickFile(page, "My Song.mp3", [SONG]);
  // Decoding uses an OfflineAudioContext: no live context before a gesture.
  assert.equal(page.contexts().length, 0);
  await renderNow(page);
  assert.equal(page.contexts().length, 1);
  const [context] = page.contexts();
  assert.equal(context.resumes, 1, "resumed inside the click, as iOS requires");
  assert.equal(context.decoded.length, 1, "the fetched tone is decoded on the playback context");
  const final = context.sources.filter((source) => source.loop);
  assert.equal(final.length, 1);
  assert.equal(final[0].buffer, context.decoded[0]);
  assert.equal(final[0].starts.length, 1);
  assert.equal(page.el("render-status").textContent, "Playing the finished tone on a loop.");
  assert.equal(page.el("render").textContent, "Stop final");
  // Play selection afterwards reuses the same context.
  page.el("play").dispatch("click");
  page.el("play").dispatch("click");
  assert.equal(page.contexts().length, 1);
});

test("the Softer-Louder slider moves the preview gain live, and levelling off plays at 0 dB", async () => {
  const page = await loadApp({ maxClipSeconds: "15", fetch: server() });
  await pickFile(page, "My Song.mp3", [SONG]);
  await sleep(300);
  page.el("play").dispatch("click");
  const [context] = page.contexts();
  // The second GainNode is the loudness gain (player.js).
  const level = context.gains[1].gain;
  const atBaseline = level.lastValue();
  assert.ok(Math.abs(20 * Math.log10(atBaseline)) > 1, "the test signal needs a preview gain away from 0 dB");

  const target = page.el("target");
  target.value = "-20";
  target.dispatch("input");
  assert.equal(level.calls[level.calls.length - 1].method, "setTargetAtTime");
  // 6 LU softer is 6 dB less gain, applied while the preview plays.
  const ratio = level.lastValue() / atBaseline;
  assert.ok(Math.abs(ratio - dbToGain(-6)) < 1e-9, `${ratio}`);

  const normalize = page.el("normalize");
  normalize.checked = false;
  normalize.dispatch("input");
  assert.equal(level.lastValue(), 1, "levelling off: 0 dB");
  // A preview started with levelling off starts at 0 dB too.
  page.el("play").dispatch("click");
  page.el("play").dispatch("click");
  assert.equal(page.el("play").textContent, "Stop");
  assert.equal(level.lastValue(), 1);
  page.el("play").dispatch("click");
});

test("Hear final sends what the fade and loudness controls say", async () => {
  const page = await loadApp({ maxClipSeconds: "15", fetch: server(), navigator: SHARE_SHEET });
  await pickFile(page, "My Song.mp3", [SONG]);
  for (const id of ["fade-in-on", "fade-out-on", "normalize"]) {
    page.el(id).checked = false;
    page.el(id).dispatch("input");
  }
  await renderNow(page);
  assert.equal(page.fetchCalls[0].url, "/api/render?fade_in=0.000&fade_out=0.000&normalize=false&target_lufs=-14.0");

  // Back on, with the sliders moved: the slider values are sent.
  const next = await loadApp({ maxClipSeconds: "15", fetch: server(), navigator: SHARE_SHEET });
  await pickFile(next, "My Song.mp3", [SONG]);
  next.el("fade-in-on").checked = false;
  next.el("fade-in-on").dispatch("input");
  next.el("fade-out").value = "3";
  next.el("fade-out").dispatch("input");
  next.el("target").value = "-12.5";
  next.el("target").dispatch("input");
  await renderNow(next);
  assert.equal(next.fetchCalls[0].url, "/api/render?fade_in=0.000&fade_out=3.000&normalize=true&target_lufs=-12.5");
});

test("the YouTube place shows only when data-fetch-enabled is exactly 'true'", async () => {
  for (const [value, hidden] of [["false", true], ["true", false], ["yes", true], ["", true], ["TRUE", true]]) {
    const page = await loadApp({ fetchEnabled: value, fetch: server() });
    assert.equal(page.el("youtube").hidden, hidden, JSON.stringify(value));
  }
});

test("a result ID that is not 32 lower-case hex digits is never built into a URL", async () => {
  assert.equal(resultUrl(RESULT_ID, "m4r"), `/api/result/${RESULT_ID}.m4r`);
  for (const id of ["../../x", RESULT_ID.toUpperCase(), RESULT_ID.slice(1), RESULT_ID + "0", `${RESULT_ID}/x`, ""]) {
    assert.throws(() => resultUrl(id, "m4a"), ShareError, id);
  }
  const page = await loadApp({ maxClipSeconds: "15", fetch: server(null, { resultId: "../../x" }) });
  await pickFile(page, "My Song.mp3", [SONG]);
  await renderNow(page);
  assert.equal(page.fetchCalls.length, 1, "nothing is fetched with that ID");
  assert.equal(page.el("render-status").textContent,
    "The server sent an answer this page does not understand. Reload the page and try again.");
  assert.equal(page.el("deliver").hidden, true);
});

test("a nudge moves an edge by exactly its step in frames", async () => {
  for (const [edge, step, frames] of [["start", "0.1", 15 * RATE - 4410], ["end", "-0.1", 15 * RATE - 4410],
    ["end", "-1", 14 * RATE]]) {
    const page = await loadApp({ maxClipSeconds: "15", fetch: server(), navigator: SHARE_SHEET });
    await pickFile(page, "My Song.mp3", [SONG]);
    await page.nudge(edge, step);
    await renderNow(page);
    assert.equal(postedFrames(page), frames, `${edge} ${step}`);
  }
});

test("hiding the page stops a looping preview", async () => {
  const page = await loadApp({ maxClipSeconds: "15", fetch: server() });
  await pickFile(page, "My Song.mp3", [SONG]);
  page.el("play").dispatch("click");
  const [context] = page.contexts();
  // Becoming visible is not a reason to stop.
  page.fireDocument("visibilitychange");
  assert.equal(page.el("play").textContent, "Stop");
  document.hidden = true;
  page.fireDocument("visibilitychange");
  assert.equal(page.el("play").textContent, "Play selection");
  assert.ok(context.sources.length > 0 && context.sources.every((source) => source.stopped));
});

test("a change while the render runs means its result is never fetched", async () => {
  let release;
  const holdRender = new Promise((resolve) => { release = resolve; });
  const page = await loadApp({ maxClipSeconds: "15", fetch: server(null, { holdRender }), navigator: SHARE_SHEET });
  await pickFile(page, "My Song.mp3", [SONG]);
  page.el("render").dispatch("click");
  await settle();
  assert.equal(page.fetchCalls.length, 1, "the render should be in flight");
  page.el("name").value = "Another name";
  page.el("name").dispatch("input");
  release(json({ result_id: RESULT_ID, duration: 15, measured_lufs: -9.5, applied_gain_db: -4.5 }));
  await settle();
  assert.equal(page.fetchCalls.length, 1, "a stale render's result is not fetched");
  assert.equal(page.el("result-info").hidden, true);
  assert.equal(page.el("render-status").textContent, "Changed while rendering. Tap Hear final to render it again.");
});

test("a tone finished while the page is hidden waits for Hear final instead of playing", async () => {
  const page = await loadApp({ maxClipSeconds: "15", fetch: server(), navigator: SHARE_SHEET });
  await pickFile(page, "My Song.mp3", [SONG]);
  document.hidden = true;
  await renderNow(page);
  const [context] = page.contexts();
  assert.equal(context.sources.filter((source) => source.loop).length, 0, "nothing plays in a hidden tab");
  assert.equal(page.el("render-status").textContent, "The tone is ready. Tap Hear final to play it.");
  assert.equal(page.el("render").textContent, "Hear final");
  assert.equal(page.el("deliver").hidden, false, "the result is kept for the links and the share sheet");
  const renders = page.fetchCalls.filter((call) => call.url.startsWith("/api/render")).length;
  // Back in view, Hear final plays the kept tone without another render.
  document.hidden = false;
  page.el("render").dispatch("click");
  assert.equal(context.sources.filter((source) => source.loop).length, 1);
  assert.equal(page.el("render").textContent, "Stop final");
  assert.equal(page.fetchCalls.filter((call) => call.url.startsWith("/api/render")).length, renders);
});

test("expired downloads go, the tone and the share sheet stay, and Hear final makes new downloads", async () => {
  const page = await loadApp({ maxClipSeconds: "15", resultTtlSeconds: "1", fetch: server(), navigator: SHARE_SHEET });
  await pickFile(page, "My Song.mp3", [SONG]);
  await renderNow(page);
  assert.equal(page.el("render").textContent, "Stop final");
  assert.equal(page.el("download-m4r").href, `/api/result/${RESULT_ID}.m4r`);
  await sleep(1300);
  assert.equal(page.el("render-status").textContent,
    "The download links have expired on the server. Tap Hear final to make the tone again.");
  // The links are gone; the file in memory is not.
  assert.equal(page.el("download-m4r").hidden, true);
  assert.equal(page.el("download-m4r").href, undefined, "no link to a result that is gone");
  assert.equal(page.el("fallback").hidden, true);
  assert.equal(page.el("deliver").hidden, false);
  assert.equal(page.el("share").disabled, false, "the share sheet still has the file");
  assert.equal(page.el("render").textContent, "Stop final", "playback goes on");
  const [context] = page.contexts();
  assert.equal(context.sources.filter((source) => source.loop && !source.stopped).length, 1);
  // Stop, then Hear final renders again instead of replaying.
  page.el("render").dispatch("click");
  assert.equal(page.el("render").textContent, "Hear final");
  await renderNow(page);
  assert.equal(page.fetchCalls.filter((call) => call.url.startsWith("/api/render")).length, 2, "rendered again");
  assert.equal(page.el("render-status").textContent, "Playing the finished tone on a loop.");
  assert.equal(page.el("download-m4r").hidden, false);
  assert.equal(page.el("download-m4r").href, `/api/result/${RESULT_ID}.m4r`);
});

test("a TTL longer than a timer can hold does not expire the tone at once", async () => {
  // 40 days in milliseconds is past 2^31 - 1, which a single setTimeout wraps.
  const page = await loadApp({ maxClipSeconds: "15", resultTtlSeconds: String(40 * 24 * 3600), fetch: server(),
    navigator: SHARE_SHEET });
  await pickFile(page, "My Song.mp3", [SONG]);
  await renderNow(page);
  await sleep(60);
  assert.equal(page.el("render-status").textContent, "Playing the finished tone on a loop.");
  assert.equal(page.el("download-m4r").hidden, false);
  assert.equal(page.el("render").textContent, "Stop final");
  // Waited in slices a timer can hold, not re-armed every millisecond after
  // the browser cut an overflowing delay down to one.
  const delays = page.timerDelays().filter((ms) => ms > 1000);
  assert.ok(delays.length >= 1 && delays.length <= 2, "one long timer, not a loop: " + page.timerDelays().length);
  assert.ok(delays.every((ms) => ms <= 2 ** 31 - 1), "within the 32-bit limit");
});

test("the expiry clock starts with the render, not after the tone was fetched", async () => {
  let release;
  const hold = new Promise((resolve) => { release = resolve; });
  // The server stored the result at the render; the fetch of it takes 0.8 s
  // here, and the TTL is 1 s, so the downloads are gone 1 s after the click.
  const page = await loadApp({ maxClipSeconds: "15", resultTtlSeconds: "1", fetch: server(hold), navigator: SHARE_SHEET });
  await pickFile(page, "My Song.mp3", [SONG]);
  page.el("render").dispatch("click");
  await sleep(800);
  release(m4a());
  await settle();
  assert.equal(page.el("render").textContent, "Stop final");
  await sleep(600);
  assert.equal(page.el("download-m4r").hidden, true, "expired 1.4 s after the click, not 1.8 s");
});

test("a blank or unreadable baseline falls back to -14, not to the loudest setting", async () => {
  for (const defaultTarget of ["", "loud"]) {
    const page = await loadApp({ defaultTarget, fetch: server() });
    assert.equal(page.el("target").value, "-14");
    assert.equal(page.el("target-value").textContent, "-14.0 LUFS (baseline)");
  }
});

test("a new file frees Hear final at once, and the old render's end says nothing about it", async () => {
  let release;
  const holdRender = new Promise((resolve) => { release = resolve; });
  // Only the first render is held; a Response can be read once.
  let renders = 0;
  const plain = server();
  const fetch = (url, init) => {
    if (url.startsWith("/api/render?")) {
      renders += 1;
      if (renders === 1) return holdRender;
    }
    return plain(url, init);
  };
  const page = await loadApp({ maxClipSeconds: "15", fetch, navigator: SHARE_SHEET });
  await pickFile(page, "My Song.mp3", [SONG]);
  page.el("render").dispatch("click");
  await settle();
  assert.equal(page.el("render").disabled, true, "rendering");
  await pickFile(page, "Other.mp3", [SONG]);
  assert.equal(page.el("render").disabled, false, "the new clip can be rendered");
  assert.equal(page.el("render").textContent, "Hear final");
  assert.equal(page.el("render-status").textContent, "");
  release(json({ result_id: RESULT_ID, duration: 15, measured_lufs: -9.5, applied_gain_db: -4.5 }));
  await settle();
  await sleep(20);
  assert.equal(page.el("render-status").textContent, "", "the abandoned render leaves the new file's line alone");
  assert.equal(page.el("result-info").hidden, true);
  assert.equal(page.el("render").disabled, false);
  // The new clip renders as usual.
  await renderNow(page);
  assert.equal(page.el("render").textContent, "Stop final");
  assert.equal(page.el("render-status").textContent, "Playing the finished tone on a loop.");
});

// --- YouTube ------------------------------------------------------------------------

const VIDEO_ID = "dQw4w9WgXcQ";
const JOB_ID = "fedcba9876543210fedcba9876543210";
const AUDIO_BYTES = [9, 8, 7, 6, 5];
// 30 s of digital silence, 15 s loud, 15 s of silence: rule 3 would start at
// about 30 s, and a start put anywhere in the silence stays exactly there.
const LOUD_AT_30 = sections([30, 0], [15, 0.5], [15, 0]);
const TITLE = "Never Gonna (Live).Remix";
const DONE = { status: "done", title: TITLE, duration: 60, heatmap: null };

// The fetcher behind /fetch/, answering in order from `statuses` (the last
// one repeats), or with `firstStatus` (a promise a test holds) to the first
// poll; the core's routes answer as server() does.
function fetcher({ statuses = [DONE], post = null, firstStatus = null } = {}) {
  let polls = 0;
  const core = server();
  return (url, init) => {
    if (url === "/fetch/api/jobs") return post ?? Promise.resolve(json({ job_id: JOB_ID }, 202));
    if (url === `/fetch/api/jobs/${JOB_ID}` && polls === 0 && firstStatus !== null) {
      polls += 1;
      return firstStatus;
    }
    if (url === `/fetch/api/jobs/${JOB_ID}`) {
      const status = statuses[Math.min(polls, statuses.length - 1)];
      polls += 1;
      return Promise.resolve(json(status));
    }
    if (url === `/fetch/api/jobs/${JOB_ID}/audio`) {
      return Promise.resolve(new Response(new Uint8Array(AUDIO_BYTES), { headers: { "Content-Type": "audio/mp4" } }));
    }
    return core(url, init);
  };
}

async function until(check, label, ms = 3000) {
  const deadline = Date.now() + ms;
  while (!check()) {
    if (Date.now() > deadline) assert.fail("timed out waiting for " + label);
    await sleep(5);
  }
}

const fetchUrls = (page) => page.fetchCalls.map((call) => call.url);
const fragment = (link) => "#yt=" + encodeURIComponent(link);

test("#yt= is read once, cleared before any request, and only the ID is posted", async () => {
  setDecoded([LOUD_AT_30]);
  const link = `https://youtu.be/${VIDEO_ID}?si=TrackingToken123&t=10`;
  const page = await loadApp({ maxClipSeconds: "15", fetchEnabled: "true", hash: fragment(link), search: "?a=1",
    fetch: fetcher(), navigator: SHARE_SHEET });
  // Cleared at start-up, before the first request, keeping path and query.
  assert.deepEqual(page.events[0], { type: "replaceState", url: "/?a=1" });
  assert.equal(page.events.filter((event) => event.type === "replaceState").length, 1);
  assert.equal(page.location.hash, "");
  await until(() => !page.el("editor").hidden, "the editor");

  assert.deepEqual(fetchUrls(page), ["/fetch/api/jobs", `/fetch/api/jobs/${JOB_ID}`, `/fetch/api/jobs/${JOB_ID}/audio`]);
  const post = page.fetchCalls[0].init;
  assert.equal(post.method, "POST");
  assert.equal(post.body, `{"video_id":"${VIDEO_ID}"}`);
  // Neither the tracking token nor the timestamp leaves the device.
  for (const { url, init } of page.fetchCalls) {
    const sent = url + " " + JSON.stringify(init ?? {});
    assert.ok(!sent.includes("TrackingToken123") && !sent.includes("t=10") && !sent.includes("youtu"), sent);
  }
  // The fetched bytes, once, are what was decoded.
  assert.deepEqual(page.decodedInputs().map((bytes) => [...bytes]), [AUDIO_BYTES]);
  assert.equal(page.el("yt-link").value, link);
  // Rule 1: the selection starts at t=10, in silence, so the snap keeps it.
  assert.equal(page.el("selection-readout").textContent, "0:10.0 to 0:25.0, 15.0 s (the maximum)");
  assert.equal(page.el("yt-status").textContent,
    `${TITLE}, 1:00.0. The selection starts at the link's timestamp; drag the handles to change it.`);
  assert.equal(page.el("yt-status").classes.has("error"), false);
});

test("a #yt= that arrives while the page is open is read the same way", async () => {
  setDecoded([LOUD_AT_30]);
  const page = await loadApp({ maxClipSeconds: "15", fetchEnabled: "true", search: "?a=1", fetch: fetcher() });
  await settle();
  assert.deepEqual(page.events, []);
  // What a browser does when a tab at the app's address is sent the same
  // address plus a fragment: the fragment changes, and nothing reloads.
  const link = `https://youtu.be/${VIDEO_ID}?t=10`;
  page.location.hash = fragment(link);
  page.fireWindow("hashchange");
  // Cleared before the first request, as at start-up.
  assert.deepEqual(page.events[0], { type: "replaceState", url: "/?a=1" });
  assert.equal(page.location.hash, "");
  await until(() => !page.el("editor").hidden, "the editor");
  assert.deepEqual(fetchUrls(page), ["/fetch/api/jobs", `/fetch/api/jobs/${JOB_ID}`, `/fetch/api/jobs/${JOB_ID}/audio`]);
  assert.equal(page.fetchCalls[0].init.body, `{"video_id":"${VIDEO_ID}"}`);
  assert.equal(page.el("yt-link").value, link);
  assert.equal(page.el("selection-readout").textContent, "0:10.0 to 0:25.0, 15.0 s (the maximum)");
  // Any other fragment is left alone.
  page.location.hash = "#other";
  page.fireWindow("hashchange");
  await settle();
  assert.equal(page.events.filter((event) => event.type === "replaceState").length, 1);
  assert.equal(page.location.hash, "#other");
  assert.equal(page.fetchCalls.length, 3);
});

test("with fetching disabled a #yt= that arrives later is cleared and fetches nothing", async () => {
  const page = await loadApp({ fetchEnabled: "false", fetch: fetcher() });
  page.location.hash = fragment(`https://youtu.be/${VIDEO_ID}`);
  page.fireWindow("hashchange");
  await settle();
  assert.deepEqual(page.events, [{ type: "replaceState", url: "/" }]);
  assert.equal(page.el("file-status").textContent,
    "YouTube fetching is not enabled on this server. Pick an audio file instead.");
});

test("the fetched audio is decoded as a File named <title>.m4a of type audio/mp4", async () => {
  setDecoded([LOUD_AT_30]);
  const page = await loadApp({ maxClipSeconds: "15", fetchEnabled: "true", fetch: fetcher() });
  page.el("yt-link").value = VIDEO_ID;
  page.el("yt-fetch").dispatch("click");
  await until(() => !page.el("editor").hidden, "the editor");
  const [file] = page.files();
  assert.equal(file.name, `${TITLE}.m4a`);
  assert.equal(file.type, "audio/mp4");
  assert.deepEqual([...new Uint8Array(await file.arrayBuffer())], AUDIO_BYTES);
});

test("the title becomes the name exactly, and goes only into the name header", async () => {
  setDecoded([LOUD_AT_30]);
  const page = await loadApp({ maxClipSeconds: "15", fetchEnabled: "true", fetch: fetcher(), navigator: SHARE_SHEET });
  page.el("yt-link").value = `https://www.youtube.com/watch?v=${VIDEO_ID}`;
  page.el("yt-fetch").dispatch("click");
  await until(() => !page.el("editor").hidden, "the editor");
  // Not displayName(TITLE), which would cut ".Remix" off as an extension.
  assert.equal(page.el("name").value, TITLE);
  await renderNow(page);
  const render = page.fetchCalls.find((call) => call.url.startsWith("/api/render?"));
  assert.equal(render.init.headers["X-Tone-Name"], encodeURIComponent(TITLE));
  for (const url of fetchUrls(page)) assert.ok(!url.includes("Never") && !url.includes("Gonna"), url);
});

test("without a title the name is left empty for the server's default", async () => {
  setDecoded([LOUD_AT_30]);
  const page = await loadApp({ maxClipSeconds: "15", fetchEnabled: "true",
    fetch: fetcher({ statuses: [{ status: "done" }] }) });
  page.el("yt-link").value = VIDEO_ID;
  page.el("yt-fetch").dispatch("click");
  await until(() => !page.el("editor").hidden, "the editor");
  assert.equal(page.el("name").value, "");
  assert.match(page.el("yt-status").textContent, /^The video, 1:00\.0\. The selection starts on the loudest stretch;/);
});

test("the heatmap places the selection (rule 2), and each state is shown while polling", async () => {
  setDecoded([LOUD_AT_30]);
  const heatmap = Array.from({ length: 60 }, (_, i) => ({ start: i, end: i + 1, value: i >= 5 && i < 20 ? 1 : 0.1 }));
  const statuses = [{ status: "queued" }, { status: "running" }, { ...DONE, heatmap }];
  const page = await loadApp({ maxClipSeconds: "15", fetchEnabled: "true", fetch: fetcher({ statuses }) });
  page.el("yt-link").value = `  https://www.youtube.com/shorts/${VIDEO_ID}?feature=share `;
  page.el("yt-link").dispatch("keydown", { key: "Enter" });
  await until(() => page.fetchCalls.length === 2, "the first poll");
  await settle();
  assert.equal(page.el("yt-status").textContent, "Waiting in the queue…");
  // The next poll comes 2 s later.
  await sleep(1500);
  assert.equal(page.fetchCalls.length, 2);
  await until(() => page.fetchCalls.length === 3, "the second poll");
  await settle();
  assert.equal(page.el("yt-status").textContent, "Downloading…");
  await until(() => !page.el("editor").hidden, "the editor", 4000);
  assert.equal(page.el("selection-readout").textContent, "0:05.0 to 0:20.0, 15.0 s (the maximum)");
  assert.match(page.el("yt-status").textContent, /The selection is on the most replayed stretch;/);
});

test("other keys in the link field do nothing", async () => {
  const page = await loadApp({ fetchEnabled: "true", fetch: fetcher() });
  page.el("yt-link").value = VIDEO_ID;
  page.el("yt-link").dispatch("keydown", { key: "a" });
  await settle();
  assert.equal(page.fetchCalls.length, 0);
});

test("with fetching disabled no /fetch/ request is ever made", async () => {
  for (const value of ["false", "", "TRUE", "yes"]) {
    const page = await loadApp({ fetchEnabled: value, hash: fragment(`https://youtu.be/${VIDEO_ID}`), fetch: fetcher() });
    // Still cleared: the link has no business staying in the address.
    assert.deepEqual(page.events, [{ type: "replaceState", url: "/" }]);
    assert.equal(page.el("file-status").textContent,
      "YouTube fetching is not enabled on this server. Pick an audio file instead.");
    page.el("yt-link").value = VIDEO_ID;
    page.el("yt-fetch").dispatch("click");
    page.el("yt-link").dispatch("keydown", { key: "Enter" });
    await settle();
    assert.equal(page.fetchCalls.length, 0, JSON.stringify(value));
  }
});

test("an address without #yt= is left alone", async () => {
  for (const hash of ["", "#", "#y=abc", "#YT=abc", "#xyt=abc"]) {
    const page = await loadApp({ fetchEnabled: "true", hash, fetch: fetcher() });
    await settle();
    assert.deepEqual(page.events, [], hash);
    assert.equal(page.location.hash, hash);
  }
});

test("a link that is not a YouTube video is refused on the device", async () => {
  for (const text of ["", "hello", `https://example.internal/watch?v=${VIDEO_ID}`]) {
    const page = await loadApp({ fetchEnabled: "true", fetch: fetcher() });
    page.el("yt-link").value = text;
    page.el("yt-fetch").dispatch("click");
    await settle();
    assert.equal(page.fetchCalls.length, 0, text);
    assert.equal(page.el("yt-status").textContent, fetchErrorMessage("bad_link"));
    assert.equal(page.el("yt-status").classes.has("error"), true);
  }
  // A #yt= with broken escapes ends the same way.
  const page = await loadApp({ fetchEnabled: "true", hash: "#yt=%E0%A4%A", fetch: fetcher() });
  await settle();
  assert.equal(page.fetchCalls.length, 0);
  assert.equal(page.el("yt-status").textContent, fetchErrorMessage("bad_link"));
});

test("a failed fetch shows plain words and leaves the page usable", async () => {
  const cases = [
    [{ statuses: [{ status: "failed", error: "too_long" }] }, "too_long"],
    [{ statuses: [{ status: "failed", error: "blocked" }] }, "blocked"],
    [{ post: Promise.resolve(json({ error: "rate_limited" }, 429)) }, "rate_limited"],
    [{ post: Promise.resolve(json({ error: "busy" }, 503)) }, "busy"],
  ];
  for (const [options, code] of cases) {
    const page = await loadApp({ maxClipSeconds: "15", fetchEnabled: "true", fetch: fetcher(options) });
    page.el("yt-link").value = VIDEO_ID;
    page.el("yt-fetch").dispatch("click");
    await until(() => page.el("yt-status").classes.has("error"), code);
    assert.equal(page.el("yt-status").textContent, fetchErrorMessage(code));
    assert.equal(page.el("editor").hidden, true);
    if (code === "too_long") assert.match(page.el("yt-status").textContent, /10 minutes/);
    // A file can still be picked.
    await pickFile(page, "My Song.mp3", [SONG]);
    assert.equal(page.el("editor").hidden, false);
    assert.equal(page.el("yt-status").textContent, "");
  }
});

test("audio the browser cannot decode leaves the page usable", async () => {
  // decode.js refuses anything under half a second.
  setDecoded([new Float32Array(1000)]);
  const page = await loadApp({ fetchEnabled: "true", fetch: fetcher() });
  page.el("yt-link").value = VIDEO_ID;
  page.el("yt-fetch").dispatch("click");
  await until(() => page.el("yt-status").classes.has("error"), "the decode error");
  assert.equal(page.el("yt-status").textContent, "This file is shorter than half a second, which is too short for a ringtone.");
  assert.equal(page.el("editor").hidden, true);
  setDecoded([LOUD_AT_30]);
  page.el("yt-fetch").dispatch("click");
  await until(() => !page.el("editor").hidden, "the editor");
});

test("a file picked while a fetch runs wins, and the fetch goes no further", async () => {
  let release;
  const post = new Promise((resolve) => { release = resolve; });
  const page = await loadApp({ maxClipSeconds: "15", fetchEnabled: "true", fetch: fetcher({ post }) });
  page.el("yt-link").value = VIDEO_ID;
  page.el("yt-fetch").dispatch("click");
  await settle();
  assert.equal(page.el("yt-status").textContent, "Fetching from YouTube…");
  await pickFile(page, "My Song.mp3", [SONG]);
  release(json({ job_id: JOB_ID }, 202));
  await settle();
  await sleep(50);
  assert.deepEqual(fetchUrls(page), ["/fetch/api/jobs"], "no poll for a replaced fetch");
  assert.equal(page.el("name").value, "My Song");
  assert.equal(page.el("yt-status").textContent, "");
  assert.match(page.el("file-status").textContent, /^My Song\.mp3, 1:00\.0\. The selection starts on the loudest stretch;/);
});

test("a fetch replaced while its status is on the way never downloads the audio", async () => {
  let release;
  const firstStatus = new Promise((resolve) => { release = resolve; });
  const page = await loadApp({ maxClipSeconds: "15", fetchEnabled: "true", fetch: fetcher({ firstStatus }) });
  page.el("yt-link").value = VIDEO_ID;
  page.el("yt-fetch").dispatch("click");
  await until(() => page.fetchCalls.length === 2, "the first poll");
  await pickFile(page, "My Song.mp3", [SONG]);
  // The poll answers done after the file replaced the fetch.
  release(json(DONE));
  await settle();
  await sleep(50);
  assert.deepEqual(fetchUrls(page), ["/fetch/api/jobs", `/fetch/api/jobs/${JOB_ID}`], "no audio for a replaced fetch");
  assert.equal(page.el("name").value, "My Song");
  assert.equal(page.el("yt-status").textContent, "");
});

test("a second fetch replaces one that is still polling", async () => {
  setDecoded([LOUD_AT_30]);
  const statuses = [{ status: "queued" }, DONE];
  const page = await loadApp({ maxClipSeconds: "15", fetchEnabled: "true", fetch: fetcher({ statuses }) });
  page.el("yt-link").value = VIDEO_ID;
  page.el("yt-fetch").dispatch("click");
  await until(() => page.fetchCalls.length === 2, "the first poll");
  // The second fetch's first poll answers done at once.
  page.el("yt-fetch").dispatch("click");
  await until(() => !page.el("editor").hidden, "the editor");
  // The first fetch's next poll, due 2 s after its first, never happens.
  await sleep(2300);
  assert.deepEqual(fetchUrls(page), [
    "/fetch/api/jobs", `/fetch/api/jobs/${JOB_ID}`,
    "/fetch/api/jobs", `/fetch/api/jobs/${JOB_ID}`, `/fetch/api/jobs/${JOB_ID}/audio`,
  ]);
});
