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
import { holdDecoding, loadApp, pickFile, setDecoded, settle, sleep, wavShape } from "./fake_dom.mjs";

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
const OTHER_VIDEO_ID = "Lmnopqrstuv";
const JOB_ID = "fedcba9876543210fedcba9876543210";
const AUDIO_BYTES = [9, 8, 7, 6, 5];
// 30 s of digital silence, 15 s loud, 15 s of silence: rule 3 would start at
// about 30 s, and a start put anywhere in the silence stays exactly there.
const LOUD_AT_30 = sections([30, 0], [15, 0.5], [15, 0]);
const TITLE = "Never Gonna (Live).Remix";
const DONE = { status: "done", title: TITLE, duration: 60, heatmap: null };

const audioResponse = () => new Response(new Uint8Array(AUDIO_BYTES), { headers: { "Content-Type": "audio/mp4" } });

// The fetcher behind /fetch/, answering in order from `statuses` (the last
// one repeats), or with `firstStatus` (a promise a test holds) to the first
// poll, and with `firstAudio` (likewise) to the first audio request; the
// core's routes answer as server() does.
function fetcher({ statuses = [DONE], post = null, firstStatus = null, firstAudio = null } = {}) {
  let polls = 0;
  let audios = 0;
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
      audios += 1;
      if (audios === 1 && firstAudio !== null) return firstAudio;
      return Promise.resolve(audioResponse());
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
  assert.equal(page.el("yt-status").textContent, "The server is downloading it…");
  await until(() => !page.el("editor").hidden, "the editor", 4000);
  assert.equal(page.el("selection-readout").textContent, "0:05.0 to 0:20.0, 15.0 s (the maximum)");
  assert.match(page.el("yt-status").textContent, /The selection is on the most replayed stretch;/);
});

