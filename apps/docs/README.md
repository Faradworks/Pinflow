# Pinflow docs site

The public documentation site for Pinflow, built with
[Nextra](https://nextra.site/) (Next.js App Router). Deployed to
`docs.pinflow.faradworks.com`.

## Develop

```bash
cd apps/docs
npm install
npm run dev      # http://localhost:3000
```

Build / preview the production output locally:

```bash
npm run build
npm run start
```

## Where the content lives

All pages are MDX under `content/`. The sidebar/nav is defined by `_meta.js`
files (order + titles); a folder with `type: 'page'` becomes a top-nav tab.

```
content/
├── _meta.js            # top-level nav (Home, For Users, For Developers)
├── index.mdx           # landing page
├── users/              # user-facing track
│   ├── _meta.js
│   └── *.mdx
└── developers/         # contributor track
    ├── _meta.js
    └── *.mdx
```

To add a page: drop a `.mdx` file in the right folder and add its slug to that
folder's `_meta.js`. MDX can use Nextra's built-in components (`Callout`,
`Steps`, `Cards`, `FileTree`, …) via `import { X } from 'nextra/components'`.

**Docs version with the code.** This site lives in the public code repo on
purpose: a PR that changes a feature should update its doc page in the same PR.

## Editorial guardrails

These docs are public. Keep internal strategy, billing economics, private
infrastructure identifiers, and internal repo/service names out of them. When
in doubt, document only what a user or external contributor needs.

## ⚠️ The zod pin (don't remove)

`package.json` has:

```json
"overrides": { "zod": "~4.1.12" }
```

This is load-bearing. nextra 4.6.1 declares `zod@^4.1.12` as a peer but was
built against the **4.1 line**. zod **4.4.x** tightened `strictObject`
validation so a required field receiving `undefined` is rejected *before* a
`z.custom()` check runs. Nextra's theme `<Layout>` strips `children` out before
validating its props against a schema that lists `children` as a required
`reactNode`, so on zod ≥ 4.4 **every page 500s** with
`Invalid input: expected nonoptional, received undefined → at children`.
Pinning zod to the 4.1 line restores the behavior nextra expects. Revisit only
after confirming a newer nextra/zod combination renders.

## Deploy (Vercel)

The site is a standalone Next.js app; deploy it as its own Vercel project so it
stays decoupled from the desktop app and the API.

1. **New Vercel project** from the `Faradworks/Pinflow` repo.
2. **Root Directory:** `apps/docs` (Project Settings → Build & Development).
   Framework preset auto-detects as **Next.js**; no build-command overrides
   needed (`npm run build`).
3. **Domain:** add `docs.pinflow.faradworks.com` and point a `CNAME` at Vercel
   (`cname.vercel-dns.com`).
4. Pushes to `main` deploy production; every PR gets a preview URL — which
   doubles as a doc-review preview.

Search uses Nextra's built-in [Pagefind](https://pagefind.app/), indexed at
build time — no external service to configure.
