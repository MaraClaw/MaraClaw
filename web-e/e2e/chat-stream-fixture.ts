import { expect, type Page, type WebSocketRoute } from '@playwright/test'

type ChatSocket = { readonly route: WebSocketRoute }

const start = new Date('2030-01-01T00:00:00Z')

// Install before navigating, like the other clock-driven specs, so the page's animation frames are the clock's.
export async function installFrameClock(page: Page) {
  await page.clock.install({ time: start })
}

// Once the chat has settled: from here an animation frame, and every other timer, runs only when the test advances it.
export async function holdFrames(page: Page) {
  await page.clock.pauseAt(new Date(start.getTime() + 60_000))
}

// One frame interval, which runs every animation frame requested so far.
export async function paintFrame(page: Page) {
  await page.clock.runFor(16)
}

export function sendFrame(socket: ChatSocket, event: Record<string, unknown>) {
  socket.route.send(JSON.stringify(event))
}

export function sendChunk(socket: ChatSocket, turn: string, seq: number, content: string) {
  sendFrame(socket, { type: 'chunk', turn_id: turn, seq, content })
}

let barriers = 0

// Frames on one socket are handled in order, so once the barrier renders every frame sent before it was accepted.
export async function handled(page: Page, socket: ChatSocket) {
  const barrier = `Barrier ${++barriers}`
  sendFrame(socket, { type: 'info', content: barrier })
  await expect(page.getByText(barrier, { exact: true })).toBeVisible()
}
