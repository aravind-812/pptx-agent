import { useState, useRef, useEffect, useCallback } from 'react'
import {
  Upload, Play, Square, CheckCircle2, XCircle, AlertTriangle,
  RefreshCw, Zap, FileText, BarChart2, FlaskConical, Wand2, ChevronDown, ChevronUp
} from 'lucide-react'

const EVAL_NAMES = ['placeholder', 'shape_coverage', 'faithfulness', 'hallucination', 'sanitization', 'completeness']
const EVAL_LABELS = {
  placeholder: 'Placeholder',
  shape_coverage: 'Coverage',
  faithfulness: 'Faithful',
  hallucination: 'Hallucin.',
  sanitization: 'Sanitize',
  completeness: 'Complete',
}
const EVAL_COLORS = {
  placeholder: '#BFDBFE',
  shape_coverage: '#BFDBFE',
  faithfulness: '#F9A8D4',
  hallucination: '#F9A8D4',
  sanitization: '#F9A8D4',
  completeness: '#FEF08A',
}

function PassBadge({ passed, confidence }) {
  if (passed === undefined || passed === null) return <span style={{ color: '#3e3e4e', fontSize: 11 }}>—</span>
  return (
    <span title={`confidence: ${(confidence * 100).toFixed(0)}%`}
      style={{
        display: 'inline-flex', alignItems: 'center', gap: 3,
        fontSize: 10, fontWeight: 700, padding: '2px 6px', borderRadius: 6,
        background: passed ? '#22c55e18' : '#ef444418',
        color: passed ? '#4ade80' : '#f87171',
        border: `1px solid ${passed ? '#22c55e30' : '#ef444430'}`,
      }}>
      {passed ? '✓' : '✗'}
    </span>
  )
}

function PassRate({ rate }) {
  const pct = Math.round((rate || 0) * 100)
  const color = pct >= 90 ? '#4ade80' : pct >= 70 ? '#fbbf24' : '#f87171'
  return (
    <div style={{ textAlign: 'center' }}>
      <div style={{ fontSize: 18, fontWeight: 800, color, fontFamily: 'monospace' }}>{pct}%</div>
      <div style={{ width: '100%', height: 4, background: '#1e1e28', borderRadius: 2, marginTop: 3 }}>
        <div style={{ width: `${pct}%`, height: '100%', background: color, borderRadius: 2, transition: 'width 0.5s ease' }} />
      </div>
    </div>
  )
}

