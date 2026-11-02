/**
 * Preload bridge.
 *
 * The renderer is sandboxed with context isolation and never receives the
 * backend token or Node APIs. Only the whitelisted channels below are exposed.
 */
import { contextBridge, ipcRenderer } from 'electron';

type Unsubscribe = () => void;

function subscribe(channel: string, listener: (payload: any) => void): Unsubscribe {
  const wrapped = (_event: unknown, payload: any) => listener(payload);
  ipcRenderer.on(channel, wrapped);
  return () => ipcRenderer.removeListener(channel, wrapped);
}

const api = {
  app: {
    capabilities: () => ipcRenderer.invoke('app:capabilities'),
    settings: () => ipcRenderer.invoke('app:settings'),
    updateSettings: (patch: unknown) => ipcRenderer.invoke('app:settings:update', patch),
    setApiKey: (apiKey: string) => ipcRenderer.invoke('app:apikey:set', { apiKey }),
    deleteApiKey: () => ipcRenderer.invoke('app:apikey:delete'),
    usage: () => ipcRenderer.invoke('app:usage'),
    audit: (limit?: number) => ipcRenderer.invoke('app:audit', { limit }),
  },
  task: {
    create: (payload: unknown) => ipcRenderer.invoke('task:create', payload),
    state: () => ipcRenderer.invoke('task:state'),
    action: (action: string, payload?: unknown) => ipcRenderer.invoke('task:action', { action, payload }),
    chat: (message: string) => ipcRenderer.invoke('task:chat', { message }),
    list: () => ipcRenderer.invoke('task:list'),
    resumePrompt: () => ipcRenderer.invoke('task:resume-prompt'),
    resume: (taskId: string, contextValues?: unknown) =>
      ipcRenderer.invoke('task:resume', { taskId, contextValues }),
    clear: () => ipcRenderer.invoke('task:clear'),
    delete: (taskId: string) => ipcRenderer.invoke('task:delete', { taskId }),
    setContext: (values: unknown) => ipcRenderer.invoke('task:context', { values }),
  },
  workflows: {
    list: () => ipcRenderer.invoke('workflows:list'),
    start: (workflowId: string, contextValues?: unknown) =>
      ipcRenderer.invoke('workflows:start', { workflowId, contextValues }),
    export: (workflowId: string) => ipcRenderer.invoke('workflows:export', { workflowId }),
    import: (workflow: unknown) => ipcRenderer.invoke('workflows:import', { workflow }),
    remove: (workflowId: string) => ipcRenderer.invoke('workflows:delete', { workflowId }),
  },
  privacy: {
    stop: () => ipcRenderer.invoke('privacy:stop'),
    enable: (acknowledgeCloudVision?: boolean) => ipcRenderer.invoke('privacy:enable', { acknowledgeCloudVision }),
    purge: () => ipcRenderer.invoke('privacy:purge'),
  },
  speech: {
    status: () => ipcRenderer.invoke('speech:status'),
    speak: (text: string) => ipcRenderer.invoke('speech:speak', { text }),
  },
  actions: {
    paste: (text: string, confirmed: boolean) => ipcRenderer.invoke('actions:paste', { text, confirmed }),
  },
  window: {
    minimize: () => ipcRenderer.invoke('window:minimize'),
    close: () => ipcRenderer.invoke('window:close'),
    collapse: (compact: boolean) => ipcRenderer.invoke('window:collapse', { compact }),
    setOpacity: (opacity: number) => ipcRenderer.invoke('window:opacity', { opacity }),
    setAlwaysOnTop: (enabled: boolean) => ipcRenderer.invoke('window:always-on-top', { enabled }),
    dock: (edge: string) => ipcRenderer.invoke('window:dock', { edge }),
    hideOverlay: () => ipcRenderer.invoke('overlay:hide'),
    previewHighlight: (payload: unknown) => ipcRenderer.invoke('overlay:preview', payload),
  },
  events: {
    onTaskState: (listener: (payload: any) => void) => subscribe('task:state', listener),
    onOverlayRender: (listener: (payload: any) => void) => subscribe('overlay:render', listener),
    onNotice: (listener: (payload: any) => void) => subscribe('app:notice', listener),
    onShortcut: (listener: (payload: any) => void) => subscribe('app:shortcut', listener),
  },
};

contextBridge.exposeInMainWorld('guidepilot', api);

export type GuidePilotApi = typeof api;
