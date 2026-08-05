export type AuthUser = {
  username: string
  first_name?: string
  last_name?: string
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
