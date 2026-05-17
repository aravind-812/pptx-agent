import { useState, useRef, useEffect, useCallback } from 'react'
import {
  Upload, FileText, Download, X, Brain, Cpu, Eye,
  CheckCircle2, XCircle, AlertTriangle, ArrowRight,
  ChevronLeft, ChevronRight, ZoomIn, Zap
} from 'lucide-react'

// ─── Node metadata ─────────────────────────────────────────────
const NODE_META = {
  planner:  { label: 'Planner',  Icon: Brain, col: '#a78bfa' },
  executor: { label: 'Executor', Icon: Cpu,   col: '#38bdf8' },
  reviewer: { label: 'Reviewer', Icon: Eye,   col: '#fb923c' },
}

// ─── Log renderers ──────────────────────────────────────────────
function NodeHeader({ node }) {
  const m = NODE_META[node] || { label: node, Icon: ArrowRight, col: '#5a5a6a' }
  return (
    <div className="flex items-center gap-3 py-2.5 mt-2 animate-in">
      <div style={{ background: m.col }} className="h-px flex-1 opacity-30" />
      <div className="flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-semibold uppercase tracking-widest"
        style={{ color: m.col, border: `1px solid ${m.col}30`, background: `${m.col}10` }}>
        <m.Icon size={11} />
        {m.label}
      </div>
      <div style={{ background: m.col }} className="h-px flex-1 opacity-30" />
    </div>
  )
}

function ThinkingLine() {
  return (
    <div className="flex items-center gap-2 pl-2 py-0.5 animate-in">
      <span className="text-ink-muted font-mono text-xs">&gt;</span>
      <span className="text-ink-muted italic font-mono text-xs">model reasoning</span>
      <span className="flex gap-0.5 items-center">
        {[0,1,2].map(i => (
          <span key={i} className="w-1 h-1 rounded-full bg-ink-faint animate-bounce"
            style={{ animationDelay: `${i*0.15}s` }} />
        ))}
      </span>
    </div>
  )
}

function ToolCallLine({ tool, args }) {
  const str = typeof args === 'string'
    ? args.slice(0, 88)
    : Object.entries(args || {}).map(([k,v]) => `${k}=${String(v).slice(0,30)}`).join(', ').slice(0, 88)
  return (
    <div className="flex items-start gap-1.5 pl-2 py-0.5 animate-in font-mono text-xs">
      <ArrowRight size={10} className="mt-0.5 shrink-0" style={{ color: '#38bdf8' }} />
      <span>
        <span style={{ color: '#7dd3fc' }} className="font-medium">{tool}</span>
        <span className="text-ink-faint">(</span>
        <span style={{ color: '#38bdf880' }}>{str}</span>
        <span className="text-ink-faint">)</span>
      </span>
    </div>
  )
}

function ToolResultLine({ output }) {
  let p = ''
  if (output && typeof output === 'object') {
    p = Object.keys(output).slice(0,4).map(k => `${k}: ${String(output[k]).slice(0,25)}`).join('  ·  ')
  } else {
    p = String(output ?? '').replace(/\n/g,' ').slice(0, 110)
  }
  return (
    <div className="flex items-start gap-1.5 pl-2 py-0.5 animate-in font-mono text-xs" style={{ color: '#4ade8088' }}>
      <CheckCircle2 size={10} className="mt-0.5 shrink-0" style={{ color: '#4ade80' }} />
      <span>{p}</span>
    </div>
  )
}

function ToolErrorLine({ error }) {
  return (
    <div className="flex items-start gap-1.5 pl-2 py-0.5 animate-in font-mono text-xs text-red-400">
      <XCircle size={10} className="mt-0.5 shrink-0" />
      <span>{String(error).slice(0, 110)}</span>
    </div>
  )
}

