import { useEffect, useRef, useState } from "react";

// Cosmetic easing for a number that updates in discrete jumps — e.g. the cost
// meter, which only gets a new value per completed LLM call (the gateway path is
// non-streaming, so there's no token-by-token stream to read). We ease UP toward
// a rising target with an ease-out cubic so the readout "counts up" smoothly,
// and snap DOWN instantly so a per-request reset is crisp rather than a weird
// countdown. The underlying data is unchanged — only the in-between frames are
// interpolated. Honors prefers-reduced-motion.
const _reduceMotion =
  typeof window !== "undefined" &&
  typeof window.matchMedia === "function" &&
  window.matchMedia("(prefers-reduced-motion: reduce)").matches;

export function useEasedNumber(target: number, durationMs = 450): number {
  const [display, setDisplay] = useState(target);
  const displayRef = useRef(target);
  const rafRef = useRef<number | null>(null);

  useEffect(() => {
    const cancel = () => {
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    };
    const from = displayRef.current;
    // Snap on reset/decrease (or reduced motion); ease only when counting up.
    if (_reduceMotion || target <= from) {
      cancel();
      displayRef.current = target;
      setDisplay(target);
      return;
    }
    const start = performance.now();
    const step = (now: number) => {
      const t = Math.min(1, (now - start) / durationMs);
      const eased = 1 - Math.pow(1 - t, 3); // ease-out cubic
      const v = from + (target - from) * eased;
      displayRef.current = v;
      setDisplay(v);
      rafRef.current = t < 1 ? requestAnimationFrame(step) : null;
    };
    cancel();
    rafRef.current = requestAnimationFrame(step);
    return cancel;
  }, [target, durationMs]);

  return display;
}