// ── Run Tab ──────────────────────────────────────────────────
function RunTab({ providers, selProvider, setSelProvider }) {
  const [template, setTemplate] = useState(null)
  const [transcripts, setTranscripts] = useState([])
  const [running, setRunning] = useState(false)
  const [progress, setProgress] = useState({})  // transcript_id → result
  const [passRates, setPassRates] = useState(null)
  const [total, setTotal] = useState(0)
  const [done, setDone] = useState(0)
  const abortRef = useRef(null)
  const logRef = useRef(null)

  useEffect(() => {
    fetch('/api/eval/transcripts').then(r => r.json()).then(setTranscripts).catch(() => {})
  }, [])

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight
  }, [progress])

  const run = useCallback(async () => {
    if (!template || running) return
    setRunning(true)
    setProgress({})
    setPassRates(null)
    setDone(0)

    const fd = new FormData()
    fd.append('template', template)
    if (selProvider) fd.append('provider', selProvider)

    const ctrl = new AbortController()
    abortRef.current = ctrl

    try {
      const res = await fetch('/api/eval/run', { method: 'POST', body: fd, signal: ctrl.signal })
      if (!res.ok) { setRunning(false); return }

      const reader = res.body.getReader()
      const dec = new TextDecoder()
      let buf = ''

      while (true) {
        const { done: d, value } = await reader.read()
        if (d) break
        buf += dec.decode(value, { stream: true })
        const lines = buf.split('\n')
        buf = lines.pop() ?? ''
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue
          try {
            const ev = JSON.parse(line.slice(6))
            if (ev.type === 'eval_start') setTotal(ev.total)
            if (ev.type === 'eval_transcript_done') {
              setDone(ev.index)
              setProgress(p => ({ ...p, [ev.id]: { results: ev.results, error: ev.error, verdict: ev.review_verdict } }))
            }
            if (ev.type === 'eval_done') {
              setPassRates(ev.pass_rates)
              setRunning(false)
            }
            if (ev.type === 'error') setRunning(false)
          } catch {}
        }
      }
    } catch (e) {
      if (e.name !== 'AbortError') console.error(e)
    } finally {
      setRunning(false)
    }
  }, [template, selProvider, running])

  const completed = Object.keys(progress).length
  const pctDone = total > 0 ? Math.round((completed / total) * 100) : 0

  return (
    <div className="flex flex-col gap-5">
      {/* Template upload */}
      <div className="card p-4">
        <p className="text-xs font-semibold uppercase tracking-widest mb-3" style={{ color: '#5a5a6a' }}>
          PPTX Template
        </p>
        <div
          onClick={() => { const i = document.createElement('input'); i.type='file'; i.accept='.pptx'; i.onchange=e=>setTemplate(e.target.files[0]); i.click() }}
          style={{
            border: `1.5px dashed ${template ? '#4ade8060' : '#1e1e28'}`,
            background: template ? 'rgba(74,222,128,0.04)' : 'transparent',
            borderRadius: 8, padding: '12px 16px', cursor: 'pointer', textAlign: 'center',
          }}>
          {template ? (
            <div className="flex items-center gap-2 justify-center" style={{ color: '#4ade80' }}>
              <FileText size={14} />
              <span className="text-sm font-medium">{template.name}</span>
              <button onClick={e => { e.stopPropagation(); setTemplate(null) }} style={{ color: '#5a5a6a', marginLeft: 4 }}>✕</button>
            </div>
          ) : (
            <div style={{ color: '#3e3e4e', fontSize: 13 }}>
              <Upload size={14} className="mx-auto mb-1" />
              Drop or click to upload .pptx template
            </div>
          )}
        </div>
      </div>

      {/* Corpus info */}
      <div className="card p-4">
        <div className="flex items-center justify-between mb-3">
          <p className="text-xs font-semibold uppercase tracking-widest" style={{ color: '#5a5a6a' }}>Eval Corpus</p>
          <span className="text-xs font-mono" style={{ color: '#c8f135' }}>{transcripts.length} transcripts</span>
        </div>
        <div style={{ maxHeight: 120, overflowY: 'auto', display: 'flex', flexWrap: 'wrap', gap: 4 }}>
          {transcripts.map(t => (
            <span key={t.id} style={{
              fontSize: 10, padding: '2px 7px', borderRadius: 5, fontFamily: 'monospace',
              background: progress[t.id] ? (Object.values(progress[t.id]?.results || {}).every(r => r.passed) ? '#22c55e18' : '#ef444418') : '#0a0a0f',
              color: progress[t.id] ? (Object.values(progress[t.id]?.results || {}).every(r => r.passed) ? '#4ade80' : '#f87171') : '#3e3e4e',
              border: `1px solid ${progress[t.id] ? '#2e2e3a' : '#1e1e28'}`,
            }}>
              {t.id.replace('_transcript', '')}
            </span>
          ))}
        </div>
      </div>

      {/* Run button */}
      <button
        onClick={running ? () => { abortRef.current?.abort(); setRunning(false) } : run}
        disabled={!template && !running}
        style={{
          width: '100%', padding: '14px', borderRadius: 10, border: 'none', cursor: template || running ? 'pointer' : 'not-allowed',
          background: running ? '#2e1a1a' : template ? 'linear-gradient(135deg, #c8f135, #a3e635)' : '#0a0a0f',
          color: running ? '#f87171' : template ? '#000' : '#3e3e4e',
          fontWeight: 700, fontSize: 14, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 8,
          transition: 'all 0.15s',
        }}>
        {running ? (
          <><Square size={14} /> Stop evaluation</>
        ) : (
          <><Play size={14} /> Run {transcripts.length} transcripts</>
        )}
      </button>

      {/* Progress bar */}
      {(running || completed > 0) && (
        <div className="card p-4">
          <div className="flex items-center justify-between mb-2">
            <span className="text-xs font-mono" style={{ color: '#5a5a6a' }}>{completed}/{total} complete</span>
            <span className="text-xs font-mono" style={{ color: '#c8f135' }}>{pctDone}%</span>
          </div>
          <div style={{ width: '100%', height: 4, background: '#1e1e28', borderRadius: 2 }}>
            <div style={{ width: `${pctDone}%`, height: '100%', background: '#c8f135', borderRadius: 2, transition: 'width 0.3s ease' }} />
          </div>
        </div>
      )}

      {/* Pass rates summary */}
      {passRates && (
        <div className="card p-4 animate-in">
          <p className="text-xs font-semibold uppercase tracking-widest mb-3" style={{ color: '#c8f135' }}>Pass Rates</p>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 12 }}>
            {EVAL_NAMES.map(name => (
              <div key={name} style={{ textAlign: 'center' }}>
                <div style={{ fontSize: 10, color: '#5a5a6a', marginBottom: 4 }}>{EVAL_LABELS[name]}</div>
                <PassRate rate={passRates[name]} />
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Live log */}
      {Object.keys(progress).length > 0 && (
        <div className="card" style={{ maxHeight: 300, overflow: 'hidden', display: 'flex', flexDirection: 'column' }}>
          <div style={{ padding: '8px 12px', borderBottom: '1px solid #1e1e28', fontSize: 10, color: '#3e3e4e', fontFamily: 'monospace' }}>
            eval.log
          </div>
          <div ref={logRef} style={{ flex: 1, overflowY: 'auto', padding: '8px 12px' }}>
            {Object.entries(progress).map(([tid, rec]) => (
              <div key={tid} style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '3px 0', borderBottom: '1px solid #0a0a0f', fontSize: 11 }}>
                <span style={{ fontFamily: 'monospace', color: '#5a5a6a', minWidth: 120 }}>{tid.replace('_transcript', '')}</span>
                {EVAL_NAMES.map(name => (
                  <PassBadge key={name} passed={rec.results?.[name]?.passed} confidence={rec.results?.[name]?.confidence || 0} />
                ))}
                {rec.error && <span style={{ color: '#f87171', fontSize: 10 }}>ERR</span>}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

// ── Results Tab ──────────────────────────────────────────────
function ResultsTab() {
  const [runs, setRuns] = useState([])
  const [selected, setSelected] = useState(null)  // run index
  const [expandedCell, setExpandedCell] = useState(null)  // "tid:eval"
  const [loading, setLoading] = useState(false)

  const load = useCallback(() => {
    setLoading(true)
    fetch('/api/eval/results')
      .then(r => r.json())
      .then(d => { setRuns(d.runs || []); if (d.runs?.length) setSelected(d.runs.length - 1) })
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { load() }, [load])

  const run = selected !== null ? runs[selected] : null

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center gap-3">
        <p className="text-xs font-semibold uppercase tracking-widest" style={{ color: '#5a5a6a' }}>Benchmark History</p>
        <button onClick={load} style={{ color: '#5a5a6a', cursor: 'pointer' }}>
          <RefreshCw size={12} className={loading ? 'spin' : ''} />
        </button>
        {runs.length > 0 && (
          <select value={selected ?? ''} onChange={e => setSelected(Number(e.target.value))}
            style={{ marginLeft: 'auto', background: '#0a0a0f', border: '1px solid #1e1e28', color: '#f0f0f5', fontSize: 11, padding: '3px 8px', borderRadius: 6 }}>
            {runs.map((r, i) => (
              <option key={r.run_id} value={i}>Run {r.run_id} — {new Date(r.timestamp).toLocaleString()}</option>
            ))}
          </select>
        )}
      </div>

      {!run && !loading && (
        <div style={{ color: '#3e3e4e', fontSize: 13, textAlign: 'center', padding: '40px 0' }}>
          No benchmark results yet. Run the eval first.
        </div>
      )}

      {run && (
        <>
          {/* Pass rate summary row */}
          <div className="card p-4">
            <div style={{ display: 'grid', gridTemplateColumns: `repeat(${EVAL_NAMES.length}, 1fr)`, gap: 8 }}>
              {EVAL_NAMES.map(name => (
                <div key={name}>
                  <div style={{ fontSize: 10, color: '#5a5a6a', textAlign: 'center', marginBottom: 4 }}>{EVAL_LABELS[name]}</div>
                  <PassRate rate={run.pass_rates?.[name]} />
                </div>
              ))}
            </div>
          </div>

          {/* Heatmap table */}
          <div className="card" style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11 }}>
              <thead>
                <tr>
                  <th style={{ padding: '8px 12px', textAlign: 'left', color: '#3e3e4e', fontFamily: 'monospace', borderBottom: '1px solid #1e1e28' }}>Transcript</th>
                  {EVAL_NAMES.map(name => (
                    <th key={name} style={{ padding: '8px 8px', color: '#3e3e4e', textAlign: 'center', borderBottom: '1px solid #1e1e28', fontSize: 10 }}>
                      {EVAL_LABELS[name]}
                    </th>
                  ))}
                  <th style={{ padding: '8px 8px', color: '#3e3e4e', textAlign: 'center', borderBottom: '1px solid #1e1e28', fontSize: 10 }}>QA</th>
                </tr>
              </thead>
              <tbody>
                {run.details?.map(row => (
                  <>
                    <tr key={row.transcript_id} style={{ borderBottom: '1px solid #0a0a0f' }}>
                      <td style={{ padding: '6px 12px', fontFamily: 'monospace', color: '#5a5a6a' }}>
                        {row.transcript_id.replace('_transcript', '')}
                        {row.error && <span style={{ color: '#f87171', marginLeft: 4 }} title={row.error}>!</span>}
                      </td>
                      {EVAL_NAMES.map(name => {
                        const er = row.evals?.[name]
                        const cellKey = `${row.transcript_id}:${name}`
                        const expanded = expandedCell === cellKey
                        return (
                          <td key={name} style={{ padding: '4px 8px', textAlign: 'center', cursor: 'pointer' }}
                            onClick={() => setExpandedCell(expanded ? null : cellKey)}>
                            <PassBadge passed={er?.passed} confidence={er?.confidence || 0} />
                          </td>
                        )
                      })}
                      <td style={{ padding: '4px 8px', textAlign: 'center' }}>
                        <span style={{ fontSize: 10, color: row.review_verdict === 'PASS' ? '#4ade80' : '#fbbf24' }}>
                          {row.review_verdict || '—'}
                        </span>
                      </td>
                    </tr>
                    {expandedCell && expandedCell.startsWith(row.transcript_id + ':') && (
                      <tr key={`${row.transcript_id}-detail`}>
                        <td colSpan={EVAL_NAMES.length + 2} style={{ padding: '6px 12px 10px', background: '#040408' }}>
                          {(() => {
                            const evalName = expandedCell.split(':')[1]
                            const er = row.evals?.[evalName]
                            return er ? (
                              <div style={{ fontSize: 11, color: '#5a5a6a', lineHeight: 1.5 }}>
                                <span style={{ color: EVAL_COLORS[evalName] || '#c8f135', fontWeight: 600, marginRight: 6 }}>{evalName}:</span>
                                {er.reasoning}
                                {er.confidence !== undefined && (
                                  <span style={{ marginLeft: 6, color: '#3e3e4e' }}>({Math.round(er.confidence * 100)}% confidence)</span>
                                )}
                              </div>
                            ) : null
                          })()}
                        </td>
                      </tr>
                    )}
                  </>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  )
}

// ── Calibrate Tab ────────────────────────────────────────────
function CalibrateTab() {
  const [evalName, setEvalName] = useState('faithfulness')
  const [labels, setLabels] = useState({})  // transcript_id → bool
  const [transcripts, setTranscripts] = useState([])
  const [result, setResult] = useState(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    fetch('/api/eval/transcripts').then(r => r.json()).then(setTranscripts).catch(() => {})
  }, [])

  const toggle = (id) => setLabels(l => ({ ...l, [id]: !l[id] }))

  const runCalibrate = async () => {
    const labeled = Object.entries(labels).map(([id, passed]) => ({ transcript_id: id, passed }))
    if (labeled.length === 0) return
    setLoading(true)
    const fd = new FormData()
    fd.append('eval_name', evalName)
    fd.append('labels', JSON.stringify(labeled))
    try {
      const res = await fetch('/api/eval/calibrate', { method: 'POST', body: fd })
      setResult(await res.json())
    } catch (e) { console.error(e) }
    finally { setLoading(false) }
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="card p-4">
        <p className="text-xs font-semibold uppercase tracking-widest mb-3" style={{ color: '#5a5a6a' }}>
          Calibrate LLM Judge vs Human Labels
        </p>
        <p className="text-xs mb-4" style={{ color: '#3e3e4e', lineHeight: 1.6 }}>
          Label each transcript manually (PASS/FAIL), then compute F1. If F1 &lt; 0.85, upgrade that eval to Sonnet.
        </p>
        <div className="flex items-center gap-3 mb-4">
          <span className="text-xs" style={{ color: '#5a5a6a' }}>Eval:</span>
          <select value={evalName} onChange={e => setEvalName(e.target.value)}
            style={{ background: '#0a0a0f', border: '1px solid #1e1e28', color: '#f0f0f5', fontSize: 12, padding: '4px 10px', borderRadius: 6 }}>
            {['faithfulness', 'hallucination', 'sanitization', 'completeness'].map(n => (
              <option key={n} value={n}>{n}</option>
            ))}
          </select>
          <span className="text-xs ml-auto" style={{ color: '#5a5a6a' }}>{Object.keys(labels).length} labeled</span>
        </div>

        <div style={{ maxHeight: 300, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 4 }}>
          {transcripts.slice(0, 20).map(t => (
            <div key={t.id} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '5px 0', borderBottom: '1px solid #0a0a0f' }}>
              <span style={{ fontFamily: 'monospace', fontSize: 11, color: '#5a5a6a', flex: 1 }}>{t.id.replace('_transcript', '')}</span>
              <button onClick={() => toggle(t.id)}
                style={{
                  fontSize: 10, padding: '2px 10px', borderRadius: 5, border: '1px solid',
                  cursor: 'pointer', fontWeight: 700,
                  background: labels[t.id] === true ? '#22c55e18' : labels[t.id] === false ? '#ef444418' : '#0a0a0f',
                  color: labels[t.id] === true ? '#4ade80' : labels[t.id] === false ? '#f87171' : '#3e3e4e',
                  borderColor: labels[t.id] === true ? '#22c55e30' : labels[t.id] === false ? '#ef444430' : '#1e1e28',
                }}>
                {labels[t.id] === true ? '✓ PASS' : labels[t.id] === false ? '✗ FAIL' : 'label'}
              </button>
            </div>
          ))}
          {transcripts.length > 20 && (
            <p style={{ fontSize: 10, color: '#3e3e4e', textAlign: 'center', padding: '6px 0' }}>
              Label first 20 for calibration (representative sample)
            </p>
          )}
        </div>
      </div>

      <button onClick={runCalibrate} disabled={loading || Object.keys(labels).length === 0}
        style={{
          padding: '11px', borderRadius: 8, border: 'none', cursor: 'pointer', fontWeight: 700, fontSize: 13,
          background: Object.keys(labels).length > 0 ? 'linear-gradient(135deg, #c8f135, #a3e635)' : '#0a0a0f',
          color: Object.keys(labels).length > 0 ? '#000' : '#3e3e4e',
        }}>
        {loading ? 'Computing F1…' : `Compute F1 for ${evalName}`}
      </button>

      {result && !result.error && (
        <div className="card p-4 animate-in">
          <div style={{ display: 'flex', gap: 12, marginBottom: 12 }}>
            {[['F1', result.f1], ['Precision', result.precision], ['Recall', result.recall]].map(([label, val]) => (
              <div key={label} style={{ flex: 1, textAlign: 'center' }}>
                <div style={{ fontSize: 10, color: '#5a5a6a', marginBottom: 2 }}>{label}</div>
                <div style={{
                  fontSize: 20, fontWeight: 800, fontFamily: 'monospace',
                  color: Number(val) >= 0.85 ? '#4ade80' : '#f87171',
                }}>{(Number(val) * 100).toFixed(0)}%</div>
              </div>
            ))}
          </div>
          <div style={{
            fontSize: 12, padding: '8px 12px', borderRadius: 8,
            background: result.trusted ? '#22c55e10' : '#ef444410',
            border: `1px solid ${result.trusted ? '#22c55e30' : '#ef444430'}`,
            color: result.trusted ? '#4ade80' : '#f87171',
          }}>
            {result.recommendation}
          </div>
        </div>
      )}
      {result?.error && (
        <div style={{ color: '#f87171', fontSize: 12, padding: 12 }}>{result.error}</div>
      )}
    </div>
  )
}

// ── Improve Tab ──────────────────────────────────────────────
function ImproveTab() {
  const [evalName, setEvalName] = useState('hallucination')
  const [loading, setLoading] = useState(false)
  const [patch, setPatch] = useState(null)
  const [applying, setApplying] = useState(false)
  const [applied, setApplied] = useState(false)
  const [provider, setProvider] = useState('')

  const requestPatch = async () => {
    setLoading(true); setPatch(null); setApplied(false)
    const fd = new FormData()
    fd.append('eval_name', evalName)
    if (provider) fd.append('provider', provider)
    try {
      const res = await fetch('/api/eval/improve', { method: 'POST', body: fd })
      setPatch(await res.json())
    } catch (e) { console.error(e) }
    finally { setLoading(false) }
  }

  const applyPatch = async () => {
    if (!patch?.patch_id || applying) return
    setApplying(true)
    const fd = new FormData()
    fd.append('patch_id', patch.patch_id)
    try {
      const res = await fetch('/api/eval/apply-patch', { method: 'POST', body: fd })
      const data = await res.json()
      if (data.applied) setApplied(true)
    } catch (e) { console.error(e) }
    finally { setApplying(false) }
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="card p-4">
        <p className="text-xs font-semibold uppercase tracking-widest mb-1" style={{ color: '#5a5a6a' }}>Meta-Improve</p>
        <p className="text-xs mb-4" style={{ color: '#3e3e4e', lineHeight: 1.6 }}>
          Cluster failures from the latest benchmark run. Sonnet reads failed cases + current planner prompt and suggests a targeted patch. You approve before it's applied.
        </p>
        <div className="flex items-center gap-3">
          <span className="text-xs" style={{ color: '#5a5a6a' }}>Worst eval:</span>
          <select value={evalName} onChange={e => setEvalName(e.target.value)}
            style={{ background: '#0a0a0f', border: '1px solid #1e1e28', color: '#f0f0f5', fontSize: 12, padding: '4px 10px', borderRadius: 6 }}>
            {['hallucination', 'faithfulness', 'sanitization', 'completeness', 'placeholder', 'shape_coverage'].map(n => (
              <option key={n} value={n}>{n}</option>
            ))}
          </select>
        </div>
      </div>

      <button onClick={requestPatch} disabled={loading}
        style={{
          padding: '11px', borderRadius: 8, border: 'none', cursor: 'pointer', fontWeight: 700, fontSize: 13,
          background: 'linear-gradient(135deg, #a78bfa, #7c3aed)', color: '#fff',
          display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 8,
        }}>
        {loading ? <><RefreshCw size={13} className="spin" /> Analysing failures…</> : <><Wand2 size={13} /> Generate prompt patch</>}
      </button>

      {patch?.error && (
        <div style={{ color: '#f87171', fontSize: 12, padding: 12, background: '#ef444410', borderRadius: 8, border: '1px solid #ef444430' }}>
          {patch.error}
        </div>
      )}

      {patch && !patch.error && (
        <div className="card p-4 animate-in flex flex-col gap-4">
          <div>
            <div className="flex items-center gap-2 mb-2">
              <span style={{ fontSize: 10, color: '#5a5a6a', textTransform: 'uppercase', letterSpacing: 1 }}>Affected cases</span>
              <span style={{ fontFamily: 'monospace', color: '#c8f135', fontSize: 13, fontWeight: 700 }}>{patch.affected_cases}</span>
            </div>
            <div style={{ fontSize: 12, color: '#5a5a6a', lineHeight: 1.6 }}>{patch.reasoning}</div>
          </div>

          <div>
            <div style={{ fontSize: 10, color: '#5a5a6a', textTransform: 'uppercase', letterSpacing: 1, marginBottom: 6 }}>Failure pattern</div>
            <div style={{ fontSize: 12, color: '#f0f0f5', lineHeight: 1.6, background: '#0a0a0f', padding: '8px 12px', borderRadius: 8, border: '1px solid #1e1e28' }}>
              {patch.affected_pattern || '—'}
            </div>
          </div>

          <div>
            <div style={{ fontSize: 10, color: '#a78bfa', textTransform: 'uppercase', letterSpacing: 1, marginBottom: 6 }}>Suggested patch to add to planner.py prompt</div>
            <pre style={{
              fontSize: 11, color: '#c4b5fd', background: '#0a0a0f', padding: '12px', borderRadius: 8,
              border: '1px solid #a78bfa30', overflowX: 'auto', whiteSpace: 'pre-wrap', lineHeight: 1.6,
              fontFamily: 'JetBrains Mono, monospace',
            }}>
              {patch.patch_text}
            </pre>
          </div>

          {patch.insert_after && (
            <div>
              <div style={{ fontSize: 10, color: '#5a5a6a', textTransform: 'uppercase', letterSpacing: 1, marginBottom: 6 }}>Insert after</div>
              <pre style={{ fontSize: 10, color: '#3e3e4e', background: '#040408', padding: '8px 12px', borderRadius: 6, fontFamily: 'monospace', overflowX: 'auto' }}>
                {patch.insert_after}
              </pre>
            </div>
          )}

          {applied ? (
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '10px 14px', borderRadius: 8, background: '#22c55e10', border: '1px solid #22c55e30', color: '#4ade80', fontSize: 13, fontWeight: 600 }}>
              <CheckCircle2 size={14} /> Patch applied to agents/planner.py — re-run benchmark to measure impact
            </div>
          ) : (
            <div style={{ display: 'flex', gap: 8 }}>
              <button onClick={applyPatch} disabled={applying}
                style={{
                  flex: 1, padding: '10px', borderRadius: 8, border: '1px solid #22c55e30', cursor: 'pointer', fontWeight: 700, fontSize: 13,
                  background: '#22c55e18', color: '#4ade80',
                }}>
                {applying ? 'Applying…' : '✓ Apply patch to planner.py'}
              </button>
              <button onClick={() => setPatch(null)}
                style={{ padding: '10px 16px', borderRadius: 8, border: '1px solid #1e1e28', cursor: 'pointer', fontSize: 12, color: '#5a5a6a', background: 'transparent' }}>
                Discard
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// ── Main EvalLab ─────────────────────────────────────────────
export default function EvalLab({ providers, selProvider, setSelProvider }) {
  const [tab, setTab] = useState('run')

  const tabs = [
    { id: 'run', label: 'Run', icon: Play },
    { id: 'results', label: 'Results', icon: BarChart2 },
    { id: 'calibrate', label: 'Calibrate', icon: FlaskConical },
    { id: 'improve', label: 'Improve', icon: Wand2 },
  ]

  return (
    <div style={{ maxWidth: 720, margin: '0 auto' }}>
      {/* Tab bar */}
      <div style={{ display: 'flex', gap: 4, marginBottom: 20 }}>
        {tabs.map(t => {
          const active = tab === t.id
          return (
            <button key={t.id} onClick={() => setTab(t.id)}
              style={{
                display: 'flex', alignItems: 'center', gap: 6,
                padding: '7px 16px', borderRadius: 8, border: '1px solid',
                cursor: 'pointer', fontSize: 12, fontWeight: 600, transition: 'all 0.12s',
                background: active ? '#c8f13515' : '#0a0a0f',
                color: active ? '#c8f135' : '#5a5a6a',
                borderColor: active ? '#c8f13540' : '#1e1e28',
              }}>
              <t.icon size={12} />
              {t.label}
            </button>
          )
        })}
      </div>

      {/* Tab content */}
      {tab === 'run' && <RunTab providers={providers} selProvider={selProvider} setSelProvider={setSelProvider} />}
      {tab === 'results' && <ResultsTab />}
      {tab === 'calibrate' && <CalibrateTab />}
      {tab === 'improve' && <ImproveTab />}
    </div>
  )
}
