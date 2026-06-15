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

The site is a standalone Next.js app deployed as its own Vercel project
(`pinflow-docs`, in the `faradworks-projects` team), decoupled from the desktop
app and the API. The project is linked from **this directory** (`.vercel/` here,
gitignored), so deploys only ever upload `apps/docs` — the rest of the monorepo
is invisible to it.

Deploy a new production build from `apps/docs`:

```bash
vercel deploy --prod        # builds remotely; Pagefind search indexed at build
```

**Custom domain.** `docs.pinflow.faradworks.com` is attached to the project.
The `faradworks.com` zone is on Google Cloud DNS and its Vercel subdomains use
an **A record to `76.76.21.21`** (same as `pinflow.faradworks.com`), so add:

```
A   docs.pinflow.faradworks.com   76.76.21.21
```

(A `CNAME` to `cname.vercel-dns.com` also works; the A record matches the
existing zone pattern.) Vercel auto-verifies and issues SSL once it resolves.

**Git auto-deploy (optional, later).** Currently deploys are CLI-driven. To get
push-to-deploy + PR previews, connect the project to the `Faradworks/Pinflow`
GitHub repo and set **Root Directory = `apps/docs`** (do this after `apps/docs`
lands on `main`).
