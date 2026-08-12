import type { AppSection } from '~/composables/useAuth'

export function appSectionTarget(section: AppSection) {
  if (section === 'overview') return { path: '/overview' }
  if (section === 'packages') return { path: '/wdl-packages' }
  if (section === 'runs') return { path: '/runs' }
  if (section === 'wdl') return { path: '/wdl' }
  return { path: '/', query: { section } }
}

export function useAppNavigation() {
  return {
    navigateSection(section: AppSection) {
      return navigateTo(appSectionTarget(section))
    },
  }
}
