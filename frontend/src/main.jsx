import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { Toaster } from 'react-hot-toast';
import { PostHogProvider } from '@posthog/react';
import App from './App.jsx';
import './theme.css';

// `as const` from the PostHog docs snippet is TypeScript-only -- this is a
// plain .jsx file, not .tsx, so it's dropped here; the object's the same.
const posthogOptions = {
  api_host: import.meta.env.VITE_POSTHOG_HOST,
  defaults: '2026-05-30',
};

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <PostHogProvider apiKey={import.meta.env.VITE_POSTHOG_PROJECT_TOKEN} options={posthogOptions}>
      <BrowserRouter>
        <App />
        <Toaster
          position="bottom-right"
          toastOptions={{
            style: {
              background: 'var(--surface)',
              color: 'var(--text)',
              border: '1px solid var(--border2)',
              fontFamily: 'var(--font-mono)',
              fontSize: '0.75rem',
              backdropFilter: 'blur(12px)',
            },
          }}
        />
      </BrowserRouter>
    </PostHogProvider>
  </React.StrictMode>
);