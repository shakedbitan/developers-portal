/**
 * useFlipAnimation
 *
 * Implements the FLIP (First, Last, Invert, Play) animation technique.
 * Call captureFirst() before a layout change to record element positions.
 * Call playFlip() after the layout change to animate from old → new positions.
 *
 * Usage:
 *   const { captureFirst, playFlip } = useFlipAnimation();
 *   // Before scroll:
 *   captureFirst(elementsMap);  // { [id]: domElement }
 *   // After scroll (next frame):
 *   playFlip(elementsMap, { duration: 600, easing: 'cubic-bezier(0.4,0,0.2,1)' });
 */
import { useRef, useCallback } from 'react';

export function useFlipAnimation() {
  const firstRects = useRef({});

  /**
   * Capture "First" positions of elements.
   * @param {Object} elements - { [id]: HTMLElement }
   */
  const captureFirst = useCallback((elements) => {
    firstRects.current = {};
    for (const [id, el] of Object.entries(elements)) {
      if (el) firstRects.current[id] = el.getBoundingClientRect();
    }
  }, []);

  /**
   * Play FLIP animation from captured First positions to current (Last) positions.
   * @param {Object} elements - { [id]: HTMLElement } — same elements, now in new position
   * @param {Object} options  - { duration, easing }
   */
  const playFlip = useCallback((elements, { duration = 600, easing = 'cubic-bezier(0.4,0,0.2,1)' } = {}) => {
    for (const [id, el] of Object.entries(elements)) {
      if (!el || !firstRects.current[id]) continue;

      const first = firstRects.current[id];
      const last  = el.getBoundingClientRect();

      const dx = first.left - last.left;
      const dy = first.top  - last.top;
      const dw = first.width  / last.width;
      const dh = first.height / last.height;

      // If no meaningful difference, skip
      if (Math.abs(dx) < 1 && Math.abs(dy) < 1 &&
          Math.abs(dw - 1) < 0.01 && Math.abs(dh - 1) < 0.01) continue;

      // Invert — start at "First" position
      el.style.transformOrigin = 'top left';
      el.style.transform = `translate(${dx}px, ${dy}px) scale(${dw}, ${dh})`;
      el.style.transition = 'none';
      el.style.zIndex = '100';
      el.style.pointerEvents = 'none';

      // Force browser to apply the inverted transform
      el.getBoundingClientRect();

      // Play — animate to "Last" (natural) position
      el.style.transition = `transform ${duration}ms ${easing}, opacity ${duration * 0.8}ms ease`;
      el.style.transform  = '';

      // Cleanup after animation
      const cleanup = () => {
        el.style.transition    = '';
        el.style.transform     = '';
        el.style.transformOrigin = '';
        el.style.zIndex        = '';
        el.style.pointerEvents = '';
      };
      el.addEventListener('transitionend', cleanup, { once: true });
    }
  }, []);

  return { captureFirst, playFlip };
}
