// The loop controls' rules (docs/PLAN.md 7.1): where the two loop bars may be,
// and what they do when a trim bar moves. Positions are whole frames, as the
// selection's are. The loop only narrows what Play repeats; nothing here ever
// reaches a render.

// The shortest loop, in seconds. Half the shortest clip the server accepts
// (0.5 s), so the default loop, start to halfway, fits every selection.
export const MIN_LOOP_SECONDS = 0.25;

function clamp(value, low, high) {
  return Math.min(Math.max(value, low), high);
}

// Inside the selection and at least minFrames long. The start gives way
// first, so a loop squeezed against the selection's end keeps that end.
function fit(start, end, selection, minFrames) {
  const from = clamp(start, selection.start, selection.end - minFrames);
  return { start: from, end: clamp(end, from + minFrames, selection.end) };
}

/**
 * The loop a clip gets when loop controls are first turned on for it: from
 * the selection's start to its middle.
 * @param {{start: number, end: number}} selection frames
 * @param {number} minFrames the shortest loop; no longer than the shortest selection
 * @returns {{start: number, end: number}}
 */
export function defaultLoop(selection, minFrames) {
  const middle = selection.start + Math.round((selection.end - selection.start) / 2);
  return fit(selection.start, middle, selection, minFrames);
}

/**
 * Carry the loop through a change of the selection.
 *
 * A loop bar that rests exactly on a trim bar travels with it, in both
 * directions: with the loop on the start, moving the start earlier is heard
 * on the next pass. Any other loop bar keeps its place in the audio, and only
 * moves when a trim bar reaches it and pushes it. Sliding the whole selection
 * ("move") carries the loop along, as it carries everything else.
 *
 * `how` is the gesture, not a guess from the numbers: dragging an edge past
 * the cap pulls the other edge along, which moves both edges by the same
 * amount and still is no slide.
 * @param {{start: number, end: number}} loop frames
 * @param {{start: number, end: number}} before the selection the loop was in
 * @param {{start: number, end: number}} after the selection now
 * @param {"edge" | "move"} how
 * @param {number} minFrames the shortest loop
 * @returns {{start: number, end: number}}
 */
export function followSelection(loop, before, after, how, minFrames) {
  if (how === "move") {
    const shift = after.start - before.start;
    return fit(loop.start + shift, loop.end + shift, after, minFrames);
  }
  const start = loop.start === before.start ? after.start : loop.start;
  const end = loop.end === before.end ? after.end : loop.end;
  return fit(start, end, after, minFrames);
}

/**
 * Move one loop bar to `frame`. It stops at the trim bar on its side, where
 * it may rest (a loop over the whole selection is a valid one), and at the
 * shortest loop from the other loop bar, which it never pushes: a loop bar
 * moves only when it is dragged or a trim bar pushes it.
 * @param {{start: number, end: number}} loop frames
 * @param {"start" | "end"} edge
 * @param {number} frame
 * @param {{start: number, end: number}} selection frames
 * @param {number} minFrames the shortest loop
 * @returns {{start: number, end: number}}
 */
export function moveLoopEdge(loop, edge, frame, selection, minFrames) {
  const target = Math.round(frame);
  if (edge === "start") {
    return { start: clamp(target, selection.start, loop.end - minFrames), end: loop.end };
  }
  if (edge === "end") {
    return { start: loop.start, end: clamp(target, loop.start + minFrames, selection.end) };
  }
  return { start: loop.start, end: loop.end };
}
