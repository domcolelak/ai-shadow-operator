/**
 * Typed client for the AI Shadow Operator API.
 *
 * All calls run on the Next.js server, so the tenant API key never reaches the
 * browser.
 */

const BASE_URL = process.env.API_BASE_URL ?? "http://localhost:8000";
const API_KEY = process.env.API_KEY ?? "";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${BASE_URL}/v1${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(API_KEY ? { "X-API-Key": API_KEY } : {}),
      ...(init.headers ?? {}),
    },
    cache: "no-store",
  });
  if (!response.ok) {
    throw new ApiError(`${path} failed: ${(await response.text()).slice(0, 300)}`, response.status);
  }
  return (await response.json()) as T;
}

export async function tryRequest<T>(path: string, init: RequestInit = {}): Promise<T | null> {
  try {
    return await request<T>(path, init);
  } catch {
    return null;
  }
}

// --- types ---------------------------------------------------------------

export interface ConsentPolicy {
  allowed_origins: string[];
  blocked_origins: string[];
  extra_sensitive_fields: string[];
  allowed_connectors: string[];
  screenshots_captured: boolean;
  keystrokes_captured: boolean;
  values_stored: boolean;
  note: string;
}

export interface RecordingSession {
  id: string;
  external_id: string;
  user_email: string;
  device: string;
  status: string;
  label: string;
  action_count: number;
  rejected_count: number;
  rejection_reasons: string[];
  started_at: string;
  completed_at: string | null;
}

export interface WorkflowStep {
  position: number;
  signature: string;
  action: string;
  origin: string;
  role: string;
  label: string;
  field_name: string;
  target_path: string;
  value_class: string;
  presence: number;
  optional: boolean;
  requires_human: boolean;
}

export interface StepVariable {
  step_index: number;
  field_name: string;
  label: string;
  value_shape: string;
  distinct_values: number;
  observations: number;
}

export interface BranchPoint {
  after_step: number;
  alternatives: { signature: string; runs: number; share: number }[];
}

export interface Candidate {
  id: string;
  fingerprint: string;
  name: string;
  observation_count: number;
  confidence: number;
  median_duration_seconds: number;
  estimated_seconds_saved: number;
  steps: WorkflowStep[];
  variables: StepVariable[];
  branches: BranchPoint[];
  session_ids: string[];
  status: string;
  narrative: Record<string, unknown>;
  created_at: string;
}

export interface DslAction {
  kind: string;
  label: string;
  selector: { role: string; name: string; field_name: string } | null;
  path: string;
  origin: string;
  variable: string | null;
  optional: boolean;
}

export interface WorkflowDefinition {
  name: string;
  description: string;
  trigger: string;
  allowed_origins: string[];
  variables: { name: string; label: string; shape: string; required: boolean }[];
  actions: DslAction[];
}

export interface Workflow {
  id: string;
  candidate_id: string | null;
  name: string;
  description: string;
  state: string;
  definition: WorkflowDefinition;
  compilation_notes: { step_index: number | null; message: string }[];
  risk: string;
  autonomous_medium_risk: boolean;
  created_at: string;
  updated_at: string;
}

export interface WorkflowDetail extends Workflow {
  validation_issues: { index: number | null; severity: string; message: string }[];
  requires_approval: boolean;
  high_risk_steps: { index: number; label: string }[];
}

export interface Execution {
  id: string;
  workflow_id: string;
  dry_run: boolean;
  status: string;
  supplied_variables: string[];
  approved_steps: number[];
  paused_at: number | null;
  pending_approval: string | null;
  steps: {
    index: number;
    kind: string;
    label: string;
    status: string;
    risk: string;
    input_summary: Record<string, unknown>;
    error: string | null;
  }[];
  error: string | null;
  started_by: string;
  created_at: string;
}

export interface Overview {
  session_count: number;
  action_count: number;
  rejected_count: number;
  candidate_count: number;
  workflow_count: number;
  enabled_workflows: number;
  executions: number;
  live_executions: number;
  awaiting_approval: number;
  estimated_seconds_saved: number;
  consent: ConsentPolicy;
  top_candidates: Candidate[];
}

// --- endpoints -----------------------------------------------------------

export const api = {
  overview: () => tryRequest<Overview>("/overview"),
  consent: () => tryRequest<ConsentPolicy>("/consent"),
  sessions: () => tryRequest<RecordingSession[]>("/sessions"),
  candidates: () => tryRequest<Candidate[]>("/workflow-candidates"),
  candidate: (id: string) => tryRequest<Candidate>(`/workflow-candidates/${id}`),
  workflows: () => tryRequest<Workflow[]>("/workflows"),
  workflow: (id: string) => tryRequest<WorkflowDetail>(`/workflows/${id}`),
  executions: () => tryRequest<Execution[]>("/executions"),
};
