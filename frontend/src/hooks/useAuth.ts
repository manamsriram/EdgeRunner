import { useQuery } from '@tanstack/react-query'
import { getMe } from '../lib/api'

export function useAuth() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ['me'],
    queryFn: () => getMe().then((r) => r.data),
    retry: false,
  })

  return {
    user: data ?? null,
    isLoading,
    isAuthenticated: !isError && !!data,
  }
}
