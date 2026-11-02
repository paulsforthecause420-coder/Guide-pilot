/** Preload for the transparent highlight overlay (render-only, no privileges). */
import { contextBridge, ipcRenderer } from 'electron';

contextBridge.exposeInMainWorld('overlay', {
  onRender: (listener: (payload: any) => void) => {
    const wrapped = (_event: unknown, payload: any) => listener(payload);
    ipcRenderer.on('overlay:render', wrapped);
    return () => ipcRenderer.removeListener('overlay:render', wrapped);
  },
});
