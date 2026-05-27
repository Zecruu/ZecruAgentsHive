// Bridge between the sandboxed renderer and the main process. Keep this
// surface small — everything the renderer can do has to go through here.

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('agentshive', {
  config: {
    get: () => ipcRenderer.invoke('config:get'),
    set: (patch) => ipcRenderer.invoke('config:set', patch),
  },
  projects: {
    list: () => ipcRenderer.invoke('projects:list'),
    create: (slug, name) => ipcRenderer.invoke('projects:create', { slug, name }),
  },
  agent: {
    launch: (payload) => ipcRenderer.invoke('agent:launch', payload),
  },
  dashboard: {
    url: (projectSlug) => ipcRenderer.invoke('dashboard:url', { projectSlug }),
    open: (projectSlug) => ipcRenderer.invoke('dashboard:open', { projectSlug }),
  },
  app: {
    hostname: () => ipcRenderer.invoke('app:hostname'),
  },
});
