import { useEffect, useRef } from 'react'
import { useInfiniteQuery, useMutation, useQueryClient } from '@tanstack/react-query'

import { agentApi, type ChatContext } from '@/api/agent'
import { isTerminal } from '@/api/runs'

const turnsKey = (sessionId: string) => ['session', sessionId, 'messages']
const PAGE_SIZE = 50

function useRefreshTurn(sessionId: string) {
  const queryClient = useQueryClient()
  return () => {
    void queryClient.invalidateQueries({ queryKey: turnsKey(sessionId) })
    void queryClient.invalidateQueries({ queryKey: ['session', sessionId] })
    void queryClient.invalidateQueries({ queryKey: ['session', sessionId, 'history'] })
  }
}

export function useTurns(sessionId: string) {
  const queryClient = useQueryClient()
  const query = useInfiniteQuery({
    queryKey: turnsKey(sessionId),
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) => agentApi.turns(sessionId, pageParam),
    // 每页返回正序的最近 50 条；上一页游标取当页最旧一条
    getNextPageParam: (lastPage) => (lastPage.length < PAGE_SIZE ? undefined : lastPage[0]?.id),
    select: (data) => {
      const seen = new Set<string>()
      const turns = []
      // pages 时间序为「最新页在前」，展示按时间正序拼接，id 去重防刷新竞态
      for (const page of [...data.pages].reverse()) {
        for (const turn of page) {
          if (seen.has(turn.id)) continue
          seen.add(turn.id)
          turns.push(turn)
        }
      }
      return turns
    },
    // 第一步 SSE 结束时下一步可能还没写进计划，整轮 running 时接着拉，避免卡片停在「等待中」。
    // 最新轮在 pages[0]（fetchNextPage 只会把更早的页追加到尾部），读错页会让运行中轮询悄悄停掉
    refetchInterval: (current) =>
      current.state.data?.pages?.[0]?.at(-1)?.status === 'running' ? 800 : false,
  })

  const latest = query.data?.at(-1)?.status
  const previous = useRef(latest)
  useEffect(() => {
    const wasRunning = previous.current === 'running'
    previous.current = latest
    if (!wasRunning || !latest || !isTerminal(latest)) return
    void queryClient.invalidateQueries({ queryKey: ['session', sessionId] })
    void queryClient.invalidateQueries({ queryKey: ['session', sessionId, 'history'] })
  }, [latest, sessionId, queryClient])

  useEffect(() => {
    previous.current = undefined
  }, [sessionId])

  return query
}

export function useSendMessage(sessionId: string) {
  const refresh = useRefreshTurn(sessionId)
  return useMutation({
    mutationFn: (input: { text: string; context?: ChatContext }) =>
      agentApi.send(sessionId, input.text, input.context),
    onSuccess: refresh,
  })
}

export function usePlanActions(sessionId: string) {
  const refresh = useRefreshTurn(sessionId)
  const confirm = useMutation({
    mutationFn: (turnId: string) => agentApi.confirm(sessionId, turnId),
    onSuccess: refresh,
  })
  const cancel = useMutation({
    mutationFn: (turnId: string) => agentApi.cancel(sessionId, turnId),
    onSuccess: refresh,
  })
  const retry = useMutation({
    mutationFn: (turnId: string) => agentApi.retry(sessionId, turnId),
    onSuccess: refresh,
  })
  return {
    confirm: (turnId: string) => confirm.mutate(turnId),
    cancel: (turnId: string) => cancel.mutate(turnId),
    retry: (turnId: string) => retry.mutate(turnId),
    busy: confirm.isPending || cancel.isPending || retry.isPending,
  }
}
