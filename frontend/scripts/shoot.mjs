// Screenshot helper for the visual feedback loop.
//
// Captures the marketing page (public) or the authenticated home page, in
// light + dark, to ./.shots/ so we can *see* what we built and iterate.
//
// Usage:
//   node scripts/shoot.mjs                                  # marketing page
//   TARGET=home EMAIL=jim@local.test PASSWORD=demopassword12 node scripts/shoot.mjs
//   URL=http://localhost:5174 REDUCE=1 node scripts/shoot.mjs
//
// Prereqs: `npm install -D @playwright/test` and `npx playwright install chromium`.

import { chromium } from '@playwright/test'
import { mkdir } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)))
const OUT = join(ROOT, '.shots')
const URL = process.env.URL || 'http://localhost:5173'
const TARGET = process.env.TARGET || 'marketing' // 'marketing' | 'home' | 'sources'
const HASH = process.env.HASH || '' // e.g. '#/sources'
const EMAIL = process.env.EMAIL
const PASSWORD = process.env.PASSWORD
const THEMES = process.env.THEME ? [process.env.THEME] : ['light', 'dark']

const VIEWPORT = { width: 1440, height: 900 }
const DEFAULT_SEL = { home: '.wb-home', sources: '.wb-sources', marketing: '.wb-marketing' }
const ROOT_SEL = process.env.SELECTOR || DEFAULT_SEL[TARGET] || '.wb-marketing'
const dest = URL + HASH

const setTheme = (page, theme) =>
  page.evaluate((t) => { document.documentElement.dataset.theme = t }, theme)

async function login(page) {
  await page.goto(URL, { waitUntil: 'domcontentloaded' }) // establish origin for cookies
  const status = await page.evaluate(async ([email, password]) => {
    const r = await fetch('/api/auth/login', {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password }),
    })
    return r.status
  }, [EMAIL, PASSWORD])
  if (status >= 400) throw new Error(`login failed: HTTP ${status}`)
  console.log(`login: ${status}`)
}

async function run() {
  await mkdir(OUT, { recursive: true })
  const browser = await chromium.launch()
  const page = await browser.newPage({
    viewport: VIEWPORT,
    deviceScaleFactor: 2,
    reducedMotion: process.env.REDUCE ? 'reduce' : 'no-preference',
  })

  if (EMAIL && PASSWORD) {
    // Authenticated flow: land on the app shell first (loading directly on an
    // app-route hash flashes the marketing page while auth resolves), then
    // navigate in-app to the target route.
    await login(page)
    await page.goto(URL, { waitUntil: 'networkidle' })
    await page.waitForSelector('.app-content, .wb-home', { timeout: 15_000 })
    if (HASH) {
      await page.evaluate((h) => { window.location.hash = h }, HASH)
    }
  } else {
    await page.goto(dest, { waitUntil: 'networkidle' })
  }
  await page.waitForSelector(ROOT_SEL, { timeout: 15_000 })
  await page.waitForTimeout(1800) // let entrance animations + data load settle

  for (const theme of THEMES) {
    await setTheme(page, theme)
    await page.waitForTimeout(450)

    // Above-the-fold hero shot.
    await page.screenshot({ path: join(OUT, `${TARGET}-top-${theme}.png`) })

    // Full content: element screenshot captures the whole node, even when it's
    // taller than the viewport (works around internal scroll containers that
    // defeat Playwright's fullPage).
    await page.locator(ROOT_SEL).screenshot({ path: join(OUT, `${TARGET}-full-${theme}.png`) })

    // Zoom on the analytical band for home.
    if (TARGET === 'home') {
      const band = page.locator('.home-insight')
      if (await band.count()) {
        await band.scrollIntoViewIfNeeded()
        await page.waitForTimeout(400)
        await band.screenshot({ path: join(OUT, `home-insight-${theme}.png`) })
      }
    }
    console.log(`shot: ${theme}`)
  }

  await browser.close()
  console.log(`\nScreenshots written to ${OUT}`)
}

run().catch((err) => {
  console.error(err)
  process.exit(1)
})
