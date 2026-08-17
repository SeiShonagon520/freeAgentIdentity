import { useCallback, useEffect, useState } from 'react'
import { RefreshCw, Square } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { TaskLogPanel } from '@/components/tasks/TaskLogPanel'
import { apiFetch } from '@/lib/utils'

const TYPE_LABELS: Record<string, string> = {
  register: '协议注册',
  refresh_token_check: '401 验活',
}

export default function Tasks() {
  const [tasks, setTasks] = useState<any[]>([])
  const [loading, setLoading] = useState(true)
  const [stoppingId, setStoppingId] = useState<string | null>(null)
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    try {
      const data = await apiFetch('/tasks?active_only=true&limit=100')
      setTasks(Array.isArray(data?.items) ? data.items : [])
      setError('')
    } catch (err: any) {
      setError(err?.message || '读取运行任务失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
    const timer = window.setInterval(() => void load(), 1000)
    return () => window.clearInterval(timer)
  }, [load])

  const stop = async (taskId: string) => {
    setStoppingId(taskId)
    try {
      await apiFetch(`/tasks/${taskId}/cancel`, { method: 'POST' })
      await load()
    } catch (err: any) {
      setError(err?.message || '停止任务失败')
    } finally {
      setStoppingId(null)
    }
  }

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold text-[var(--text-primary)]">运行任务</h1>
          <p className="mt-1 text-sm text-[var(--text-muted)]">当前运行：{tasks.length} 个。每个任务会实时同步后端日志，页面刷新后自动恢复最新日志。</p>
        </div>
        <Button variant="outline" size="sm" onClick={() => void load()} disabled={loading}>
          <RefreshCw className={`mr-2 h-4 w-4 ${loading ? 'animate-spin' : ''}`} />刷新
        </Button>
      </div>

      {error ? <div className="rounded-xl border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-300">{error}</div> : null}

      <div className="overflow-hidden rounded-xl border border-[var(--border)] bg-[var(--bg-card)]">
        {!loading && tasks.length === 0 ? (
          <div className="px-5 py-12 text-center text-sm text-[var(--text-muted)]">当前没有运行任务</div>
        ) : (
          <div className="divide-y divide-[var(--border)]">
            {tasks.map(task => (
              <div key={task.task_id} className="space-y-3 px-4 py-4">
                <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-medium text-[var(--text-primary)]">{TYPE_LABELS[task.type] || task.type}</span>
                      <span className="rounded-full bg-sky-500/10 px-2 py-0.5 text-xs text-sky-300 ring-1 ring-inset ring-sky-500/30">{task.status}</span>
                    </div>
                    <div className="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-sm text-[var(--text-secondary)]">
                      <span>进度 <b className="text-[var(--text-primary)]">{task.progress}</b></span>
                      <span className="font-mono text-xs text-[var(--text-muted)]" title={task.task_id}>{task.task_id}</span>
                    </div>
                  </div>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => void stop(task.task_id)}
                    disabled={!task.cancellable || stoppingId === task.task_id}
                    className="shrink-0 border-red-500/35 text-red-300 hover:bg-red-500/10 hover:text-red-200"
                  >
                    <Square className="mr-2 h-3.5 w-3.5" />
                    {stoppingId === task.task_id ? '停止中…' : '停止任务'}
                  </Button>
                </div>
                <TaskLogPanel taskId={task.task_id} compact onDone={() => void load()} />
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
