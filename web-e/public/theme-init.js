(function () {
  try {
    var key = 'maraclaw-theme'
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