function PlanSummaryLine({ edits, removes, multi_phase, summary }) {
  return (
    <div className="ml-2 my-2 p-3 rounded-lg animate-in"
      style={{ border: '1px solid #a78bfa30', background: '#a78bfa08' }}>
      <div className="flex items-center flex-wrap gap-x-3 gap-y-1 mb-1.5">
        <span className="font-mono text-xs font-semibold" style={{ color: '#c4b5fd' }}>{edits} edits</span>
        <span className="text-ink-faint text-xs">·</span>
        <span className="font-mono text-xs font-semibold" style={{ color: '#c4b5fd' }}>{removes} removals</span>
        <span className="text-ink-faint text-xs">·</span>
        <span className="text-xs px-2 py-0.5 rounded-full font-medium"
          style={{ background: '#a78bfa18', color: '#c4b5fd', border: '1px solid #a78bfa30' }}>
          {multi_phase ? 'multi-phase' : 'single-phase'}
        </span>
      </div>
      {summary && <p className="text-xs italic leading-relaxed" style={{ color: '#5a5a6a' }}>{summary}</p>}
    </div>
  )
}

function ReviewerDoneLine({ verdict, issues }) {
  const pass = verdict === 'PASS'
  return (
    <div className="ml-2 my-2 p-3 rounded-lg animate-in"
      style={{
        border: `1px solid ${pass ? '#22c55e30' : '#f5953030'}`,
        background: pass ? '#22c55e08' : '#f5953008',
      }}>
      <div className="flex items-center gap-2 font-semibold text-xs mb-1"
        style={{ color: pass ? '#4ade80' : '#fb923c' }}>
        {pass ? <CheckCircle2 size={12} /> : <AlertTriangle size={12} />}
        VERDICT: {verdict}
      </div>
      {issues.map((iss, i) => (
        <div key={i} className="text-xs pl-1 flex items-start gap-1.5 mt-0.5" style={{ color: '#5a5a6a' }}>
          <span className="mt-0.5 shrink-0">·</span><span>{iss}</span>
        </div>
      ))}
    </div>
  )
}

function DoneLine() {
  return (
    <div className="mx-2 my-3 p-3 rounded-lg text-center animate-in"
      style={{ border: '1px solid #c8f13540', background: '#c8f13508' }}>
      <div className="font-display font-bold text-sm tracking-wide" style={{ color: '#c8f135' }}>
        ✦ GENERATION COMPLETE
      </div>
      <p className="text-xs mt-0.5" style={{ color: '#5a5a6a' }}>Download your edited deck below</p>
    </div>
  )
}

function ModelInfoLine({ provider, model }) {
  const providerLabel = provider === 'anthropic' ? 'Anthropic' : provider === 'openai' ? 'OpenAI' : provider
  return (
    <div className="flex items-center gap-2 pl-2 py-1.5 animate-in">
      <Zap size={10} style={{ color: '#c8f135' }} />
      <span className="font-mono text-xs" style={{ color: '#5a5a6a' }}>
        <span style={{ color: '#c8f13590' }}>{providerLabel}</span>
        <span style={{ color: '#3e3e4e' }}> / </span>
        <span style={{ color: '#a3e635' }}>{model}</span>
      </span>
    </div>
  )
}

function ErrorLine({ msg }) {
  return (
    <div className="mx-2 my-2 p-3 rounded-lg animate-in"
      style={{ border: '1px solid #ef444430', background: '#ef444408' }}>
      <div className="flex items-start gap-2 text-red-400 text-xs font-mono">
        <XCircle size={12} className="mt-0.5 shrink-0" />
        <span>{msg}</span>
      </div>
    </div>
  )
}

function LogEntry({ entry }) {
  switch (entry.type) {
    case 'model_info':    return <ModelInfoLine provider={entry.provider} model={entry.model} />
    case 'node_start':    return <NodeHeader node={entry.node} />
    case 'thinking':      return <ThinkingLine />
    case 'tool_call':     return <ToolCallLine tool={entry.tool} args={entry.args} />
    case 'tool_result':   return <ToolResultLine output={entry.output} />
    case 'tool_error':    return <ToolErrorLine error={entry.error} />
    case 'plan_summary':  return <PlanSummaryLine {...entry} />
    case 'executor_done': return null
    case 'reviewer_done': return <ReviewerDoneLine verdict={entry.verdict} issues={entry.issues} />
    case 'done':          return <DoneLine />
    case 'error':         return <ErrorLine msg={entry.msg} />
    default:              return null
  }
}

