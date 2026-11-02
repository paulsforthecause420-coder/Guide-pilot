export {};

interface IpcResult<T = any> {
  ok: boolean;
  data?: T;
  error?: { code: string; message: string; detail?: unknown };
}

type Ipc<T = any> = Promise<IpcResult<T>>;

declare global {
  interface Window {
    guidepilot: {
      app: {
        capabilities: () => Ipc;
        settings: () => Ipc;
        updateSettings: (patch: unknown) => Ipc;
        setApiKey: (apiKey: string) => Ipc;
        deleteApiKey: () => Ipc;
        usage: () => Ipc;
        audit: (limit?: number) => Ipc;
      };
      task: {
        create: (payload: unknown) => Ipc;
        state: () => Ipc;
        action: (action: string, payload?: unknown) => Ipc;
        chat: (message: string) => Ipc;
        list: () => Ipc;
        resumePrompt: () => Ipc;
        resume: (taskId: string, contextValues?: unknown) => Ipc;
        clear: () => Ipc;
        delete: (taskId: string) => Ipc;
        setContext: (values: unknown) => Ipc;
      };
      workflows: {
        list: () => Ipc;
        start: (workflowId: string, contextValues?: unknown) => Ipc;
        export: (workflowId: string) => Ipc;
        import: (workflow: unknown) => Ipc;
        remove: (workflowId: string) => Ipc;
      };
      privacy: {
        stop: () => Ipc;
        enable: (acknowledgeCloudVision?: boolean) => Ipc;
        purge: () => Ipc;
      };
      speech: {
        status: () => Ipc;
        speak: (text: string) => Ipc;
      };
      actions: {
        paste: (text: string, confirmed: boolean) => Ipc;
      };
      window: {
        minimize: () => Ipc;
        close: () => Ipc;
        collapse: (compact: boolean) => Ipc;
        setOpacity: (opacity: number) => Ipc;
        setAlwaysOnTop: (enabled: boolean) => Ipc;
        dock: (edge: string) => Ipc;
        hideOverlay: () => Ipc;
        previewHighlight: (payload: unknown) => Ipc;
      };
      events: {
        onTaskState: (listener: (payload: any) => void) => () => void;
        onOverlayRender: (listener: (payload: any) => void) => () => void;
        onNotice: (listener: (payload: any) => void) => () => void;
        onShortcut: (listener: (payload: any) => void) => () => void;
      };
    };
    overlay?: {
      onRender: (listener: (payload: any) => void) => () => void;
    };
  }
}
