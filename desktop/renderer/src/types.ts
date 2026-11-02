/** Renderer-side view types (mirroring the backend payloads). */

export interface RectTuple extends Array<number> {
  0: number;
  1: number;
  2: number;
  3: number;
}

export interface Highlight {
  element_id: string | null;
  rect: RectTuple;
  label: string;
  instruction: string;
  confidence: number;
  source: string;
  pulsing: boolean;
  number: number | null;
  arrow: string;
}

export interface TextToEnter {
  value: string;
  field_label: string;
  source: string;
  sensitive: boolean;
  copyable: boolean;
  explanation: string;
}

export interface Guidance {
  step_id: string;
  instruction: string;
  action_type: string;
  highlight: Highlight | null;
  text_to_enter: TextToEnter | null;
  expected_result: string;
  verification_method: string;
  requires_confirmation: boolean;
  confidence: number;
  why: string;
  alternatives: Array<Record<string, unknown>>;
  warnings: string[];
  notes: string[];
}

export interface StepView {
  id: string;
  index: number;
  title: string;
  instruction: string;
  action_type: string;
  target_description: string;
  text_value: Record<string, unknown> | null;
  expected_result: string;
  why: string;
  status: string;
  attempts: number;
  requires_user_input: boolean;
  risk: string;
  rules: Array<{ kind: string; params: Record<string, unknown>; required: boolean }>;
}

export interface TaskView {
  id: string;
  objective: string;
  state: string;
  steps: StepView[];
  current_index: number;
  current_step: StepView | null;
  summary: string;
  app_context: string;
  progress: { total: number; completed: number };
  history: Array<Record<string, unknown>>;
  last_verified_step: number;
  paused_reason: string;
}

export interface VerificationView {
  outcome: 'VERIFIED' | 'FAILED' | 'INCONCLUSIVE';
  summary: string;
  evidence: Array<{ rule: string; passed: boolean | null; detail: string }>;
}

export interface ElementView {
  id: string;
  name: string;
  role: string;
  rect: RectTuple;
  value: string | null;
  source: string;
  confidence: number;
}

export interface ObservationView {
  id: string;
  element_count: number;
  elements: ElementView[];
  sensitive: boolean;
  active_window: { title: string; app_class: string; rect: RectTuple } | null;
  sources: Record<string, any>;
  frame: { change_ratio: number; width: number; height: number; backend: string } | null;
  notes: string[];
}

export interface TaskStatePayload {
  task: TaskView | null;
  guidance: Guidance | null;
  verification: VerificationView | null;
  observation: ObservationView | null;
  warnings: string[];
  can: Record<string, boolean>;
  overlay_mode?: string;
  session_type?: string;
  monitors?: Array<{ id: string; name: string; x: number; y: number; width: number; height: number; scale: number }>;
  privacy?: Record<string, unknown>;
}

export interface Notice {
  message: string;
  level: 'info' | 'warning' | 'error';
  at: number;
}

export interface ChatEntry {
  role: 'user' | 'assistant';
  text: string;
  source?: string;
  warnings?: string[];
}
