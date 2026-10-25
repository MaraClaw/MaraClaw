import { createInvitationsHarness, expect } from './invitations-browser-harness.mjs'

const harness = await createInvitationsHarness()
let completed = false
try {
  for (const width of [390, 1440, 1920]) for (const scheme of ['light', 'dark']) {
    await harness.scenario({ name: `before shell ${width} ${scheme}`, width, scheme, path: 'broadcast' }, async (page) => {
      await expect(page.getByRole('heading', { name: 'Organization broadcast' })).toBeVisible()
      await harness.capture(page, `before-shell-${width}-${scheme}`)
    })
  }
  completed = true
} finally { await harness.close(completed) }
