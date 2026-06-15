export type Question = {
  id: string;
  q: string;
  options: string[];
  answer?: string;
};

// Fuzzy "cost to finish from here" range shown on a Confirm/Discard gate.
// `credits` on the cloud path, `usd` on self/BYOK. From the backend ev_ai cost.
export type GateCost = {
  unit: "credits" | "usd";
  lo: number;
  hi: number;
  balance: number | null;
};

export type DiffRow = {
  sym: "+" | "-" | "~";
  ref: string;
  part: string;
  note: string;
};

export type ToolMetaRow = { k: string; v: string };

export type BlockDiagramNode = { id: string; role: string; mpn?: string };
export type BlockDiagramEdge = { from: string; to: string; interface: string };

export type DesignSpecComponent = {
  purpose: string;
  refdes_hint: string;
  component: string;
  value: string;
  chip_pin_number?: string | null;
  equation?: string | null;
  tolerance?: string | null;
  source: "computed" | "datasheet";
};
export type DesignSpecRail = {
  pin_number: string;
  pin_name: string;
  rail: string;
};
export type DesignSpecData = {
  mpn: string;
  orderable_part?: string | null;
  variant_code?: string | null;
  topology: string;
  role?: string | null;
  vin: string;
  vout: string;
  duty_cycle?: number | null;
  components: DesignSpecComponent[];
  rail_map: DesignSpecRail[];
  blurb: string;
  warnings: string[];
};

export type UserAttachment = {
  filename: string;
  size: number;
  mime: string;
};

export type Message =
  | { id: string; kind: "user"; text: string; attachments?: UserAttachment[] }
  | {
      id: string;
      kind: "ai";
      text: string;
      questions?: Question[];
      diff?: DiffRow[];
      confirm?: boolean;
      locked?: boolean;
      cost?: GateCost | null;
    }
  | { id: string; kind: "thinking"; text: string; streaming: boolean }
  | { id: string; kind: "tool"; tool: string; title: string; meta: ToolMetaRow[]; live?: boolean }
  | { id: string; kind: "action"; actKind: "place" | "route"; text: string }
  | { id: string; kind: "system"; text: string }
  | {
      id: string;
      kind: "block_diagram";
      nodes: BlockDiagramNode[];
      edges: BlockDiagramEdge[];
    }
  | { id: string; kind: "design_spec"; spec: DesignSpecData }
  | { id: string; kind: "signin_required"; hint: string };
