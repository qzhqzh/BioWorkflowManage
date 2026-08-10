export type AppSection = 'edit' | 'tools' | 'packages' | 'artifacts' | 'runs' | 'wdl' | 'help'

export type AuthUser = {
  username: string
  first_name?: string
  last_name?: string
  is_admin: boolean
  role: 'admin' | 'analysis_operator' | 'restricted'
  allowed_sections: AppSection[]
}

export function defaultRouteForUser(user: AuthUser): string {
  return user.allowed_sections.includes('wdl') ? '/wdl' : '/runs'
}

export function routeSection(path: string, section?: unknown): AppSection {
  if (path.startsWith('/runs')) return 'runs'
  if (path.startsWith('/wdl-packages')) return 'packages'
  if (path.startsWith('/wdl')) return 'wdl'
  if (typeof section === 'string' && ['edit', 'tools', 'artifacts', 'help'].includes(section)) {
    return section as AppSection
  }
  return 'edit'
}

export function useAuth() {
  const user = useState<AuthUser | null>('auth-user', () => null)
  const ready = useState<boolean>('auth-ready', () => false)

  return {
    user,
    ready,
    clear() {
      user.value = null
      ready.value = true
    },
  }
}
