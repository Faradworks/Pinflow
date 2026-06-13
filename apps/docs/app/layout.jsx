import { Footer, Layout, Navbar } from 'nextra-theme-docs'
import { Head } from 'nextra/components'
import { getPageMap } from 'nextra/page-map'
import 'nextra-theme-docs/style.css'

export const metadata = {
  title: {
    default: 'Pinflow Docs',
    template: '%s — Pinflow Docs'
  },
  description:
    'Documentation for Pinflow — an open-source agentic assistant for electronics design in KiCad.'
}

const navbar = (
  <Navbar
    logo={<b>Pinflow</b>}
    projectLink="https://github.com/Faradworks/Pinflow"
  />
)

const footer = (
  <Footer>GPL-3.0-or-later © {new Date().getFullYear()} Faradworks.</Footer>
)

export default async function RootLayout({ children }) {
  return (
    <html lang="en" dir="ltr" suppressHydrationWarning>
      <Head />
      <body>
        <Layout
          navbar={navbar}
          footer={footer}
          pageMap={await getPageMap()}
          docsRepositoryBase="https://github.com/Faradworks/Pinflow/tree/main/apps/docs"
          sidebar={{ defaultMenuCollapseLevel: 1 }}
        >
          {children}
        </Layout>
      </body>
    </html>
  )
}
