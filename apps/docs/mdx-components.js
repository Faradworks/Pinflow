import { useMDXComponents as getThemeComponents } from 'nextra-theme-docs'

// Merge Nextra's docs-theme MDX components with any page-level overrides.
const themeComponents = getThemeComponents()

export function useMDXComponents(components) {
  return {
    ...themeComponents,
    ...components
  }
}
