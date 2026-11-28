// Applies the saved theme before first paint. External, because the CSP allows no inline script; the key is the one
// src/hooks/use-theme.tsx writes.
(function () {
  try {
    var key = 'maraclaw-admin-theme'
    var stored = localStorage.getItem(key)
    var theme = stored === 'light' || stored === 'dark' || stored === 'system' ? stored : 'system'
    var dark =
      theme === 'dark' ||
      (theme === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches)
    document.documentElement.classList.toggle('dark', dark)
    document.documentElement.style.colorScheme = dark ? 'dark' : 'light'
    document.documentElement.dataset.theme = dark ? 'dark' : 'light'
    var meta = document.querySelector('meta[name="theme-color"]')
    if (meta) meta.setAttribute('content', dark ? '#1c1612' : '#faf8f5')
  } catch {}
})()
