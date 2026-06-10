import type { DetailedHTMLProps, HTMLAttributes } from "react";

type KicanvasEmbedProps = DetailedHTMLProps<
  HTMLAttributes<HTMLElement>,
  HTMLElement
> & {
  controls?: "none" | "basic" | "full";
  controlslist?: string;
  src?: string;
  theme?: string;
  zoom?: string;
};

type KicanvasSourceProps = DetailedHTMLProps<
  HTMLAttributes<HTMLElement>,
  HTMLElement
> & {
  src?: string;
  type?: "schematic" | "board" | "project" | "worksheet";
  name?: string;
};

declare module "react" {
  namespace JSX {
    interface IntrinsicElements {
      "kicanvas-embed": KicanvasEmbedProps;
      "kicanvas-source": KicanvasSourceProps;
    }
  }
}
