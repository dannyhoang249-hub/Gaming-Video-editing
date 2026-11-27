// Words shown on screen at once, so long sentences do not cover the video.
export const WORDS_PER_CAPTION = 5;

export const captionWords = <T extends {start: number}>(words: T[], time: number): T[] => {
  // The last word that has started, so the caption holds during pauses between words.
  const spoken = Math.max(0, words.filter((word) => word.start <= time).length - 1);
  const first = spoken - (spoken % WORDS_PER_CAPTION);
  return words.slice(first, first + WORDS_PER_CAPTION);
};
