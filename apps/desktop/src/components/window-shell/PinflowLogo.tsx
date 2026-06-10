import { useId } from "react";

// The official Pinflow mark: a royal disc with a white knockout routing trace
// + via passing through (reads as PCB routing). Brand colors are fixed —
// royal #6D28D9 / white #FFFFFF — and must not recolor per theme.
// Source: assets/svg/pinflow-mark.svg.
export function PinflowLogo({ size = 20 }: { size?: number }) {
  const clipId = useId();
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 512 512"
      fill="none"
      aria-hidden="true"
    >
      <circle cx="256" cy="256" r="158.72" fill="#6D28D9" />
      <clipPath id={clipId}>
        <circle cx="256" cy="256" r="158.72" />
      </clipPath>
      <g clipPath={`url(#${clipId})`}>
        <path
          d="M 97.28 211.558 L 186.163 211.558 L 256 281.395 L 325.837 211.558 L 414.72 211.558"
          fill="none"
          stroke="#FFFFFF"
          strokeWidth="34.918"
          strokeLinejoin="round"
        />
        <circle cx="256" cy="281.395" r="34.918" fill="#FFFFFF" />
        <circle cx="256" cy="281.395" r="12.698" fill="#6D28D9" />
      </g>
    </svg>
  );
}
