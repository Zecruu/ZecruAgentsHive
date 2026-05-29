import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import './index.css';
import { applyTheme, getCachedTheme, reconcileTheme } from './lib/theme';

// Apply the cached theme synchronously BEFORE first paint (no flash), then
// reconcile from the durable userData store (survives updates).
applyTheme(getCachedTheme());
reconcileTheme();

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