// ─── Dropzone ──────────────────────────────────────────────────
function Dropzone({ accept, label, hint, file, onFile, disabled }) {
  const [drag, setDrag] = useState(false)
  const ref = useRef(null)
  const drop = useCallback((e) => {
    e.preventDefault(); setDrag(false)
    if (!disabled && e.dataTransfer.files[0]) onFile(e.dataTransfer.files[0])
  }, [disabled, onFile])

  const borderColor = drag ? '#c8f135' : file ? '#4ade8060' : '#1e1e28'
  const bg = drag ? 'rgba(200,241,53,0.06)' : file ? 'rgba(74,222,128,0.04)' : 'transparent'

  return (
    <div onClick={() => !disabled && ref.current?.click()}
      onDragOver={(e) => { e.preventDefault(); if (!disabled) setDrag(true) }}
      onDragLeave={() => setDrag(false)}
      onDrop={drop}
      style={{ border: `1.5px dashed ${borderColor}`, background: bg, borderRadius: 8,
        cursor: disabled ? 'not-allowed' : 'pointer', transition: 'all 0.15s ease',
        opacity: disabled && !file ? 0.4 : 1 }}
      className="p-4">
      <input ref={ref} type="file" accept={accept} className="hidden"
        onChange={(e) => e.target.files[0] && onFile(e.target.files[0])} disabled={disabled} />
      {file ? (
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2 min-w-0" style={{ color: '#4ade80' }}>
            <FileText size={14} className="shrink-0" />
            <span className="text-sm font-medium truncate">{file.name}</span>
            <span className="text-xs shrink-0" style={{ color: '#5a5a6a' }}>{(file.size/1024).toFixed(0)}KB</span>
          </div>
          <button onClick={(e) => { e.stopPropagation(); onFile(null) }} disabled={disabled}
            style={{ color: '#5a5a6a' }} className="hover:text-white transition-colors shrink-0">
            <X size={13} />
          </button>
        </div>
      ) : (
        <div className="text-center py-1">
          <Upload size={16} className="mx-auto mb-1.5" style={{ color: '#3e3e4e' }} />
          <p className="text-sm font-medium" style={{ color: '#5a5a6a' }}>{label}</p>
          <p className="text-xs mt-0.5" style={{ color: '#3e3e4e' }}>{hint}</p>
        </div>
      )}
    </div>
  )
}