test("a clip no longer than the cap is selected whole, and the status does not name t= or the heatmap", async () => {
  // suggestSelection takes such a clip whole from 0 before any rule applies.
  const heatmap = Array.from({ length: 10 }, (_, i) => ({ start: i, end: i + 1, value: i === 6 ? 1 : 0.1 }));
  for (const statuses of [[{ ...DONE, duration: 10 }], [{ ...DONE, duration: 10, heatmap }]]) {
    setDecoded([sections([10, 0.3])]);
    const page = await loadApp({ maxClipSeconds: "15", fetchEnabled: "true", fetch: fetcher({ statuses }) });
    page.el("yt-link").value = `https://youtu.be/${VIDEO_ID}?t=5`;
    page.el("yt-fetch").dispatch("click");
    await until(() => !page.el("editor").hidden, "the editor");
    assert.match(page.el("selection-readout").textContent, /^0:00\.0 to 0:10\.0, 10\.0 s/);
    assert.equal(page.el("yt-status").textContent,
      `${TITLE}, 0:10.0. The whole clip is selected; drag the handles to change it.`);
  }
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
    [{ statuses: [{ status: "failed", error: "live" }] }, "live"],
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
    if (code === "live") assert.match(page.el("yt-status").textContent, /live stream/);
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

test("a fetch for a different video replaces one that is still polling", async () => {
  setDecoded([LOUD_AT_30]);
  const statuses = [{ status: "queued" }, DONE];
  const page = await loadApp({ maxClipSeconds: "15", fetchEnabled: "true", fetch: fetcher({ statuses }) });
  page.el("yt-link").value = VIDEO_ID;
  page.el("yt-fetch").dispatch("click");
  await until(() => page.fetchCalls.length === 2, "the first poll");
  // The second fetch's first poll answers done at once.
  page.el("yt-link").value = OTHER_VIDEO_ID;
  page.el("yt-fetch").dispatch("click");
  await until(() => !page.el("editor").hidden, "the editor");
  // The first fetch's next poll, due 2 s after its first, never happens.
  await sleep(2300);
  assert.deepEqual(fetchUrls(page), [
    "/fetch/api/jobs", `/fetch/api/jobs/${JOB_ID}`,
    "/fetch/api/jobs", `/fetch/api/jobs/${JOB_ID}`, `/fetch/api/jobs/${JOB_ID}/audio`,
  ]);
  assert.equal(page.fetchCalls[0].init.body, `{"video_id":"${VIDEO_ID}"}`);
  assert.equal(page.fetchCalls[2].init.body, `{"video_id":"${OTHER_VIDEO_ID}"}`);
});

test("a second tap on Fetch for the same video while it is on its way opens no second job", async () => {
  setDecoded([LOUD_AT_30]);
  const statuses = [{ status: "queued" }, DONE];
  const page = await loadApp({ maxClipSeconds: "15", fetchEnabled: "true", fetch: fetcher({ statuses }) });
  page.el("yt-link").value = VIDEO_ID;
  // A double tap, then an impatient one while the job is still queued.
  page.el("yt-fetch").dispatch("click");
  page.el("yt-fetch").dispatch("click");
  await until(() => page.fetchCalls.length === 2, "the first poll");
  page.el("yt-fetch").dispatch("click");
  await until(() => !page.el("editor").hidden, "the editor", 5000);
  assert.deepEqual(fetchUrls(page), [
    "/fetch/api/jobs", `/fetch/api/jobs/${JOB_ID}`, `/fetch/api/jobs/${JOB_ID}`, `/fetch/api/jobs/${JOB_ID}/audio`,
  ]);
  assert.equal(page.el("yt-status").textContent,
    `${TITLE}, 1:00.0. The selection starts on the loudest stretch; drag the handles to change it.`);
  // Once it is done, the same link can be fetched again. Its audio arrives
  // too, so nothing of it is left running into the next test.
  page.el("yt-fetch").dispatch("click");
  await until(() => fetchUrls(page).filter((url) => url === "/fetch/api/jobs").length === 2, "the second job");
  await until(() => fetchUrls(page).filter((url) => url.endsWith("/audio")).length === 2, "the second audio");
  await settle();
});

test("the replaced fetch's cancel neither shows its words nor drops the newer fetch's way to be stopped", async () => {
  // Fetch A polls queued, then fetch B replaces it and polls queued too.
  // A's next look, 2 s on, ends in "cancelled" while B's next poll (held
  // here) is still out, so nothing of B's overwrites what A's catch does.
  let release;
  const held = new Promise((resolve) => { release = resolve; });
  let polls = 0;
  const core = server();
  const fetch = (url, init) => {
    if (url === "/fetch/api/jobs") return Promise.resolve(json({ job_id: JOB_ID }, 202));
    if (url === `/fetch/api/jobs/${JOB_ID}`) {
      polls += 1;
      if (polls <= 2) return Promise.resolve(json({ status: "queued" }));
      if (polls === 3) return held;
      // Only a regression gets here; failing ends the polling.
      return Promise.resolve(json({ status: "failed", error: "failed" }));
    }
    return core(url, init);
  };
  const page = await loadApp({ maxClipSeconds: "15", fetchEnabled: "true", fetch });
  page.el("yt-link").value = VIDEO_ID;
  page.el("yt-fetch").dispatch("click");
  await until(() => page.fetchCalls.length === 2, "fetch A's first poll");
  page.el("yt-link").value = OTHER_VIDEO_ID;
  page.el("yt-fetch").dispatch("click");
  await until(() => page.fetchCalls.length === 4, "fetch B's first poll");
  // B's second poll comes after A's cancelled look, which was due first. At
  // least, not exactly, 5: a regression that lets A's look through makes it
  // 6, which the exact count at the end catches; an exact check here could
  // miss that and wait out the deadline. The budget allows for a slow runner.
  await until(() => page.fetchCalls.length >= 5, "fetch B's second poll", 8000);
  await settle();
  assert.equal(page.el("yt-status").textContent, "Waiting in the queue…");
  assert.equal(page.el("yt-status").classes.has("error"), false);
  // A pick must still stop B: its controller was not A's catch to drop.
  await pickFile(page, "My Song.mp3", [SONG]);
  release(json({ status: "queued" }));
  await sleep(2300);
  assert.equal(page.fetchCalls.filter((call) => call.url.startsWith("/fetch/")).length, 5, "no poll after the pick");
  assert.equal(page.el("yt-status").textContent, "");
  assert.equal(page.el("name").value, "My Song");
});

test("a file picked while the audio downloads wins, and the stale audio is never decoded", async () => {
  let release;
  const firstAudio = new Promise((resolve) => { release = resolve; });
  const page = await loadApp({ maxClipSeconds: "15", fetchEnabled: "true", fetch: fetcher({ firstAudio }) });
  page.el("yt-link").value = VIDEO_ID;
  page.el("yt-fetch").dispatch("click");
  await until(() => page.fetchCalls.length === 3, "the audio request");
  assert.equal(page.el("yt-status").textContent, "Fetching the audio…");
  await pickFile(page, "My Song.mp3", [SONG]);
  // The audio arrives after the file replaced the fetch.
  release(audioResponse());
  await settle();
  await sleep(50);
  assert.equal(page.decodedInputs().length, 1, "only the picked file is decoded");
  assert.notDeepEqual([...page.decodedInputs()[0]], AUDIO_BYTES);
  assert.equal(page.el("yt-status").textContent, "");
  assert.equal(page.el("name").value, "My Song");
  assert.match(page.el("file-status").textContent, /^My Song\.mp3, 1:00\.0\. The selection starts on the loudest stretch;/);
});

test("a fetch replaced while its audio downloads leaves the newer fetch alone", async () => {
  let release;
  const firstAudio = new Promise((resolve) => { release = resolve; });
  // The first fetch's poll answers done, the second fetch's first poll
  // queued. Its next one, which only a regression makes, ends the polling,
  // so a failure here can't keep the run alive for MAX_POLLS.
  const statuses = [DONE, { status: "queued" }, { status: "failed", error: "failed" }];
  const page = await loadApp({ maxClipSeconds: "15", fetchEnabled: "true", fetch: fetcher({ statuses, firstAudio }) });
  page.el("yt-link").value = VIDEO_ID;
  page.el("yt-fetch").dispatch("click");
  await until(() => page.fetchCalls.length === 3, "the first fetch's audio request");
  page.el("yt-link").value = OTHER_VIDEO_ID;
  page.el("yt-fetch").dispatch("click");
  await until(() => page.fetchCalls.length === 5, "the second fetch's first poll");
  await settle();
  assert.equal(page.el("yt-status").textContent, "Waiting in the queue…");
  // The first fetch's audio lands now: it must not touch the status line,
  // decode anything, or take the second fetch's way to be stopped.
  release(audioResponse());
  await settle();
  await sleep(50);
  assert.equal(page.el("yt-status").textContent, "Waiting in the queue…");
  assert.deepEqual(page.decodedInputs(), []);
  // A pick now must still stop the second fetch's polling.
  await pickFile(page, "My Song.mp3", [SONG]);
  await sleep(2300);
  assert.deepEqual(fetchUrls(page), [
    "/fetch/api/jobs", `/fetch/api/jobs/${JOB_ID}`, `/fetch/api/jobs/${JOB_ID}/audio`,
    "/fetch/api/jobs", `/fetch/api/jobs/${JOB_ID}`,
  ], "no poll after the pick");
  assert.equal(page.el("name").value, "My Song");
});

// --- the byte cap -------------------------------------------------------------------

const OVER_CAP = 64 * 1024 * 1024 + 1;

test("the picker's hint gives both limits a picked file must meet", async () => {
  const page = await loadApp({ maxClipSeconds: "15" });
  const hint = page.el("file-status").textContent;
  assert.match(hint, /\b10 minutes\b/);
  assert.match(hint, /\b64 MB\b/);
  assert.equal(page.el("file-status").classes.has("error"), false);
});

test("a picked file over 64 MB is refused with words for it, before any of it is read", async () => {
  const page = await loadApp({ maxClipSeconds: "15" });
  setDecoded([SONG]);
  let reads = 0;
  const input = page.el("file");
  input.files = [{
    name: "Huge.wav",
    size: OVER_CAP,
    arrayBuffer: () => {
      reads += 1;
      return Promise.resolve(new ArrayBuffer(8));
    },
  }];
  await Promise.all(input.dispatch("change"));
  await settle();
  assert.equal(reads, 0, "the oversized file was read");
  assert.deepEqual(page.decodedInputs(), []);
  assert.equal(page.el("editor").hidden, true);
  assert.ok(page.el("file-status").classes.has("error"));
  assert.match(page.el("file-status").textContent, /64 MB/);
  // A file within the cap still opens.
  await pickFile(page, "My Song.mp3", [SONG]);
  assert.equal(page.el("editor").hidden, false);
});

test("fetched audio over 64 MB is refused with its own words and never decoded", async () => {
  setDecoded([LOUD_AT_30]);
  const huge = new Response(new Uint8Array(AUDIO_BYTES),
    { headers: { "Content-Type": "audio/mp4", "Content-Length": String(OVER_CAP) } });
  const page = await loadApp({
    maxClipSeconds: "15", fetchEnabled: "true", fetch: fetcher({ firstAudio: Promise.resolve(huge) }),
  });
  page.el("yt-link").value = VIDEO_ID;
  page.el("yt-fetch").dispatch("click");
  await until(() => page.el("yt-status").classes.has("error") || !page.el("editor").hidden, "the fetch's outcome");
  assert.deepEqual(page.decodedInputs(), [], "the oversized audio was decoded");
  assert.equal(page.el("editor").hidden, true);
  assert.equal(page.el("yt-status").textContent, fetchErrorMessage("audio_too_large"));
  assert.notEqual(page.el("yt-status").textContent, fetchErrorMessage("zz_unknown"));
});

// --- long videos and windows of them (docs/PLAN.md 6.2) ------------------------------------

const WINDOW_JOB_ID = "aaaaaaaaaaaaaaaabbbbbbbbbbbbbbbb";
// An hour in 200 points of 18 s, hot from 1206 s to 1800 s: the ten minutes
// with the most replays begin at 1200 s (20:00).
const HOUR_MAP = Array.from({ length: 200 }, (_, i) => ({
  start: i * 18, end: (i + 1) * 18, value: i >= 67 && i < 100 ? 1 : 0.1,
}));
const LONG = { status: "long", title: TITLE, duration: 3600, heatmap: HOUR_MAP };
// The window the server cut begins a little before the start asked for.
const WINDOW_DONE = { status: "done", title: TITLE, duration: 3600, heatmap: HOUR_MAP, window_start: 1195 };
const ASK_WORDS = "This video is 1:00:00 long, and the server fetches up to 10 minutes of it at a time. "
  + "The start time is set to the most replayed part. Change it if you like, then tap Fetch.";
const WINDOW_AFTER = " For another part of the video, change the start time and tap Fetch.";

// The fetcher for a long video: a job without a start answers from `first`
// (in order, the last repeats); one with a start gets a job ID of its own,
// answers from `second`, and is the only one with audio. `windowPosts`,
// promises a test holds, answer the first requests for a window instead, in
// order, and `lookUpPosts` the first requests without a start.
function longFetcher({ first = [LONG], second = [WINDOW_DONE], windowPosts = [], lookUpPosts = [] } = {}) {
  const polls = { [JOB_ID]: 0, [WINDOW_JOB_ID]: 0 };
  const held = [...windowPosts];
  const heldLookUps = [...lookUpPosts];
  const core = server();
  return (url, init) => {
    if (url === "/fetch/api/jobs") {
      if (!("start" in JSON.parse(init.body))) {
        return heldLookUps.shift() ?? Promise.resolve(json({ job_id: JOB_ID }, 202));
      }
      return held.shift() ?? Promise.resolve(json({ job_id: WINDOW_JOB_ID }, 202));
    }
    for (const [id, statuses] of [[JOB_ID, first], [WINDOW_JOB_ID, second]]) {
      if (url === `/fetch/api/jobs/${id}`) {
        const status = statuses[Math.min(polls[id], statuses.length - 1)];
        polls[id] += 1;
        return Promise.resolve(json(status));
      }
    }
    if (url === `/fetch/api/jobs/${WINDOW_JOB_ID}/audio`) return Promise.resolve(audioResponse());
    return core(url, init);
  };
}

const jobPosts = (page) => page.fetchCalls.filter((call) => call.url === "/fetch/api/jobs").map((call) => call.init.body);
const LOOK_UP = `{"video_id":"${VIDEO_ID}"}`;

// Loads the app and taps Fetch on a link to the long video; returns once the
// page has asked where to start.
async function askedWhereToStart(options = {}, link = `https://youtu.be/${VIDEO_ID}`) {
  setDecoded([LOUD_AT_30]);
  const page = await loadApp({ maxClipSeconds: "15", fetchEnabled: "true", fetch: longFetcher(options) });
  page.el("yt-link").value = link;
  page.el("yt-fetch").dispatch("click");
  await until(() => !page.el("yt-start-row").hidden, "the start field");
  await settle();
  return page;
}

test("the start field is hidden until a video turns out to be long", async () => {
  const page = await loadApp({ fetchEnabled: "true", fetch: fetcher() });
  assert.equal(page.el("yt-start-row").hidden, true);
  setDecoded([LOUD_AT_30]);
  page.el("yt-link").value = VIDEO_ID;
  page.el("yt-fetch").dispatch("click");
  await until(() => !page.el("editor").hidden, "the editor");
  // A video the server fetched whole never shows it.
  assert.equal(page.el("yt-start-row").hidden, true);
});

test("a long video asks where to start before anything is downloaded", async () => {
  const page = await askedWhereToStart();
  // Asked once and answered long: no second request, no audio, no editor.
  assert.deepEqual(fetchUrls(page), ["/fetch/api/jobs", `/fetch/api/jobs/${JOB_ID}`]);
  assert.deepEqual(jobPosts(page), [LOOK_UP]);
  assert.deepEqual(page.decodedInputs(), []);
  assert.equal(page.el("editor").hidden, true);
  // The ten minutes with the most replays, as a time a person reads.
  assert.equal(page.el("yt-start").value, "20:00");
  assert.equal(page.el("yt-status").textContent, ASK_WORDS);
  assert.equal(page.el("yt-status").classes.has("error"), false);
  // Nothing more happens by itself.
  await sleep(200);
  assert.equal(page.fetchCalls.length, 2);
});

test("the next tap fetches the window the start field names, and rule 2 reads the heatmap in the window's time", async () => {
  const page = await askedWhereToStart();
  page.el("yt-fetch").dispatch("click");
  await until(() => !page.el("editor").hidden, "the editor");
  // Only now does a start time leave the device: one whole number.
  assert.deepEqual(jobPosts(page), [LOOK_UP, `{"video_id":"${VIDEO_ID}","start":1200}`]);
  assert.deepEqual(fetchUrls(page).slice(2),
    ["/fetch/api/jobs", `/fetch/api/jobs/${WINDOW_JOB_ID}`, `/fetch/api/jobs/${WINDOW_JOB_ID}/audio`]);
  assert.deepEqual(page.decodedInputs().map((bytes) => [...bytes]), [AUDIO_BYTES]);
  // The window begins at 1195 s, so its hot part begins 11 s in.
  assert.equal(page.el("selection-readout").textContent, "0:11.0 to 0:26.0, 15.0 s (the maximum)");
  assert.equal(page.el("yt-status").textContent,
    `${TITLE} from 19:55, 1:00.0. The selection is on the most replayed stretch; drag the handles to change it.`
    + WINDOW_AFTER);
  assert.equal(page.el("name").value, TITLE);
  // The field stays, with the start that was fetched, for the next part.
  assert.equal(page.el("yt-start-row").hidden, false);
  assert.equal(page.el("yt-start").value, "20:00");
});

test("a link with a timestamp is fetched from there at once, and the selection starts at it", async () => {
  setDecoded([LOUD_AT_30]);
  const page = await loadApp({ maxClipSeconds: "15", fetchEnabled: "true",
    fetch: longFetcher({ second: [{ ...WINDOW_DONE, heatmap: null }] }) });
  page.el("yt-link").value = `https://www.youtube.com/watch?v=${VIDEO_ID}&t=20m3s&si=TrackingToken123`;
  page.el("yt-fetch").dispatch("click");
  await until(() => !page.el("editor").hidden, "the editor");
  // One tap: the look-up, then the window from the link's own second.
  assert.deepEqual(jobPosts(page), [LOOK_UP, `{"video_id":"${VIDEO_ID}","start":1203}`]);
  assert.equal(page.fetchCalls.length, 5);
  for (const { url, init } of page.fetchCalls) {
    const sent = url + " " + JSON.stringify(init ?? {});
    assert.ok(!sent.includes("TrackingToken123") && !sent.includes("20m3s") && !sent.includes("youtube"), sent);
  }
  // Rule 1: 1203 s is 8 s into a window that begins at 1195 s.
  assert.equal(page.el("selection-readout").textContent, "0:08.0 to 0:23.0, 15.0 s (the maximum)");
  assert.equal(page.el("yt-status").textContent,
    `${TITLE} from 19:55, 1:00.0. The selection starts at the link's timestamp; drag the handles to change it.`
    + WINDOW_AFTER);
  // Shown, so the user sees what was fetched and can ask for another part.
  assert.equal(page.el("yt-start-row").hidden, false);
  assert.equal(page.el("yt-start").value, "20:03");
});

test("a timestamp at or past the end of a long video is not sent: the page asks instead", async () => {
  for (const timestamp of ["1h", "2h", "3600"]) {
    const page = await askedWhereToStart({}, `https://youtu.be/${VIDEO_ID}?t=${timestamp}`);
    assert.deepEqual(jobPosts(page), [LOOK_UP]);
    assert.equal(page.el("yt-start").value, "20:00");
    assert.equal(page.el("yt-status").textContent, ASK_WORDS);
  }
});

test("a start the user typed is where the selection begins", async () => {
  const page = await askedWhereToStart();
  // The iPhone's number pad has a point, no colon: 20.05 is 20:05.
  page.el("yt-start").value = "20.05";
  page.el("yt-fetch").dispatch("click");
  await until(() => !page.el("editor").hidden, "the editor");
  assert.deepEqual(jobPosts(page), [LOOK_UP, `{"video_id":"${VIDEO_ID}","start":1205}`]);
  // 1205 s is 10 s into the window, and the heatmap does not get a say.
  assert.equal(page.el("selection-readout").textContent, "0:10.0 to 0:25.0, 15.0 s (the maximum)");
  assert.equal(page.el("yt-status").textContent,
    `${TITLE} from 19:55, 1:00.0. The selection starts at the time you chose; drag the handles to change it.`
    + WINDOW_AFTER);
  // What the user typed stays as typed.
  assert.equal(page.el("yt-start").value, "20.05");
});

test("Enter in the start field fetches too", async () => {
  const page = await askedWhereToStart();
  page.el("yt-start").dispatch("keydown", { key: "a" });
  await settle();
  assert.equal(jobPosts(page).length, 1);
  page.el("yt-start").dispatch("keydown", { key: "Enter" });
  await until(() => !page.el("editor").hidden, "the editor");
  assert.equal(jobPosts(page)[1], `{"video_id":"${VIDEO_ID}","start":1200}`);
});

test("a start that is no time, or lies at or past the end, is refused on the device", async () => {
  const page = await askedWhereToStart();
  for (const [value, code] of [["soon", "bad_time"], ["", "bad_time"], ["1:2:3:4", "bad_time"], ["-5", "bad_time"],
    ["1:00:00", "past_end"], ["2:00:00", "past_end"], ["3600", "past_end"]]) {
    page.el("yt-start").value = value;
    page.el("yt-fetch").dispatch("click");
    await settle();
    assert.equal(page.el("yt-status").textContent, fetchErrorMessage(code), value);
    assert.equal(page.el("yt-status").classes.has("error"), true);
  }
  // Nothing was asked of the server for any of them, and the field is still there.
  assert.deepEqual(jobPosts(page), [LOOK_UP]);
  assert.equal(page.el("yt-start-row").hidden, false);
  // The last second of the video is a start.
  page.el("yt-start").value = "59:59";
  page.el("yt-fetch").dispatch("click");
  await until(() => jobPosts(page).length === 2, "the window request");
  assert.equal(jobPosts(page)[1], `{"video_id":"${VIDEO_ID}","start":3599}`);
});

test("without a heatmap the start field begins at the beginning", async () => {
  const page = await askedWhereToStart({ first: [{ ...LONG, heatmap: null }], second: [{ ...WINDOW_DONE, heatmap: null,
    window_start: 0 }] });
  assert.equal(page.el("yt-start").value, "0:00");
  assert.equal(page.el("yt-status").textContent, ASK_WORDS.replace("the most replayed part", "the beginning"));
  page.el("yt-fetch").dispatch("click");
  await until(() => !page.el("editor").hidden, "the editor");
  assert.equal(jobPosts(page)[1], `{"video_id":"${VIDEO_ID}","start":0}`);
  // The page's own guess, not the user's: rule 3 places the selection.
  assert.match(page.el("yt-status").textContent, /^Never Gonna \(Live\)\.Remix from 0:00, 1:00\.0\. The selection starts on the loudest stretch;/);
});

test("another video starts over with a look-up of its own", async () => {
  const page = await askedWhereToStart();
  page.el("yt-start").value = "5:00";
  page.el("yt-link").value = `https://youtu.be/${OTHER_VIDEO_ID}`;
  page.el("yt-fetch").dispatch("click");
  await until(() => jobPosts(page).length === 2, "the second look-up");
  await settle();
  // Not a window of the first video from 5:00, and not a start for the second.
  assert.equal(jobPosts(page)[1], `{"video_id":"${OTHER_VIDEO_ID}"}`);
  // The fake fetcher calls every video long: the field is filled afresh.
  assert.equal(page.el("yt-start").value, "20:00");
});

test("picking a file puts the YouTube box back as it was", async () => {
  const page = await askedWhereToStart();
  await pickFile(page, "My Song.mp3", [SONG]);
  assert.equal(page.el("yt-start-row").hidden, true);
  assert.equal(page.el("yt-start").value, "");
  assert.equal(page.el("yt-status").textContent, "");
  // The same link again is looked up again: nothing remembers the old answer.
  page.el("yt-fetch").dispatch("click");
  await until(() => jobPosts(page).length === 2, "the second look-up");
  assert.equal(jobPosts(page)[1], LOOK_UP);
});

test("an answer to a window request that is no window is refused", async () => {
  for (const second of [[LONG], [{ ...WINDOW_DONE, window_start: undefined }], [{ ...WINDOW_DONE, window_start: -5 }],
    [{ ...WINDOW_DONE, window_start: "1195" }]]) {
    const page = await askedWhereToStart({ second });
    page.el("yt-fetch").dispatch("click");
    await until(() => page.el("yt-status").classes.has("error"), "the refusal");
    assert.equal(page.el("yt-status").textContent, fetchErrorMessage("bad_response"));
    assert.equal(page.el("editor").hidden, true);
    // The audio of such an answer is never asked for.
    assert.ok(!fetchUrls(page).some((url) => url.endsWith("/audio")));
    assert.equal(page.el("yt-start-row").hidden, false);
  }
});

test("the server's refusal of a window is shown, and the field stays for another try", async () => {
  for (const code of ["past_end", "live", "blocked", "timeout"]) {
    const page = await askedWhereToStart({ second: [{ status: "failed", error: code }] });
    page.el("yt-fetch").dispatch("click");
    await until(() => page.el("yt-status").classes.has("error"), code);
    assert.equal(page.el("yt-status").textContent, fetchErrorMessage(code));
    assert.equal(page.el("yt-start-row").hidden, false);
    assert.equal(page.el("yt-start").value, "20:00");
    // Another start is another request for a window, not a new look-up.
    page.el("yt-start").value = "10:00";
    page.el("yt-fetch").dispatch("click");
    await until(() => jobPosts(page).length === 3, "the next window request");
    assert.equal(jobPosts(page)[2], `{"video_id":"${VIDEO_ID}","start":600}`);
  }
});

test("a second tap while a window is on its way opens no second job", async () => {
  let release;
  const held = new Promise((resolve) => {
    release = resolve;
  });
  const page = await askedWhereToStart({ windowPosts: [held] });
  page.el("yt-fetch").dispatch("click");
  await until(() => jobPosts(page).length === 2, "the window request");
  page.el("yt-fetch").dispatch("click");
  page.el("yt-start").dispatch("keydown", { key: "Enter" });
  await settle();
  assert.equal(jobPosts(page).length, 2);
  assert.match(page.el("yt-status").textContent, /^Fetching up to 10 minutes from 20:00/);
  release(json({ job_id: WINDOW_JOB_ID }, 202));
  await until(() => !page.el("editor").hidden, "the editor");
  assert.equal(jobPosts(page).length, 2);
});

test("after a window, the next tap fetches the part the field now names", async () => {
  const page = await askedWhereToStart();
  page.el("yt-fetch").dispatch("click");
  await until(() => !page.el("editor").hidden, "the editor");
  page.el("yt-start").value = "45:00";
  page.el("yt-fetch").dispatch("click");
  await until(() => jobPosts(page).length === 3, "the second window request");
  // No new look-up: the video is known to be long.
  assert.equal(jobPosts(page)[2], `{"video_id":"${VIDEO_ID}","start":2700}`);
  // Finished before the next test begins: what a page decodes is recorded in one place for them all.
  await until(() => !page.el("editor").hidden, "the editor");
});

test("a tap for another part while a window is on its way replaces it", async () => {
  let release;
  const held = new Promise((resolve) => {
    release = resolve;
  });
  const page = await askedWhereToStart({ windowPosts: [held], second: [{ ...WINDOW_DONE, window_start: 2695 }] });
  page.el("yt-fetch").dispatch("click");
  await until(() => jobPosts(page).length === 2, "the first window request");
  // Not ignored: the field names another part now.
  page.el("yt-start").value = "45:00";
  page.el("yt-fetch").dispatch("click");
  await until(() => !page.el("editor").hidden, "the editor");
  assert.deepEqual(jobPosts(page).slice(1),
    [`{"video_id":"${VIDEO_ID}","start":1200}`, `{"video_id":"${VIDEO_ID}","start":2700}`]);
  assert.equal(page.el("yt-status").textContent,
    `${TITLE} from 44:55, 1:00.0. The selection starts at the time you chose; drag the handles to change it.`
    + WINDOW_AFTER);
  // The first request's answer arrives late and changes nothing.
  const calls = page.fetchCalls.length;
  release(json({ job_id: WINDOW_JOB_ID }, 202));
  await sleep(100);
  assert.equal(page.fetchCalls.length, calls);
  assert.equal(page.decodedInputs().length, 1);
});

test("the same video under a link with another timestamp is fetched from that timestamp", async () => {
  setDecoded([LOUD_AT_30]);
  const page = await loadApp({ maxClipSeconds: "15", fetchEnabled: "true",
    fetch: longFetcher({ second: [{ ...WINDOW_DONE, heatmap: null }] }) });
  page.el("yt-link").value = `https://youtu.be/${VIDEO_ID}?t=20m3s`;
  page.el("yt-fetch").dispatch("click");
  await until(() => !page.el("editor").hidden, "the editor");
  assert.equal(page.el("yt-start").value, "20:03");
  // The same video again, pasted with another time and the field untouched.
  page.el("yt-link").value = `https://youtu.be/${VIDEO_ID}?t=20m10s`;
  page.el("yt-fetch").dispatch("click");
  await until(() => jobPosts(page).length === 3 && !page.el("editor").hidden, "the second window");
  // No new look-up, and not 20:03 again in the new link's name.
  assert.equal(jobPosts(page)[2], `{"video_id":"${VIDEO_ID}","start":1210}`);
  assert.equal(page.el("yt-start").value, "20:10");
  // Rule 1 at the new time: 1210 s is 15 s into a window that begins at 1195 s.
  assert.equal(page.el("selection-readout").textContent, "0:15.0 to 0:30.0, 15.0 s (the maximum)");
  assert.equal(page.el("yt-status").textContent,
    `${TITLE} from 19:55, 1:00.0. The selection starts at the link's timestamp; drag the handles to change it.`
    + WINDOW_AFTER);
  // The same link once more is the same part once more: the field's own start.
  page.el("yt-fetch").dispatch("click");
  await until(() => jobPosts(page).length === 4, "the third window request");
  assert.equal(jobPosts(page)[3], `{"video_id":"${VIDEO_ID}","start":1210}`);
  // Finished before the next test begins: what a page decodes is recorded in one place for them all.
  await until(() => !page.el("editor").hidden, "the editor");
});

test("a link that names no time any more is asked about again, with nothing fetched", async () => {
  // Without its time, and with one that lies past the video's end.
  for (const link of [`https://youtu.be/${VIDEO_ID}`, `https://youtu.be/${VIDEO_ID}?t=2h`]) {
    setDecoded([LOUD_AT_30]);
    const page = await loadApp({ maxClipSeconds: "15", fetchEnabled: "true", fetch: longFetcher() });
    page.el("yt-link").value = `https://youtu.be/${VIDEO_ID}?t=20m3s`;
    page.el("yt-fetch").dispatch("click");
    await until(() => !page.el("editor").hidden, "the editor");
    assert.equal(page.el("yt-start").value, "20:03");
    page.el("yt-link").value = link;
    page.el("yt-fetch").dispatch("click");
    await settle();
    // Not 20:03 again: that was the other link's time.
    assert.equal(jobPosts(page).length, 2, link);
    assert.equal(page.el("yt-start").value, "20:00");
    assert.equal(page.el("yt-status").textContent, ASK_WORDS);
  }
});

test("a start the user typed is kept when the same video's link changes", async () => {
  const page = await askedWhereToStart();
  page.el("yt-start").value = "5:00";
  page.el("yt-link").value = `https://youtu.be/${VIDEO_ID}?t=30m`;
  page.el("yt-fetch").dispatch("click");
  await until(() => jobPosts(page).length === 2, "the window request");
  // The field is what the user sees and typed in: it is what is fetched.
  assert.equal(jobPosts(page)[1], `{"video_id":"${VIDEO_ID}","start":300}`);
  assert.equal(page.el("yt-start").value, "5:00");
  // Finished before the next test begins: what a page decodes is recorded in one place for them all.
  await until(() => !page.el("editor").hidden, "the editor");
});

test("a shared link to the same video with another timestamp is fetched from that timestamp", async () => {
  const page = await askedWhereToStart();
  assert.equal(page.el("yt-start").value, "20:00");
  page.location.hash = fragment(`https://youtu.be/${VIDEO_ID}?t=30m`);
  page.fireWindow("hashchange");
  await until(() => jobPosts(page).length === 2, "the window request");
  assert.equal(jobPosts(page)[1], `{"video_id":"${VIDEO_ID}","start":1800}`);
  assert.equal(page.el("yt-start").value, "30:00");
  // Finished before the next test begins: what a page decodes is recorded in one place for them all.
  await until(() => !page.el("editor").hidden, "the editor");
});

test("asking again stops a window that is on its way", async () => {
  let release;
  const held = new Promise((resolve) => {
    release = resolve;
  });
  setDecoded([LOUD_AT_30]);
  const page = await loadApp({ maxClipSeconds: "15", fetchEnabled: "true", fetch: longFetcher({ windowPosts: [held] }) });
  page.el("yt-link").value = `https://youtu.be/${VIDEO_ID}?t=20m3s`;
  page.el("yt-fetch").dispatch("click");
  await until(() => jobPosts(page).length === 2, "the window request");
  // The link loses its time while that window is still on its way.
  page.el("yt-link").value = `https://youtu.be/${VIDEO_ID}`;
  page.el("yt-fetch").dispatch("click");
  await settle();
  assert.equal(page.el("yt-start").value, "20:00");
  assert.equal(page.el("yt-status").textContent, ASK_WORDS);
  // The window's answer arrives after the question and does not replace it.
  release(json({ job_id: WINDOW_JOB_ID }, 202));
  await sleep(100);
  assert.equal(jobPosts(page).length, 2);
  assert.deepEqual(page.decodedInputs(), []);
  assert.equal(page.el("editor").hidden, true);
  assert.equal(page.el("yt-status").textContent, ASK_WORDS);
  // And the next tap is not held back by the fetch that was stopped.
  page.el("yt-fetch").dispatch("click");
  await until(() => jobPosts(page).length === 3, "the next window request");
  assert.equal(jobPosts(page)[2], `{"video_id":"${VIDEO_ID}","start":1200}`);
  // Finished before the next test begins: what a page decodes is recorded in one place for them all.
  await until(() => !page.el("editor").hidden, "the editor");
});

test("a second tap on a link with a timestamp while its window is on its way opens no second job", async () => {
  let release;
  const held = new Promise((resolve) => {
    release = resolve;
  });
  setDecoded([LOUD_AT_30]);
  const page = await loadApp({ maxClipSeconds: "15", fetchEnabled: "true", fetch: longFetcher({ windowPosts: [held] }) });
  page.el("yt-link").value = `https://youtu.be/${VIDEO_ID}?t=20m3s`;
  page.el("yt-fetch").dispatch("click");
  // One tap made both requests: the look-up, then the window from the link's time.
  await until(() => jobPosts(page).length === 2, "the window request");
  page.el("yt-fetch").dispatch("click");
  page.el("yt-link").dispatch("keydown", { key: "Enter" });
  await settle();
  assert.equal(jobPosts(page).length, 2);
  release(json({ job_id: WINDOW_JOB_ID }, 202));
  await until(() => !page.el("editor").hidden, "the editor");
  assert.equal(jobPosts(page).length, 2);
});

test("asking again also stops a window that has arrived and is still being decoded", async () => {
  setDecoded([LOUD_AT_30]);
  const page = await loadApp({ maxClipSeconds: "15", fetchEnabled: "true", fetch: longFetcher() });
  const release = holdDecoding();
  page.el("yt-link").value = `https://youtu.be/${VIDEO_ID}?t=20m3s`;
  page.el("yt-fetch").dispatch("click");
  // The window's audio is here and with the decoder: no request is on its way any more.
  await until(() => page.decodedInputs().length === 1, "the decode");
  assert.equal(page.el("yt-status").textContent, "Reading the audio…");
  page.el("yt-link").value = `https://youtu.be/${VIDEO_ID}`;
  page.el("yt-fetch").dispatch("click");
  await settle();
  assert.equal(page.el("yt-status").textContent, ASK_WORDS);
  release();
  await sleep(100);
  // The decoded window does not take the question's place.
  assert.equal(page.el("yt-status").textContent, ASK_WORDS);
  assert.equal(page.el("editor").hidden, true);
  assert.equal(page.el("yt-start").value, "20:00");
  assert.equal(jobPosts(page).length, 2);
});

test("a timestamp that changes while the look-up is on its way is the one that is fetched", async () => {
  let release;
  const held = new Promise((resolve) => {
    release = resolve;
  });
  setDecoded([LOUD_AT_30]);
  const page = await loadApp({ maxClipSeconds: "15", fetchEnabled: "true",
    fetch: longFetcher({ lookUpPosts: [held], second: [{ ...WINDOW_DONE, heatmap: null }] }) });
  page.el("yt-link").value = `https://youtu.be/${VIDEO_ID}?t=20m3s`;
  page.el("yt-fetch").dispatch("click");
  await until(() => jobPosts(page).length === 1, "the look-up");
  // Before the server has answered, the link in the box gets another time.
  page.el("yt-link").value = `https://youtu.be/${VIDEO_ID}?t=20m10s`;
  page.el("yt-fetch").dispatch("click");
  await settle();
  // No second look-up for the same video.
  assert.equal(jobPosts(page).length, 1);
  release(json({ job_id: JOB_ID }, 202));
  await until(() => !page.el("editor").hidden, "the editor");
  // Not 20:03, which the link no longer says.
  assert.deepEqual(jobPosts(page), [LOOK_UP, `{"video_id":"${VIDEO_ID}","start":1210}`]);
  assert.equal(page.el("yt-start").value, "20:10");
  assert.equal(page.el("selection-readout").textContent, "0:15.0 to 0:30.0, 15.0 s (the maximum)");
});

test("a timestamp taken away while the look-up is on its way leaves the page asking", async () => {
  let release;
  const held = new Promise((resolve) => {
    release = resolve;
  });
  setDecoded([LOUD_AT_30]);
  const page = await loadApp({ maxClipSeconds: "15", fetchEnabled: "true", fetch: longFetcher({ lookUpPosts: [held] }) });
  page.el("yt-link").value = `https://youtu.be/${VIDEO_ID}?t=20m3s`;
  page.el("yt-fetch").dispatch("click");
  await until(() => jobPosts(page).length === 1, "the look-up");
  page.el("yt-link").value = `https://youtu.be/${VIDEO_ID}`;
  page.el("yt-fetch").dispatch("click");
  release(json({ job_id: JOB_ID }, 202));
  await until(() => !page.el("yt-start-row").hidden, "the start field");
  await sleep(100);
  // Nothing is fetched from 20:03 in the name of a link that names no time.
  assert.deepEqual(jobPosts(page), [LOOK_UP]);
  assert.equal(page.el("yt-start").value, "20:00");
  assert.equal(page.el("yt-status").textContent, ASK_WORDS);
});

test("a timestamp that changes while a short video is on its way moves the selection, with one job", async () => {
  let release;
  const held = new Promise((resolve) => {
    release = resolve;
  });
  setDecoded([LOUD_AT_30]);
  const page = await loadApp({ maxClipSeconds: "15", fetchEnabled: "true", fetch: fetcher({ post: held }) });
  page.el("yt-link").value = `https://youtu.be/${VIDEO_ID}?t=10`;
  page.el("yt-fetch").dispatch("click");
  await until(() => page.fetchCalls.length === 1, "the job request");
  page.el("yt-link").value = `https://youtu.be/${VIDEO_ID}?t=20`;
  page.el("yt-fetch").dispatch("click");
  release(json({ job_id: JOB_ID }, 202));
  await until(() => !page.el("editor").hidden, "the editor");
  assert.deepEqual(fetchUrls(page), ["/fetch/api/jobs", `/fetch/api/jobs/${JOB_ID}`, `/fetch/api/jobs/${JOB_ID}/audio`]);
  // Rule 1 with the time the link has now.
  assert.equal(page.el("selection-readout").textContent, "0:20.0 to 0:35.0, 15.0 s (the maximum)");
});
