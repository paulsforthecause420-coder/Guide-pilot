const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('demo', {
  reportState: (state) => ipcRenderer.send('demo-state', state),
});
