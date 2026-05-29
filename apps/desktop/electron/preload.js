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
  paths: {
    get: (projectSlug) => ipcRenderer.invoke('paths:get', { projectSlug }),
    set: (projectSlug, path) => ipcRenderer.invoke('paths:set', { projectSlug, path }),
    pick: (projectSlug) => ipcRenderer.invoke('paths:pick', { projectSlug }),
  },
  prefs: {
    get: (projectSlug) => ipcRenderer.invoke('prefs:get', { projectSlug }),
    set: (projectSlug, prefs) => ipcRenderer.invoke('prefs:set', { projectSlug, prefs }),
  },
  workspace: {
    get: () => ipcRenderer.invoke('workspace:get'),
    set: (patch) => ipcRenderer.invoke('workspace:set', patch),
  },
  agents: {
    list: (projectSlug) => ipcRenderer.invoke('agents:list', { projectSlug }),
    save: (projectSlug, agent) => ipcRenderer.invoke('agents:save', { projectSlug, agent }),
    delete: (projectSlug, agentId) => ipcRenderer.invoke('agents:delete', { projectSlug, agentId }),
  },
  pty: {
    available: () => ipcRenderer.invoke('pty:available'),
    spawn: (opts) => ipcRenderer.invoke('pty:spawn', opts),
    write: (id, data) => ipcRenderer.invoke('pty:write', { id, data }),
    resize: (id, cols, rows) => ipcRenderer.invoke('pty:resize', { id, cols, rows }),
    kill: (id) => ipcRenderer.invoke('pty:kill', { id }),
    sendCmd: (id, text) => ipcRenderer.invoke('pty:send-cmd', { id, text }),
    onData: (id, cb) => {
      const ch = `pty:data:${id}`;
      const handler = (_e, data) => cb(data);
      ipcRenderer.on(ch, handler);
      return () => ipcRenderer.removeListener(ch, handler);
    },
    onExit: (id, cb) => {
      const ch = `pty:exit:${id}`;
      const handler = (_e, payload) => cb(payload);
      ipcRenderer.on(ch, handler);
      return () => ipcRenderer.removeListener(ch, handler);
    },
  },
  agent: {
    launch: (payload) => ipcRenderer.invoke('agent:launch', payload),
    embed: (payload) => ipcRenderer.invoke('agent:embed', payload),
  },
  chat: {
    send: (payload) => ipcRenderer.invoke('chat:send', payload),
    cancel: (chatId) => ipcRenderer.invoke('chat:cancel', { chatId }),
    onEvent: (chatId, cb) => {
      const ch = `chat:event:${chatId}`;
      const h = (_e, ev) => cb(ev);
      ipcRenderer.on(ch, h);
      return () => ipcRenderer.removeListener(ch, h);
    },
    onStderr: (chatId, cb) => {
      const ch = `chat:stderr:${chatId}`;
      const h = (_e, t) => cb(t);
      ipcRenderer.on(ch, h);
      return () => ipcRenderer.removeListener(ch, h);
    },
    onDone: (chatId, cb) => {
      const ch = `chat:done:${chatId}`;
      const h = (_e, p) => cb(p);
      ipcRenderer.on(ch, h);
      return () => ipcRenderer.removeListener(ch, h);
    },
    onError: (chatId, cb) => {
      const ch = `chat:error:${chatId}`;
      const h = (_e, p) => cb(p);
      ipcRenderer.on(ch, h);
      return () => ipcRenderer.removeListener(ch, h);
    },
  },
  dashboard: {
    url: (projectSlug) => ipcRenderer.invoke('dashboard:url', { projectSlug }),
    open: (projectSlug) => ipcRenderer.invoke('dashboard:open', { projectSlug }),
    state: (projectSlug) => ipcRenderer.invoke('dashboard:state', { projectSlug }),
  },
  app: {
    hostname: () => ipcRenderer.invoke('app:hostname'),
    version: () => ipcRenderer.invoke('app:version'),
  },
  authStore: {
    get: (key) => ipcRenderer.invoke('authstore:get', { key }),
    set: (key, value) => ipcRenderer.invoke('authstore:set', { key, value }),
    remove: (key) => ipcRenderer.invoke('authstore:remove', { key }),
  },
  skills: {
    list: (projectSlug) => ipcRenderer.invoke('skills:list', { projectSlug }),
  },
  admin: {
    listUsers: (token) => ipcRenderer.invoke('admin:listUsers', { token }),
    setBanned: (token, sub, banned) => ipcRenderer.invoke('admin:setBanned', { token, sub, banned }),
    setPlan: (token, sub, plan) => ipcRenderer.invoke('admin:setPlan', { token, sub, plan }),
    removeUser: (token, sub) => ipcRenderer.invoke('admin:removeUser', { token, sub }),
  },
  missions: {
    syncDocs: (projectSlug) => ipcRenderer.invoke('missions:syncDocs', { projectSlug }),
    export: (projectSlug) => ipcRenderer.invoke('missions:export', { projectSlug }),
  },
  web: {
    presence: (token, project, agents) => ipcRenderer.invoke('web:presence', { token, project, agents }),
    inbound: (token) => ipcRenderer.invoke('web:inbound', { token }),
    ack: (token, messageId) => ipcRenderer.invoke('web:ack', { token, messageId }),
    relay: (token, parentId, project, agentKey, body) => ipcRenderer.invoke('web:relay', { token, parentId, project, agentKey, body }),
    me: (token) => ipcRenderer.invoke('web:me', { token }),
    syncPush: (token, payload) => ipcRenderer.invoke('web:syncPush', { token, payload }),
    syncPull: (token, project, since) => ipcRenderer.invoke('web:syncPull', { token, project, since }),
  },
  tools: {
    status: () => ipcRenderer.invoke('tools:status'),
    connect: (tool) => ipcRenderer.invoke('tools:connect', { tool }),
  },
  attachments: {
    save: (payload) => ipcRenderer.invoke('attachments:save', payload),
  },
  updates: {
    onAvailable: (cb) => {
      const h = (_e, info) => cb(info);
      ipcRenderer.on('update:available', h);
      return () => ipcRenderer.removeListener('update:available', h);
    },
    onProgress: (cb) => {
      const h = (_e, p) => cb(p);
      ipcRenderer.on('update:progress', h);
      return () => ipcRenderer.removeListener('update:progress', h);
    },
    onDownloaded: (cb) => {
      const h = (_e, info) => cb(info);
      ipcRenderer.on('update:downloaded', h);
      return () => ipcRenderer.removeListener('update:downloaded', h);
    },
    onError: (cb) => {
      const h = (_e, p) => cb(p);
      ipcRenderer.on('update:error', h);
      return () => ipcRenderer.removeListener('update:error', h);
    },
    quitAndInstall: () => ipcRenderer.invoke('update:quitAndInstall'),
  },
});