// ─── Slide viewer ──────────────────────────────────────────────
function SlideViewer({ fileId, slideCount }) {
  const [active, setActive] = useState(0)
  const [modal, setModal] = useState(false)

  if (!fileId || !slideCount) return null

  const slides = Array.from({ length: slideCount }, (_, i) => i)
  const prev = () => setActive(a => Math.max(0, a - 1))
  const next = () => setActive(a => Math.min(slideCount - 1, a + 1))

  const slideUrl = (i) => `/api/slides/${fileId}/${i}`

  return (
    <div className="mt-6 animate-in">
      <div className="flex items-center justify-between mb-3">
        <h3 className="font-display font-bold text-sm uppercase tracking-widest" style={{ color: '#c8f135' }}>
          ✦ Slide Preview
        </h3>
        <span className="text-xs" style={{ color: '#5a5a6a' }}>{slideCount} slides</span>
      </div>

      {/* Active slide large view */}
      <div className="relative rounded-xl overflow-hidden mb-3"
        style={{ background: '#0a0a0f', border: '1px solid #1e1e28', aspectRatio: '16/9' }}>
        <img
          src={slideUrl(active)} alt={`Slide ${active + 1}`}
          className="w-full h-full object-contain"
          style={{ imageRendering: 'crisp-edges' }}
        />
        <button onClick={() => setModal(true)}
          className="absolute top-2 right-2 p-1.5 rounded-lg transition-all hover:scale-105"
          style={{ background: 'rgba(10,10,15,0.8)', border: '1px solid #1e1e28', color: '#c8f135' }}>
          <ZoomIn size={14} />
        </button>
        {slideCount > 1 && (
          <>
            <button onClick={prev} disabled={active === 0}
              className="absolute left-2 top-1/2 -translate-y-1/2 p-1.5 rounded-lg transition-all"
              style={{ background: 'rgba(10,10,15,0.8)', border: '1px solid #1e1e28', color: active === 0 ? '#2e2e3a' : '#f0f0f5' }}>
              <ChevronLeft size={16} />
            </button>
            <button onClick={next} disabled={active === slideCount - 1}
              className="absolute right-2 top-1/2 -translate-y-1/2 p-1.5 rounded-lg transition-all"
              style={{ background: 'rgba(10,10,15,0.8)', border: '1px solid #1e1e28', color: active === slideCount - 1 ? '#2e2e3a' : '#f0f0f5' }}>
              <ChevronRight size={16} />
            </button>
          </>
        )}
        <div className="absolute bottom-2 left-1/2 -translate-x-1/2 px-2 py-0.5 rounded-full text-xs"
          style={{ background: 'rgba(10,10,15,0.8)', border: '1px solid #1e1e28', color: '#5a5a6a' }}>
          {active + 1} / {slideCount}
        </div>
      </div>

      {/* Thumbnail strip */}
      <div className="flex gap-2 overflow-x-auto pb-1">
        {slides.map(i => (
          <button key={i} onClick={() => setActive(i)}
            className="shrink-0 rounded-lg overflow-hidden transition-all hover:scale-105"
            style={{
              width: 96, aspectRatio: '16/9',
              border: `1.5px solid ${i === active ? '#c8f135' : '#1e1e28'}`,
              outline: i === active ? '0' : 'none',
              background: '#0a0a0f',
            }}>
            <img src={slideUrl(i)} alt={`Slide ${i + 1}`} className="w-full h-full object-contain" />
          </button>
        ))}
      </div>

      {/* Modal */}
      {modal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4"
          style={{ background: 'rgba(5,5,7,0.96)' }} onClick={() => setModal(false)}>
          <div onClick={e => e.stopPropagation()} className="relative max-w-5xl w-full">
            <img src={slideUrl(active)} alt={`Slide ${active + 1}`}
              className="w-full rounded-xl" style={{ border: '1px solid #1e1e28' }} />
            <button onClick={() => setModal(false)}
              className="absolute -top-3 -right-3 p-2 rounded-full"
              style={{ background: '#111118', border: '1px solid #1e1e28', color: '#f0f0f5' }}>
              <X size={16} />
            </button>
            {slideCount > 1 && (
              <div className="flex items-center justify-center gap-4 mt-4">
                <button onClick={prev} disabled={active === 0}
                  className="p-2 rounded-lg transition-all"
                  style={{ background: '#111118', border: '1px solid #1e1e28', color: active === 0 ? '#2e2e3a' : '#f0f0f5' }}>
                  <ChevronLeft size={20} />
                </button>
                <span className="text-sm" style={{ color: '#5a5a6a' }}>{active + 1} / {slideCount}</span>
                <button onClick={next} disabled={active === slideCount - 1}
                  className="p-2 rounded-lg transition-all"
                  style={{ background: '#111118', border: '1px solid #1e1e28', color: active === slideCount - 1 ? '#2e2e3a' : '#f0f0f5' }}>
                  <ChevronRight size={20} />
                </button>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

// ─── App ───────────────────────────────────────────────────────
export default function App() {
  const [template, setTemplate] = useState(null)
  const [txText, setTxText] = useState('')
  const [txFile, setTxFile] = useState(null)
  const [running, setRunning] = useState(false)
  const [log, setLog] = useState([])
  const [fileId, setFileId] = useState(null)
  const [slideCount, setSlideCount] = useState(0)
  const [verdict, setVerdict] = useState(null)
  const [providers, setProviders] = useState([])
  const [selProvider, setSelProvider] = useState('')
  const [activeModels, setActiveModels] = useState(null)
  const logRef = useRef(null)
  const abortRef = useRef(null)

  useEffect(() => {
    fetch('/api/providers')
      .then(r => r.json())
      .then(data => {
        setProviders(data)
        if (data.length > 0) {
          setSelProvider(data[0].id)
        }
      })
      .catch(() => {})
  }, [])

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight
  }, [log])

  const push = useCallback((e) => setLog(p => [...p, e]), [])

  const generate = useCallback(async () => {
    if (!template) return
    const tx = txText.trim()
    if (!tx && !txFile) return

    setRunning(true)
    setLog([])
    setFileId(null)
    setSlideCount(0)
    setVerdict(null)
    setActiveModels(null)

    const fd = new FormData()
    fd.append('template', template)
    fd.append('transcript', tx)
    if (txFile) fd.append('transcript_file', txFile)
    if (selProvider) fd.append('provider', selProvider)

    const ctrl = new AbortController()
    abortRef.current = ctrl

    try {
      const res = await fetch('/api/generate', { method: 'POST', body: fd, signal: ctrl.signal })
      if (!res.ok) { push({ type: 'error', msg: `HTTP ${res.status}` }); return }

      const reader = res.body.getReader()
      const dec = new TextDecoder()
      let buf = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buf += dec.decode(value, { stream: true })
        const lines = buf.split('\n')
        buf = lines.pop() ?? ''
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue
          try {
            const ev = JSON.parse(line.slice(6))
            if (ev.type === 'done') { setFileId(ev.file_id) }
            if (ev.type === 'slides_ready') { setSlideCount(ev.count) }
            if (ev.type === 'reviewer_done') { setVerdict(ev.verdict) }
            if (ev.type === 'model_info') { setActiveModels({ provider: ev.provider, planner: ev.planner_model, executor: ev.executor_model }) }
            push(ev)
          } catch {}
        }
      }
    } catch (e) {
      if (e.name !== 'AbortError') push({ type: 'error', msg: e.message })
    } finally {
      setRunning(false)
    }
  }, [template, txText, txFile, push])

  const canRun = template && (txText.trim() || txFile) && !running

  return (
    <div className="min-h-screen" style={{ background: '#050507' }}>
      <div className="max-w-screen-xl mx-auto px-6 py-10">

        {/* ── Header ── */}
        <header className="mb-10">
          <div className="flex items-start justify-between">
            <div>
              <h1 className="font-display font-extrabold leading-none tracking-tight"
                style={{ fontSize: 'clamp(2.5rem, 6vw, 5rem)', color: '#f0f0f5', letterSpacing: '-0.03em' }}>
                PPT<span style={{ color: '#c8f135' }}>.</span>AGENT
              </h1>
              <p className="mt-2 text-sm font-light" style={{ color: '#5a5a6a', maxWidth: 380 }}>
                Drop any PPTX template and content — AI reads the template, fills every placeholder
              </p>
            </div>
            <span className="px-2.5 py-1 rounded-md text-xs font-semibold font-display tracking-widest uppercase mt-2"
              style={{ background: '#111118', color: '#5a5a6a', border: '1px solid #1e1e28' }}>
              beta
            </span>
          </div>
          <div className="mt-6 h-px" style={{ background: 'linear-gradient(90deg, #c8f135 0%, #c8f13540 30%, transparent 70%)' }} />

          {/* ── Model selector ── */}
          {providers.length > 0 && (
            <div className="mt-5 flex flex-col gap-3">
              <div className="flex items-center gap-3 flex-wrap">
                <span className="text-xs font-mono uppercase tracking-widest" style={{ color: '#3e3e4e' }}>Provider</span>
                {providers.map(p => {
                  const active = selProvider === p.id
                  return (
                    <button key={p.id} onClick={() => setSelProvider(p.id)} disabled={running}
                      className="px-2.5 py-1 rounded-md text-xs font-semibold transition-all"
                      style={{
                        background: active ? '#c8f13515' : '#0a0a0f',
                        border: `1px solid ${active ? '#c8f13560' : '#1e1e28'}`,
                        color: active ? '#c8f135' : '#5a5a6a',
                        cursor: running ? 'not-allowed' : 'pointer',
                      }}>
                      {p.label}
                    </button>
                  )
                })}
                {activeModels && (
                  <div className="ml-auto flex items-center gap-2 px-2.5 py-1 rounded-md"
                    style={{ background: '#c8f13508', border: '1px solid #c8f13530' }}>
                    <Zap size={10} style={{ color: '#c8f135' }} />
                    <span className="font-mono text-xs" style={{ color: '#a3e635' }}>{activeModels.planner}</span>
                    <span className="font-mono text-xs" style={{ color: '#3e3e4e' }}>+</span>
                    <span className="font-mono text-xs" style={{ color: '#6ee7b7' }}>{activeModels.executor}</span>
                  </div>
                )}
              </div>
              {/* Model split info */}
              <div className="flex items-center gap-4 font-mono text-xs" style={{ color: '#3e3e4e' }}>
                <span>Planner →</span>
                <span style={{ color: '#a78bfa' }}>{selProvider === 'openai' ? 'gpt-4o' : 'claude-sonnet-4-6'}</span>
                <span style={{ color: '#2e2e3a' }}>·</span>
                <span>Executor + Reviewer →</span>
                <span style={{ color: '#6ee7b7' }}>{selProvider === 'openai' ? 'gpt-4o-mini' : 'claude-haiku-4-5'}</span>
              </div>
            </div>
          )}
          {providers.length === 0 && (
            <div className="mt-5 px-3 py-2 rounded-md text-xs font-mono animate-in"
              style={{ background: '#2e0a0a', border: '1px solid #ef444430', color: '#fca5a5' }}>
              No API keys found — set ANTHROPIC_API_KEY or OPENAI_API_KEY in .env
            </div>
          )}
        </header>

        {/* ── Steps legend ── */}
        <div className="flex items-center gap-0 mb-6 overflow-x-auto">
          {[
            { n: '01', label: 'Upload Template', done: !!template },
            { n: '02', label: 'Add Content',  done: !!(txText.trim() || txFile) },
            { n: '03', label: 'Edit',         done: !!fileId },
            { n: '04', label: 'Download & View',  done: !!fileId && slideCount > 0 },
          ].map((s, i, arr) => (
            <div key={s.n} className="flex items-center shrink-0">
              <div className="flex items-center gap-2 px-3 py-1.5 rounded-lg transition-all"
                style={{
                  background: s.done ? '#c8f13515' : '#0a0a0f',
                  border: `1px solid ${s.done ? '#c8f13540' : '#1e1e28'}`,
                }}>
                <span className="font-display font-bold text-xs"
                  style={{ color: s.done ? '#c8f135' : '#3e3e4e' }}>{s.n}</span>
                <span className="text-xs font-medium"
                  style={{ color: s.done ? '#f0f0f5' : '#5a5a6a' }}>{s.label}</span>
                {s.done && <span style={{ color: '#c8f135', fontSize: 10 }}>✓</span>}
              </div>
              {i < arr.length - 1 && (
                <div className="w-6 h-px mx-0.5" style={{ background: '#1e1e28' }} />
              )}
            </div>
          ))}
        </div>

        {/* ── Main grid ── */}
        <div className="grid gap-5" style={{ gridTemplateColumns: 'minmax(320px,380px) 1fr' }}>

          {/* Left: inputs */}
          <div className="flex flex-col gap-4">

            {/* Template */}
            <div className="card p-4" style={template ? { borderLeft: '2px solid #c8f135' } : {}}>
              <div className="flex items-center gap-2.5 mb-3">
                <span className="font-display font-bold text-lg leading-none"
                  style={{ color: '#c8f135' }}>01</span>
                <div>
                  <p className="text-sm font-semibold" style={{ color: '#f0f0f5' }}>Upload PPTX Template</p>
                  <p className="text-xs" style={{ color: '#5a5a6a' }}>The base slide deck to customise</p>
                </div>
              </div>
              <Dropzone accept=".pptx" label="Drop template here" hint=".pptx · drag or click"
                file={template} onFile={setTemplate} disabled={running} />
            </div>

            {/* Transcript */}
            <div className="card p-4 flex-1" style={(txText.trim() || txFile) ? { borderLeft: '2px solid #c8f135' } : {}}>
              <div className="flex items-center gap-2.5 mb-3">
                <span className="font-display font-bold text-lg leading-none"
                  style={{ color: '#c8f135' }}>02</span>
                <div>
                  <p className="text-sm font-semibold" style={{ color: '#f0f0f5' }}>Content / Instructions</p>
                  <p className="text-xs" style={{ color: '#5a5a6a' }}>Paste notes, briefs, data, or any text</p>
                </div>
              </div>
              <textarea
                value={txText} onChange={e => setTxText(e.target.value)} disabled={running}
                placeholder="Paste the content, notes, or instructions to fill the template with…"
                rows={10}
                style={{
                  width: '100%', background: '#050507', border: '1px solid #1e1e28', borderRadius: 8,
                  padding: '10px 12px', color: '#f0f0f5', fontSize: 13, lineHeight: 1.6,
                  resize: 'vertical', fontFamily: 'Space Grotesk, sans-serif',
                  outline: 'none', opacity: running ? 0.5 : 1,
                }}
                onFocus={e => e.target.style.borderColor = '#2e2e3e'}
                onBlur={e => e.target.style.borderColor = '#1e1e28'}
              />
              <div className="flex items-center gap-2 my-3">
                <div className="h-px flex-1" style={{ background: '#1e1e28' }} />
                <span className="text-xs" style={{ color: '#3e3e4e' }}>or upload</span>
                <div className="h-px flex-1" style={{ background: '#1e1e28' }} />
              </div>
              <Dropzone accept=".txt" label="Upload .txt file" hint="Drop or click to browse"
                file={txFile} onFile={setTxFile} disabled={running || !!txText.trim()} />
            </div>

            {/* Step 03 — Generate */}
            <div>
              <div className="flex items-center gap-2.5 mb-2">
                <span className="font-display font-bold text-lg leading-none"
                  style={{ color: canRun || running ? '#c8f135' : '#3e3e4e' }}>03</span>
                <div>
                  <p className="text-sm font-semibold" style={{ color: canRun || running ? '#f0f0f5' : '#5a5a6a' }}>Edit Presentation</p>
                  <p className="text-xs" style={{ color: '#5a5a6a' }}>Runs planner → executor → reviewer</p>
                </div>
              </div>
              <button onClick={running ? () => { abortRef.current?.abort(); setRunning(false) } : generate}
                disabled={!running && !canRun}
                className="btn-generate w-full py-4 flex items-center justify-center gap-2.5">
                {running ? (
                  <>
                    <span className="w-3.5 h-3.5 rounded-full border-2 border-black/30 border-t-black spin" />
                    Stop generation
                  </>
                ) : (
                  'Edit Presentation →'
                )}
              </button>
            </div>

            {/* Step 04 — Download */}
            {fileId && (
              <div>
              <div className="flex items-center gap-2.5 mb-2">
                <span className="font-display font-bold text-lg leading-none" style={{ color: '#c8f135' }}>04</span>
                <div>
                  <p className="text-sm font-semibold" style={{ color: '#f0f0f5' }}>Download & View</p>
                  <p className="text-xs" style={{ color: '#5a5a6a' }}>Save the deck, preview slides below</p>
                </div>
              </div>
              <a href={`/api/download/${fileId}`} download="edited.pptx"
                className="flex items-center justify-center gap-2.5 py-3 rounded-lg font-semibold text-sm transition-all animate-in"
                style={{ border: '1px solid #4ade8040', background: '#4ade8008', color: '#4ade80' }}
                onMouseEnter={e => e.currentTarget.style.background = '#4ade8015'}
                onMouseLeave={e => e.currentTarget.style.background = '#4ade8008'}>
                <Download size={15} />
                Download edited.pptx
                {verdict && (
                  <span className="ml-auto text-xs px-2 py-0.5 rounded-full font-semibold"
                    style={{
                      background: verdict === 'PASS' ? '#22c55e18' : '#f59e0b18',
                      color: verdict === 'PASS' ? '#4ade80' : '#fbbf24',
                      border: `1px solid ${verdict === 'PASS' ? '#22c55e30' : '#f59e0b30'}`,
                    }}>
                    {verdict}
                  </span>
                )}
              </a>
              </div>
            )}
          </div>

          {/* Right: terminal */}
          <div className="card flex flex-col overflow-hidden" style={{ minHeight: 560, maxHeight: '80vh' }}>
            {/* Bar */}
            <div className="flex items-center gap-2 px-4 py-2.5 shrink-0"
              style={{ borderBottom: '1px solid #1e1e28', background: '#0a0a0f' }}>
              <div className="flex gap-1.5">
                {['#2e2e3a','#2e2e3a','#2e2e3a'].map((c,i) => (
                  <div key={i} className="w-2.5 h-2.5 rounded-full" style={{ background: c }} />
                ))}
              </div>
              <span className="font-mono text-xs ml-2" style={{ color: '#3e3e4e' }}>pipeline.log</span>
              {running && (
                <div className="ml-auto flex items-center gap-1.5">
                  <span className="w-1.5 h-1.5 rounded-full" style={{ background: '#c8f135', animation: 'blink 1.1s step-end infinite' }} />
                  <span className="text-xs font-mono" style={{ color: '#c8f135' }}>running</span>
                </div>
              )}
              {!running && fileId && (
                <div className="ml-auto flex items-center gap-1.5">
                  <CheckCircle2 size={11} style={{ color: '#4ade80' }} />
                  <span className="text-xs font-mono" style={{ color: '#4ade80' }}>complete</span>
                </div>
              )}
            </div>

            {/* Log body */}
            <div ref={logRef} className="flex-1 overflow-y-auto p-3" style={{ minHeight: 0, background: '#040408' }}>
              {log.length === 0 ? (
                <div className="h-full flex flex-col items-center justify-center gap-3 py-12">
                  <div className="w-10 h-10 rounded-xl flex items-center justify-center"
                    style={{ background: '#0a0a0f', border: '1px solid #1e1e28' }}>
                    <Cpu size={18} style={{ color: '#2e2e3a' }} />
                  </div>
                  <p className="text-xs" style={{ color: '#3e3e4e', fontFamily: 'JetBrains Mono, monospace' }}>
                    awaiting generation…
                  </p>
                </div>
              ) : (
                <div>
                  <div className="font-mono text-xs pb-2" style={{ color: '#3e3e4e' }}>
                    $ ppt-agent generate
                  </div>
                  {log.map((e, i) => e.type !== 'start' && <LogEntry key={i} entry={e} />)}
                  {running && (
                    <span className="font-mono text-xs cursor-blink ml-2" style={{ color: '#c8f135' }}>▋</span>
                  )}
                </div>
              )}
            </div>
          </div>
        </div>

        {/* ── Slide viewer ── */}
        {fileId && slideCount > 0 && (
          <SlideViewer fileId={fileId} slideCount={slideCount} />
        )}
        {fileId && slideCount === 0 && (
          <div className="mt-6 p-4 rounded-xl text-xs text-center animate-in"
            style={{ border: '1px solid #1e1e28', color: '#3e3e4e', fontFamily: 'JetBrains Mono, monospace' }}>
            Slide preview requires LibreOffice — install with{' '}
            <code className="px-1" style={{ color: '#5a5a6a' }}>brew install --cask libreoffice</code>
          </div>
        )}

        {/* ── Footer ── */}
        <footer className="mt-12 flex items-center justify-between">
          <p className="text-xs" style={{ color: '#2e2e3a', fontFamily: 'JetBrains Mono, monospace' }}>
            LangGraph · {activeModels ? `${activeModels.planner} + ${activeModels.executor}` : 'Sonnet + Haiku'} · Multi-agent
          </p>
          <div className="h-px flex-1 mx-6" style={{ background: '#1e1e28' }} />
          <p className="text-xs font-display font-bold tracking-widest uppercase" style={{ color: '#2e2e3a' }}>
            PPT.AGENT
          </p>
        </footer>
      </div>
    </div>
  )
}
