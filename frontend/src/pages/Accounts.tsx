import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { ChevronLeft, ChevronRight, Copy, KeyRound, Plus, RefreshCw, ShieldCheck, X } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { apiFetch } from '@/lib/utils'

type AccountListItem = {
  id: number
  email: string
  password: string
  totp_secret: string
  refresh_token_status: string
  has_refresh_token: boolean
  created_at: string | null
}

type SurvivalStats = {
  platform: string
  alive_accounts: number
  historical_registered_emails: number
  survival_rate: number
}

type CreatedTask = {
  id: string
  title: string
}

type ProxyNode = {
  name: string
  type: string
  alive: boolean | null
  delay: number | null
  last_test: string
  udp: boolean
  selected: boolean
}

type ProxyNodesResponse = {
  available: boolean
  group: string
  selected: string
  nodes: ProxyNode[]
  error: string
}

function formatDate(value: string | null) {
  if (!value) return '-'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

function statePill(value: string) {
  const state = String(value || 'unknown').toLowerCase()
  const isYes = state === 'invalid'
  const isNo = state === 'valid'
  const label = isYes
    ? '401'
    : isNo
      ? '正常'
      : state === 'checking'
        ? '校验中'
        : state === 'not_checked'
          ? '未校验'
          : state === 'missing'
            ? '待复验'
            : '未确认'
  const styles = isYes
    ? 'border-red-500/30 bg-red-500/10 text-red-400'
    : isNo
      ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-400'
      : 'border-[var(--border)] bg-[var(--bg-pane)] text-[var(--text-muted)]'
  return <span className={`inline-flex min-w-8 justify-center rounded-full border px-2 py-0.5 text-xs ${styles}`}>{label}</span>
}

function RegisterDialog({ onClose, onCreated }: { onClose: () => void, onCreated: (task: CreatedTask) => void }) {
  const [count, setCount] = useState('1')
  const [concurrency, setConcurrency] = useState('1')
  const [proxyNodes, setProxyNodes] = useState<ProxyNode[]>([])
  const [proxyLoading, setProxyLoading] = useState(false)
  const [proxyError, setProxyError] = useState('')
  const [proxyMode, setProxyMode] = useState<'pool' | 'dynamic' | 'direct'>('pool')
  const [proxyApiUrl, setProxyApiUrl] = useState('')
  const [mailProvider, setMailProvider] = useState('')
  const [mailProviders, setMailProviders] = useState<Array<any>>([])
  const [splitRegister, setSplitRegister] = useState(true)
  const [harAvailable, setHarAvailable] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const [pulse, setPulse] = useState(true)
  const [probeInterval, setProbeInterval] = useState('600')
  const [probeOtpTimeout, setProbeOtpTimeout] = useState('90')
  const [banAfter, setBanAfter] = useState('3')
  const [probeBatch, setProbeBatch] = useState('5')
  const [harCapture, setHarCapture] = useState(false)
  const [harCapture2fa, setHarCapture2fa] = useState(false)
  const [executorType, setExecutorType] = useState<'protocol' | 'headed' | 'headless'>('protocol')

  const loadProxyNodes = useCallback(async (refresh = false) => {
    setProxyLoading(true)
    try {
      const data = await apiFetch(`/proxy-nodes${refresh ? '?refresh=true' : ''}`) as ProxyNodesResponse
      const nodes = Array.isArray(data?.nodes) ? data.nodes : []
      setProxyNodes(nodes)
      setProxyError(data?.available ? '' : (data?.error || 'Mihomo 代理服务不可用'))
    } catch (err: any) {
      setProxyNodes([])
      setProxyError(err?.message || '代理节点读取失败')
    } finally {
      setProxyLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadProxyNodes()
    void Promise.all([
      apiFetch('/provider-settings?provider_type=mailbox').catch(() => []),
      apiFetch('/system/runtime').catch(() => ({ har_capture_available: false })),
    ]).then(([settings, runtime]) => {
      const enabled = Array.isArray(settings) ? settings.filter(item => item?.enabled !== false) : []
      setMailProviders(enabled)
      setMailProvider('')
      setHarAvailable(Boolean(runtime?.har_capture_available))
    })
  }, [loadProxyNodes])

  useEffect(() => {
    if (!harAvailable) setHarCapture(false)
  }, [harAvailable])

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    const numericCount = Number(count)
    const numericConcurrency = Number(concurrency)
    if (!Number.isInteger(numericCount) || numericCount < 0) {
      setError('注册数量必须是大于等于 0 的整数；0 代表无限注册。')
      return
    }
    if (!Number.isInteger(numericConcurrency) || numericConcurrency < 1 || numericConcurrency > 50) {
      setError('并发必须在 1 到 50 之间。')
      return
    }
    if (!mailProvider) {
      setError('请选择注册邮箱服务。')
      return
    }
    const numericProbeInterval = Number(probeInterval)
    const numericProbeOtpTimeout = Number(probeOtpTimeout)
    const numericBanAfter = Number(banAfter)
    const numericProbeBatch = Number(probeBatch)
    if (
      !Number.isInteger(numericProbeInterval) || numericProbeInterval < 30 ||
      !Number.isInteger(numericProbeOtpTimeout) || numericProbeOtpTimeout < 20 ||
      !Number.isInteger(numericBanAfter) || numericBanAfter < 1 ||
      !Number.isInteger(numericProbeBatch) || numericProbeBatch < 1 || numericProbeBatch > 20
    ) {
      setError('脉冲探测参数无效：探测间隔≥30s，验证码超时≥20s，封禁阈值≥1，探测批量 1-20。')
      return
    }
    const useDynamic = proxyMode === 'dynamic' && proxyApiUrl.trim().length > 0
    if (proxyMode === 'dynamic' && !proxyApiUrl.trim()) {
      setError('动态 IP 模式需要填写代理提取 API')
      return
    }
    setSubmitting(true)
    setError('')
    try {
      const task = await apiFetch('/tasks/register', {
        method: 'POST',
        body: JSON.stringify({
          count: harCapture ? 1 : numericCount,
          concurrency: numericConcurrency,
          proxy: null,
          proxy_node: null,
          proxy_pool: harCapture ? false : proxyMode === 'pool',
          proxy_api_url: useDynamic ? proxyApiUrl.trim() : null,
          executor_type: harCapture ? 'protocol' : executorType,
          pulse: harCapture ? false : (proxyMode === 'pool' ? pulse : false),
          pulse_interval_seconds: 0,
          probe_interval_seconds: numericProbeInterval,
          probe_otp_timeout_seconds: numericProbeOtpTimeout,
          ban_after_consecutive_no_email: numericBanAfter,
          probe_batch_size: numericProbeBatch,
          har_capture: harCapture,
          har_capture_2fa: harCapture && harCapture2fa,
          split_register: splitRegister,
          extra: {
            mail_provider: mailProvider,
            bind_totp_2fa: true,
          },
        }),
      })
      onCreated({
        id: task.task_id,
        title: harCapture
          ? harCapture2fa
            ? 'camoufox 抓包任务已创建（注册 + 绑定 2FA，请在浏览器手动操作）'
            : 'camoufox 抓包任务已创建（请在浏览器手动注册）'
          : proxyMode === 'dynamic' ? `${executorType === 'protocol' ? '协议' : executorType === 'headed' ? '有头浏览器' : '无头浏览器'}注册任务已创建（动态 IP）`
            : proxyMode === 'pool' ? `${executorType === 'protocol' ? '协议' : executorType === 'headed' ? '有头浏览器' : '无头浏览器'}注册任务已创建（Mihomo 代理池）`
              : `${executorType === 'protocol' ? '协议' : executorType === 'headed' ? '有头浏览器' : '无头浏览器'}注册任务已创建（直连）`,
      })
    } catch (err: any) {
      setError(err?.message || '创建注册任务失败')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
      <form onSubmit={submit} className="max-h-[calc(100vh-2rem)] w-full max-w-lg overflow-y-auto rounded-xl border border-[var(--border)] bg-[var(--bg-card)] p-5 shadow-xl">
        <div className="flex items-center justify-between gap-4">
          <div>
            <h2 className="text-base font-semibold text-[var(--text-primary)]">创建注册任务</h2>
            <p className="mt-1 text-xs text-[var(--text-muted)]">选择注册方式、邮箱来源和代理后创建注册任务。浏览器模式走 camoufox，密码 + 邮箱验证码 + 姓名生日全自动。</p>
          </div>
          <button type="button" onClick={onClose} className="rounded p-1 text-[var(--text-muted)] hover:bg-[var(--bg-hover)]"><X className="h-4 w-4" /></button>
        </div>
        <div className="mt-5 grid gap-4 sm:grid-cols-2">
          <label className="grid gap-1.5 text-sm text-[var(--text-secondary)]">
            注册方式
            <select value={executorType} onChange={event => setExecutorType(event.target.value as 'protocol' | 'headed' | 'headless')} disabled={harCapture} className="rounded-md border border-[var(--border)] bg-[var(--bg-card)] px-3 py-2 text-sm text-[var(--text-primary)]">
              <option value="protocol">协议注册（curl 模拟）</option>
              <option value="headed">浏览器注册（有头 camoufox）</option>
              <option value="headless">浏览器注册（无头 camoufox）</option>
            </select>
            <span className="text-xs text-[var(--text-muted)]">有头模式可在服务器 VNC（:6080）实时观察浏览器操作。</span>
          </label>
          <label className="grid gap-1.5 text-sm text-[var(--text-secondary)]">
            注册数量
            <input value={count} onChange={event => setCount(event.target.value)} inputMode="numeric" className="rounded-md border border-[var(--border)] bg-transparent px-3 py-2 text-[var(--text-primary)]" />
            <span className="text-xs text-[var(--text-muted)]">0 = 无限，直到在任务页停止。</span>
          </label>
          <label className="grid gap-1.5 text-sm text-[var(--text-secondary)]">
            并发（最高 50）
            <input value={concurrency} onChange={event => setConcurrency(event.target.value)} inputMode="numeric" className="rounded-md border border-[var(--border)] bg-transparent px-3 py-2 text-[var(--text-primary)]" />
          </label>
          <label className="grid gap-1.5 text-sm text-[var(--text-secondary)] sm:col-span-2">
            注册邮箱服务
            <select required value={mailProvider} onChange={event => setMailProvider(event.target.value)} className="rounded-md border border-[var(--border)] bg-[var(--bg-card)] px-3 py-2 text-sm text-[var(--text-primary)]">
              <option value="" disabled>请选择邮箱服务</option>
              {mailProviders.map(provider => <option key={provider.provider_key} value={provider.provider_key}>{provider.provider_key === 'local_ms_pool' ? '微软邮箱池（每个邮箱注册 6 次）' : provider.display_name || provider.provider_key}</option>)}
              {!mailProviders.some(provider => provider.provider_key === 'local_ms_pool') && <option value="local_ms_pool" disabled>微软邮箱池（请先在设置中配置）</option>}
            </select>
            {mailProvider === 'local_ms_pool' && (
              <div className="flex flex-col gap-2">
                <span className="text-xs text-[var(--text-muted)]">每个微软邮箱最多可注册 {splitRegister ? 6 : 1} 次。</span>
                <label className="flex items-center gap-2 text-xs text-[var(--text-secondary)]">
                  <input type="checkbox" checked={splitRegister} onChange={event => setSplitRegister(event.target.checked)} className="accent-sky-500" />
                  分裂注册（每个父邮箱拆分 6 个子地址 +reg1~+reg6 注册）
                </label>
              </div>
            )}
          </label>
          <div className="rounded-md border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-400 sm:col-span-2">
            自动注册固定要求远端密码，并在成功后绑定和激活 TOTP 2FA；任一步失败都不会保存账号。
          </div>
          {harAvailable && <div className="grid gap-2 rounded-md border border-[var(--border)] bg-[var(--bg-pane)]/40 p-3 sm:col-span-2">
            <label className="flex items-center gap-2 text-sm text-[var(--text-secondary)]">
              <input type="checkbox" checked={harCapture} onChange={event => setHarCapture(event.target.checked)} className="accent-sky-500" />
              camoufox 抓包模式（手动注册，抓取 HAR）
            </label>
            {harCapture ? (
              <>
                <label className="flex items-center gap-2 text-xs text-[var(--text-muted)]">
                  <input type="checkbox" checked={harCapture2fa} onChange={event => setHarCapture2fa(event.target.checked)} className="accent-sky-500" />
                  注册完成后继续绑定 2FA（两步验证）
                </label>
                <span className="text-xs text-[var(--text-muted)]">
                  打开 camoufox 真实浏览器到 ChatGPT 注册页并录制 HAR；请在浏览器里手动完成注册，勾选 2FA 时注册完成后不自动跳转，你可继续打开 OpenAI 安全设置页手动绑定两步验证。完成后关闭浏览器窗口，HAR 自动保存。启用后自动关闭脉冲/动态 IP。
                </span>
              </>
            ) : null}
          </div>}
          <label className="grid gap-1.5 text-sm text-[var(--text-secondary)] sm:col-span-2">
            注册代理
            <select value={proxyMode} onChange={event => setProxyMode(event.target.value as 'pool' | 'dynamic' | 'direct')} className="rounded-md border border-[var(--border)] bg-[var(--bg-card)] px-3 py-2 text-sm text-[var(--text-primary)]">
              <option value="pool">Mihomo 代理池（自动选择节点）</option>
              <option value="dynamic">动态 IP（轮换住宅代理）</option>
              <option value="direct">无（本机直连）</option>
            </select>
          </label>
          {proxyMode === 'dynamic' ? (
            <div className="grid gap-2 rounded-md border border-[var(--border)] bg-[var(--bg-pane)]/40 p-3 sm:col-span-2">
              <label className="grid gap-1 text-xs text-[var(--text-muted)]">
                代理提取 API
                <input value={proxyApiUrl} onChange={event => setProxyApiUrl(event.target.value)} placeholder="http://us.rrp.bestgo.work:8089/gen?zone=...&sessType=rotating..." className="rounded-md border border-[var(--border)] bg-transparent px-3 py-2 text-sm text-[var(--text-primary)]" />
              </label>
              <span className="text-xs text-[var(--text-muted)]">
                每个 worker 从该 API 获取一个新的轮换住宅 IP；遇到 Cloudflare 挑战自动更换 IP。启用后自动关闭脉冲注册。
              </span>
            </div>
          ) : null}
          {proxyMode === 'pool' && <div className="grid gap-2 sm:col-span-2">
            <div className="flex items-center justify-between gap-3">
              <span className="text-sm text-[var(--text-secondary)]">Mihomo 代理池状态</span>
              <button
                type="button"
                onClick={() => void loadProxyNodes(true)}
                disabled={proxyLoading}
                className="inline-flex items-center text-xs text-[var(--text-muted)] hover:text-[var(--text-primary)] disabled:opacity-50"
              >
                <RefreshCw className={`mr-1 h-3.5 w-3.5 ${proxyLoading ? 'animate-spin' : ''}`} />
                测速刷新
              </button>
            </div>
            <div className="rounded-md border border-[var(--border)] bg-[var(--bg-pane)]/50 px-3 py-2 text-xs text-[var(--text-muted)]">
              {proxyLoading ? '正在读取节点…' : proxyNodes.length > 0 ? `已发现 ${proxyNodes.length} 个节点，注册时自动按负载分配。` : '未发现可用节点，请先在设置中配置 Mihomo 订阅。'}
            </div>
            {proxyError ? <div className="rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-300">{proxyError}</div> : null}
          </div>}
          {proxyMode === 'pool' ? (
            <div className="grid gap-3 rounded-md border border-[var(--border)] bg-[var(--bg-pane)]/40 p-3 sm:col-span-2">
              <label className="flex items-center gap-2 text-sm text-[var(--text-secondary)]">
                <input type="checkbox" checked={pulse} onChange={event => setPulse(event.target.checked)} className="accent-sky-500" />
                脉冲注册（每波所有健康节点并发；节点 IP 被封自动暂停并定时探测恢复）
              </label>
              {pulse ? (
                <div className="grid gap-3 sm:grid-cols-4">
                  <label className="grid gap-1 text-xs text-[var(--text-muted)]">
                    探测间隔（秒）
                    <input value={probeInterval} onChange={event => setProbeInterval(event.target.value)} inputMode="numeric" className="rounded-md border border-[var(--border)] bg-transparent px-2 py-1.5 text-sm text-[var(--text-primary)]" />
                  </label>
                  <label className="grid gap-1 text-xs text-[var(--text-muted)]">
                    探测验证码超时（秒）
                    <input value={probeOtpTimeout} onChange={event => setProbeOtpTimeout(event.target.value)} inputMode="numeric" className="rounded-md border border-[var(--border)] bg-transparent px-2 py-1.5 text-sm text-[var(--text-primary)]" />
                  </label>
                  <label className="grid gap-1 text-xs text-[var(--text-muted)]">
                    连续未收码封禁阈值
                    <input value={banAfter} onChange={event => setBanAfter(event.target.value)} inputMode="numeric" className="rounded-md border border-[var(--border)] bg-transparent px-2 py-1.5 text-sm text-[var(--text-primary)]" />
                  </label>
                  <label className="grid gap-1 text-xs text-[var(--text-muted)]">
                    每轮探测批量
                    <input value={probeBatch} onChange={event => setProbeBatch(event.target.value)} inputMode="numeric" className="rounded-md border border-[var(--border)] bg-transparent px-2 py-1.5 text-sm text-[var(--text-primary)]" />
                  </label>
                </div>
              ) : (
                <span className="text-xs text-[var(--text-muted)]">关闭后按现有并发滚动注册，不做节点封禁 / 探测。</span>
              )}
            </div>
          ) : null}
        </div>
        {error ? <div className="mt-4 rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">{error}</div> : null}
        <div className="mt-5 flex justify-end gap-2">
          <Button type="button" variant="outline" onClick={onClose}>取消</Button>
          <Button type="submit" disabled={submitting}>{submitting ? '创建中…' : '开始注册'}</Button>
        </div>
      </form>
    </div>
  )
}

export default function Accounts() {
  const navigate = useNavigate()
  const [accounts, setAccounts] = useState<AccountListItem[]>([])
  const [total, setTotal] = useState(0)
  const [survivalStats, setSurvivalStats] = useState<SurvivalStats | null>(null)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [search, setSearch] = useState('')
  const [debouncedSearch, setDebouncedSearch] = useState('')
  const [hasRefreshTokenOnly, setHasRefreshTokenOnly] = useState(false)
  const [loading, setLoading] = useState(false)
  const [showRegister, setShowRegister] = useState(false)
  const [runningAction, setRunningAction] = useState('')
  const [maintenanceConcurrency, setMaintenanceConcurrency] = useState('100')
  const [maintenanceProxyNode, setMaintenanceProxyNode] = useState('')
  const [maintenanceProxyNodes, setMaintenanceProxyNodes] = useState<ProxyNode[]>([])
  const [createdTask, setCreatedTask] = useState<CreatedTask | null>(null)
  const [error, setError] = useState('')

  const totalPages = Math.max(1, Math.ceil(total / pageSize))

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const params = new URLSearchParams({
        platform: 'chatgpt',
        page: String(page),
        page_size: String(pageSize),
      })
      if (debouncedSearch.trim()) params.set('email', debouncedSearch.trim())
      if (hasRefreshTokenOnly) params.set('has_refresh_token', 'true')
      const [data, stats] = await Promise.all([
        apiFetch(`/accounts?${params}`),
        apiFetch('/accounts/survival-stats?platform=chatgpt'),
      ])
      setAccounts(Array.isArray(data?.items) ? data.items : [])
      setTotal(Number(data?.total || 0))
      setSurvivalStats({
        platform: String(stats?.platform || 'chatgpt'),
        alive_accounts: Number(stats?.alive_accounts || 0),
        historical_registered_emails: Number(stats?.historical_registered_emails || 0),
        survival_rate: Number(stats?.survival_rate || 0),
      })
      setError('')
    } catch (err: any) {
      setError(err?.message || '读取账号列表失败')
    } finally {
      setLoading(false)
    }
  }, [debouncedSearch, hasRefreshTokenOnly, page, pageSize])

  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedSearch(search), 350)
    return () => window.clearTimeout(timer)
  }, [search])

  useEffect(() => { setPage(1) }, [debouncedSearch, hasRefreshTokenOnly, pageSize])
  useEffect(() => { void load() }, [load])
  useEffect(() => {
    void apiFetch('/proxy-nodes')
      .then((data: ProxyNodesResponse) => {
        setMaintenanceProxyNodes(Array.isArray(data?.nodes) ? data.nodes : [])
      })
      .catch(() => setMaintenanceProxyNodes([]))
  }, [])

  const createRefreshCheckTask = async (browser = false) => {
    setRunningAction(browser ? 'refresh_browser' : 'refresh')
    try {
      const task = await apiFetch('/accounts/check-refresh-tokens', {
        method: 'POST',
        body: JSON.stringify({
          platform: 'chatgpt',
          concurrency: Number(maintenanceConcurrency),
          proxy_node: maintenanceProxyNode || null,
          browser,
        }),
      })
      setCreatedTask({
        id: task.task_id,
        title: browser
          ? `浏览器验活任务已创建（${maintenanceConcurrency} 并发，camoufox）`
          : `401 验活任务已创建（${maintenanceConcurrency} 并发）`,
      })
      await load()
    } catch (err: any) {
      setError(err?.message || '创建任务失败')
    } finally {
      setRunningAction('')
    }
  }

  const [copiedId, setCopiedId] = useState<number | null>(null)

  const copyAccount = async (account: AccountListItem) => {
    const lines = [account.email, account.password || '']
    if (account.totp_secret) {
      lines.push(`https://2fa.live/tok/${account.totp_secret}`)
    }
    const text = lines.join('\n')
    let ok = false
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text)
        ok = true
      }
    } catch {
      ok = false
    }
    if (!ok) {
      // Fallback for plain-HTTP deployments where the Clipboard API is blocked:
      // select a hidden textarea and use the legacy execCommand('copy').
      try {
        const ta = document.createElement('textarea')
        ta.value = text
        ta.style.position = 'fixed'
        ta.style.opacity = '0'
        document.body.appendChild(ta)
        ta.select()
        ta.setSelectionRange(0, text.length)
        ok = document.execCommand('copy')
        document.body.removeChild(ta)
      } catch {
        ok = false
      }
    }
    if (ok) {
      setCopiedId(account.id)
      setTimeout(() => setCopiedId(null), 1500)
    } else {
      setError('复制失败')
    }
  }

  return (
    <div className="space-y-4">
      {showRegister ? <RegisterDialog onClose={() => setShowRegister(false)} onCreated={(task) => { setShowRegister(false); setCreatedTask(task) }} /> : null}
      <Card className="border border-[var(--border)] bg-[var(--bg-pane)]/40 p-5">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
          <div>
            <h1 className="text-xl font-semibold text-[var(--text-primary)]">ChatGPT 账号</h1>
            <p className="mt-1 text-sm text-[var(--text-muted)]">共 {total} 个账号；列表按服务端分页加载。</p>
          </div>
          <div className="flex flex-wrap gap-2">
            <select
              value={maintenanceProxyNode}
              onChange={event => setMaintenanceProxyNode(event.target.value)}
              disabled={Boolean(runningAction)}
              aria-label="注册或登录代理节点"
              className="max-w-56 rounded-md border border-[var(--border)] bg-transparent px-2 py-1.5 text-sm text-[var(--text-primary)] disabled:opacity-50"
            >
              <option value="">登录代理：自动</option>
              {maintenanceProxyNodes.map(node => (
                <option key={node.name} value={node.name} disabled={node.alive === false}>
                  {node.name} · {node.delay ? `${node.delay}ms` : '未测速'}
                </option>
              ))}
            </select>
            <select
              value={maintenanceConcurrency}
              onChange={event => setMaintenanceConcurrency(event.target.value)}
              disabled={Boolean(runningAction)}
              aria-label="任务并发数"
              className="rounded-md border border-[var(--border)] bg-transparent px-2 py-1.5 text-sm text-[var(--text-primary)] disabled:opacity-50"
            >
              <option value="50">并发 50</option>
              <option value="100">并发 100</option>
              <option value="150">并发 150</option>
              <option value="200">并发 200</option>
            </select>
            <Button size="sm" variant="outline" disabled={Boolean(runningAction)} onClick={() => void createRefreshCheckTask()}>
              <ShieldCheck className="mr-1.5 h-4 w-4" />
              {runningAction === 'refresh' ? '创建中…' : '401 验活'}
            </Button>
            <Button size="sm" variant="outline" disabled={Boolean(runningAction)} onClick={() => void createRefreshCheckTask(true)} title="用 camoufox 真实浏览器执行验活，规避 Cloudflare 403">
              <ShieldCheck className="mr-1.5 h-4 w-4" />
              {runningAction === 'refresh_browser' ? '创建中…' : '浏览器验活'}
            </Button>
            <Button size="sm" onClick={() => setShowRegister(true)}>
              <Plus className="mr-1.5 h-4 w-4" />协议注册
            </Button>
          </div>
        </div>
        <div className="mt-5 grid grid-cols-1 border-t border-[var(--border)] pt-4 sm:grid-cols-3">
          <div className="py-2 sm:pr-5">
            <div className="text-xs text-[var(--text-muted)]">存活账号</div>
            <div className="mt-1 text-2xl font-semibold text-emerald-400">{survivalStats?.alive_accounts ?? '-'}</div>
          </div>
          <div className="border-t border-[var(--border)] py-2 sm:border-l sm:border-t-0 sm:px-5">
            <div className="text-xs text-[var(--text-muted)]">历史注册成功邮箱</div>
            <div className="mt-1 text-2xl font-semibold text-[var(--text-primary)]">{survivalStats?.historical_registered_emails ?? '-'}</div>
          </div>
          <div className="border-t border-[var(--border)] py-2 sm:border-l sm:border-t-0 sm:pl-5">
            <div className="text-xs text-[var(--text-muted)]">存活率</div>
            <div className="mt-1 text-2xl font-semibold text-sky-400">{survivalStats ? `${survivalStats.survival_rate.toFixed(2)}%` : '-'}</div>
          </div>
        </div>
        {createdTask ? (
          <div className="mt-4 flex flex-wrap items-center justify-between gap-3 rounded-md border border-sky-500/30 bg-sky-500/10 px-3 py-2 text-sm text-sky-200">
            <span>{createdTask.title}：<span className="font-mono text-xs">{createdTask.id}</span></span>
            <Button size="sm" variant="outline" onClick={() => navigate('/tasks')}>查看任务</Button>
          </div>
        ) : null}
        {error ? <div className="mt-4 rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">{error}</div> : null}
      </Card>

      <Card className="overflow-hidden border border-[var(--border)] bg-[var(--bg-card)] p-0">
        <div className="flex flex-col gap-3 border-b border-[var(--border)] px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
          <input
            value={search}
            onChange={event => setSearch(event.target.value)}
            placeholder="搜索账号"
            className="w-full max-w-sm rounded-md border border-[var(--border)] bg-transparent px-3 py-1.5 text-sm text-[var(--text-primary)] placeholder:text-[var(--text-muted)]"
          />
          <div className="flex items-center gap-2">
            <Button
              variant={hasRefreshTokenOnly ? 'default' : 'outline'}
              size="sm"
              className="whitespace-nowrap"
              aria-pressed={hasRefreshTokenOnly}
              onClick={() => setHasRefreshTokenOnly(value => !value)}
            >
              <KeyRound className="mr-1.5 h-4 w-4" />仅看有 RT
            </Button>
            <Button variant="ghost" size="sm" onClick={() => void load()} disabled={loading}>
              <RefreshCw className={`mr-1.5 h-4 w-4 ${loading ? 'animate-spin' : ''}`} />刷新
            </Button>
          </div>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[600px] text-sm">
            <thead className="bg-[var(--bg-pane)]/60 text-left text-xs uppercase tracking-wide text-[var(--text-muted)]">
              <tr>
                <th className="px-4 py-3 font-medium">账号</th>
                <th className="px-4 py-3 font-medium">密码</th>
                <th className="px-4 py-3 font-medium">RT</th>
                <th className="px-4 py-3 font-medium">401 状态</th>
                <th className="px-4 py-3 font-medium">注册时间</th>
                <th className="px-4 py-3 font-medium"></th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[var(--border)]">
              {accounts.map(account => (
                <tr key={account.id} className="hover:bg-[var(--bg-hover)]/60">
                  <td className="px-4 py-3 font-mono text-[var(--text-primary)]">{account.email}</td>
                  <td className="px-4 py-3 font-mono text-[var(--text-secondary)]">{account.password || '-'}</td>
                  <td className="px-4 py-3">
                    <span className={account.has_refresh_token ? 'text-emerald-400' : 'text-[var(--text-muted)]'}>
                      {account.has_refresh_token ? '有 RT' : '无 RT'}
                    </span>
                  </td>
                  <td className="px-4 py-3">{statePill(account.refresh_token_status)}</td>
                  <td className="px-4 py-3 text-[var(--text-secondary)]">{formatDate(account.created_at)}</td>
                  <td className="px-4 py-3 text-right">
                    <button
                      type="button"
                      onClick={() => void copyAccount(account)}
                      title="复制 账号 / 密码 / 2FA 查看链接"
                      className="inline-flex items-center gap-1 rounded border border-[var(--border)] bg-[var(--bg-pane)]/40 px-2 py-1 text-xs text-[var(--text-muted)] hover:text-[var(--text-primary)]"
                    >
                      <Copy className="h-3.5 w-3.5" />
                      {copiedId === account.id ? '已复制' : '复制'}
                    </button>
                  </td>
                </tr>
              ))}
              {!loading && accounts.length === 0 ? (
                <tr><td colSpan={6} className="px-4 py-16 text-center text-[var(--text-muted)]">暂无账号</td></tr>
              ) : null}
            </tbody>
          </table>
        </div>
        <div className="flex flex-wrap items-center justify-between gap-3 border-t border-[var(--border)] px-4 py-3 text-sm text-[var(--text-muted)]">
          <span>第 {page} / {totalPages} 页</span>
          <div className="flex items-center gap-2">
            <select value={pageSize} onChange={event => setPageSize(Number(event.target.value))} className="rounded-md border border-[var(--border)] bg-transparent px-2 py-1 text-[var(--text-primary)]">
              <option value={20}>20 / 页</option>
              <option value={50}>50 / 页</option>
              <option value={100}>100 / 页</option>
            </select>
            <Button variant="outline" size="sm" disabled={page <= 1 || loading} onClick={() => setPage(value => value - 1)}><ChevronLeft className="h-4 w-4" /></Button>
            <Button variant="outline" size="sm" disabled={page >= totalPages || loading} onClick={() => setPage(value => value + 1)}><ChevronRight className="h-4 w-4" /></Button>
          </div>
        </div>
      </Card>
    </div>
  )
}
