import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import App from './App.tsx'
import { ThemeProvider } from './hooks/use-theme.tsx'
import './index.css'

const CHUNK_RELOAD_KEY = 'maraclaw-admin-chunk-reload'

// A deploy replaced the hashed chunks this tab was built against; reload once per URL to fetch the new build.
window.addEventListener('vite:preloadError', (event) => {
  if (sessionStorage.getItem(CHUNK_RELOAD_KEY) === window.location.href) return
  sessionStorage.setItem(CHUNK_RELOAD_KEY, window.location.href)
  event.preventDefault()
  window.location.reload()
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ThemeProvider>
      <App />
    </ThemeProvider>
  </StrictMode>,
)
