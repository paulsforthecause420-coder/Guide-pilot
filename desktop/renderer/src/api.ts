/** Typed wrapper around the preload IPC bridge. */

export interface ApiFailure {
  code: string;
  message: string;
  detail?: unknown;
}

export class ApiError extends Error {
  code: string;
  detail?: unknown;
  constructor(failure?: ApiFailure) {
    super(failure?.message ?? 'The local GuidePilot service could not complete that request.');
    this.code = failure?.code ?? 'unknown';
    this.detail = failure?.detail;
    this.name = 'ApiError';
  }
}

interface IpcResult<T> {
  ok: boolean;
  data?: T;
  error?: ApiFailure;
}

async function call<T>(operation: () => Promise<IpcResult<T>>): Promise<T> {
  let result: IpcResult<T>;
  try {
    result = await operation();
  } catch (error) {
    throw new ApiError({ code: 'ipc', message: error instanceof Error ? error.message : String(error) });
  }
  if (!result || result.ok !== true) throw new ApiError(result?.error);
  return result.data as T;
}

type GuidePilotBridge = Window['guidepilot'];

function bridge(): GuidePilotBridge {
  if (!window.guidepilot) throw new ApiError({ code: 'preload_missing', message: 'GuidePilot bridge is unavailable.' });
  return window.guidepilot;
}

export const api = {
  capabilities: () => call<any>(() => bridge().app.capabilities()),
  settings: () => call<any>(() => bridge().app.settings()),
  updateSettings: (patch: unknown) => call<any>(() => bridge().app.updateSettings(patch)),
  setApiKey: (apiKey: string) => call<any>(() => bridge().app.setApiKey(apiKey)),
  deleteApiKey: () => call<any>(() => bridge().app.deleteApiKey()),
  usage: () => call<any>(() => bridge().app.usage()),
  audit: (limit = 100) => call<any>(() => bridge().app.audit(limit)),

  createTask: (payload: {
    objective: string;
    contextValues?: Record<string, string>;
    appHint?: string;
    projectDir?: string;
  }) => call<any>(() => bridge().task.create(payload)),
  taskState: () => call<any>(() => bridge().task.state()),
  action: (action: string, payload?: unknown) => call<any>(() => bridge().task.action(action, payload)),
  chat: (message: string) => call<any>(() => bridge().task.chat(message)),
  listTasks: () => call<any>(() => bridge().task.list()),
  resumePrompt: () => call<any>(() => bridge().task.resumePrompt()),
  resumeTask: (taskId: string, contextValues?: Record<string, string>) =>
    call<any>(() => bridge().task.resume(taskId, contextValues)),
  clearTasks: () => call<any>(() => bridge().task.clear()),
  deleteTask: (taskId: string) => call<any>(() => bridge().task.delete(taskId)),
  setContext: (values: Record<string, string>) => call<any>(() => bridge().task.setContext(values)),

  workflows: () => call<any>(() => bridge().workflows.list()),
  startWorkflow: (workflowId: string, contextValues?: Record<string, string>) =>
    call<any>(() => bridge().workflows.start(workflowId, contextValues)),
  exportWorkflow: (workflowId: string) => call<any>(() => bridge().workflows.export(workflowId)),
  importWorkflow: (workflow: unknown) => call<any>(() => bridge().workflows.import(workflow)),
  deleteWorkflow: (workflowId: string) => call<any>(() => bridge().workflows.remove(workflowId)),

  emergencyStop: () => call<any>(() => bridge().privacy.stop()),
  enableSharing: (acknowledgeCloudVision = false) => call<any>(() => bridge().privacy.enable(acknowledgeCloudVision)),
  purge: () => call<any>(() => bridge().privacy.purge()),

  speechStatus: () => call<any>(() => bridge().speech.status()),
  speak: (text: string) => call<any>(() => bridge().speech.speak(text)),

  paste: (text: string, confirmed: boolean) => call<any>(() => bridge().actions.paste(text, confirmed)),

  minimize: () => call<any>(() => bridge().window.minimize()),
  close: () => call<any>(() => bridge().window.close()),
  collapse: (compact: boolean) => call<any>(() => bridge().window.collapse(compact)),
  setOpacity: (opacity: number) => call<any>(() => bridge().window.setOpacity(opacity)),
  setAlwaysOnTop: (enabled: boolean) => call<any>(() => bridge().window.setAlwaysOnTop(enabled)),
  dock: (edge: string) => call<any>(() => bridge().window.dock(edge)),
  hideOverlay: () => call<any>(() => bridge().window.hideOverlay()),
  previewHighlight: (payload: unknown) => call<any>(() => bridge().window.previewHighlight(payload)),
};
