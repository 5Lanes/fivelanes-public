export type LooseObj = Record<string, unknown>;

export type ThreadView = {
  id: string;
  messages: Array<{ cleaned: LooseObj | null; summary: LooseObj | null }>;
};

export type AppRoute =
  | "dashboard"
  | "threads"
  | "meetings"
  | "lanes"
  | "people"
  | "plans"
  | "texts-setup";

export type LaneView = {
  id: number;
  name: string;
  created_at: string;
  updated_at: string;
};

export type LaneSummaryView = {
  summary: string;
  highlights: string[];
  current_priorities: string[];
  waiting_on_others: string[];
  tone_overview: string;
  updated_at: string;
};

export type PersonView = {
  id: number;
  name: string;
  created_at: string;
  updated_at: string;
};

export type PersonSummaryView = {
  summary: string;
  highlights: string[];
  current_priorities: string[];
  waiting_on_others: string[];
  tone_overview: string;
  updated_at: string;
};

export type PlanView = {
  id: number;
  inbox_thread_id: string;
  action: string;
  step_type: string;
  by_when: string;
  created_at: string;
  updated_at: string;
};

export type SlotMention = {
  raw: string;
  start: number;
  end: number;
  date_key: string;
  start_minute: number;
  end_minute: number;
  label: string;
};

export type AvailabilityStatus = "open" | "virtual-only" | "busy" | "blocked" | "unknown";

export type AvailabilityMatch = {
  status: AvailabilityStatus;
  date_key: string;
  start_minute: number;
  end_minute: number;
  details: string;
};
