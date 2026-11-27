import assert from "node:assert/strict";
import test from "node:test";
import {captionWords} from "../src/captions.ts";

// Twelve words, one per second.
const words = Array.from({length: 12}, (_, start) => ({start}));
const shown = (time) => captionWords(words, time).map((word) => word.start);

test("shows five words at a time and turns the page on the sixth", () => {
  assert.deepEqual(shown(0), [0, 1, 2, 3, 4]);
  assert.deepEqual(shown(4.9), [0, 1, 2, 3, 4]);
  assert.deepEqual(shown(5), [5, 6, 7, 8, 9]);
});

test("handles the edges", () => {
  assert.deepEqual(shown(-1), [0, 1, 2, 3, 4]);
  assert.deepEqual(shown(99), [10, 11]);
  assert.deepEqual(captionWords([], 3), []);
});
