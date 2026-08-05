import js from '@eslint/js'
import globals from 'globals'
import react from 'eslint-plugin-react'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'

// Added to a frontend that had never run a linter, so this starts at the
// recommended rules plus the React-hooks checks (the ones that catch real bugs:
// stale closures, conditional hooks). CI runs it non-blocking for now.
export default [
  { ignores: ['dist/**', 'node_modules/**'] },
  js.configs.recommended,
  {
    files: ['**/*.{js,jsx}'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      globals: { ...globals.browser, ...globals.es2021 },
      parserOptions: { ecmaFeatures: { jsx: true } },
    },
    settings: { react: { version: 'detect' } },
    plugins: {
      react,
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
    },
    rules: {
      // The two classic, high-signal hooks rules only. v7's `recommended` also
      // enables the React Compiler ruleset (set-state-in-effect, immutability,
      // static-components), which flags idiomatic code here — `immutability`
      // objects to `window.location.hash = ...`, which is this app's router.
      // Those are a deliberate migration, not a lint baseline.
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'warn',
      // Without these two, no-unused-vars can't see JSX usage and reports every
      // imported component as dead.
      'react/jsx-uses-react': 'error',
      'react/jsx-uses-vars': 'error',
      'react-refresh/only-export-components': ['warn', { allowConstantExport: true }],
      // `catch { /* ignore */ }` is used deliberately around localStorage and
      // JSON.parse of untrusted SSE frames; an unused binding there isn't a defect.
      'no-unused-vars': ['error', {
        argsIgnorePattern: '^_',
        varsIgnorePattern: '^(_|React$)',
        caughtErrors: 'none',
      }],
    },
  },
  {
    // Build config runs under Node, not the browser.
    files: ['vite.config.js'],
    languageOptions: { globals: { ...globals.node } },
  },
  {
    // The Playwright screenshot script is Node, but its page.evaluate()
    // callbacks are serialized into the browser — so it legitimately references
    // both sets of globals.
    files: ['scripts/**/*.{js,mjs}'],
    languageOptions: { globals: { ...globals.node, ...globals.browser } },
  },
]
