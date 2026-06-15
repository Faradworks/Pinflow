import nextra from 'nextra'

const withNextra = nextra({
  // Search is on by default (Pagefind, built at `next build`).
  defaultShowCopyCode: true
})

export default withNextra({
  reactStrictMode: true
})
