import { useState, useRef, useCallback } from 'react'
import {
  Upload, Play, Square, FileText, Columns, ArrowRight, CheckCircle2, XCircle, AlertTriangle,
} from 'lucide-react'

const STAGES = ['extractor', 'planner', 'executor', 'reviewer']
const STAGE_LABELS = {
  extractor: 'Extract', planner: 'Plan', executor: 'Execute', reviewer: 'Review',
}

// ── one before/after slide card ──────────────────────────────────
function SlideCard({ fileId, slide }) {
  // editable note per edit, keyed by shape — seeded from planner evidence
  const [notes, setNotes] = useState(() =>
    Object.fromEntries(slide.edits.map((e, i) => [`${e.shape}:${i}`, e.evidence || '']))
  )

  const beforeUrl = `/api/diff/before/${fileId}/${slide.slide - 1}`
  const afterUrl = `/api/slides/${fileId}/${slide.slide - 1}`

  return (
    <div className="card p-4" style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      <div className="flex items-center gap-2">
        <span style={{
          fontSize: 11, fontWeight: 800, fontFamily: 'monospace', color: '#000',
          background: '#c8f135', padding: '2px 9px', borderRadius: 6,
        }}>SLIDE {slide.slide}</span>
        <span style={{ fontSize: 11, color: '#5a5a6a' }}>{slide.edits.length} change{slide.edits.length !== 1 ? 's' : ''}</span>
      </div>

      {/* before | after images */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
        {[['Before', beforeUrl, '#5a5a6a'], ['After', afterUrl, '#4ade80']].map(([label, url, color]) => (
          <div key={label}>
            <p style={{ fontSize: 10, fontWeight: 700, textTransform: 'uppercase', letterSpacing: 1, color, marginBottom: 5 }}>{label}</p>
            <img
              src={url} alt={label}
              loading="lazy"
              style={{ width: '100%', borderRadius: 6, border: '1px solid #1e1e28', background: '#0a0a0f', display: 'block' }}
              onError={e => { e.target.style.opacity = 0.25 }}
            />
          </div>
        ))}
      </div>

      {/* per-edit: shape, before→after text, editable meeting note */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
        {slide.edits.map((e, i) => {
          const key = `${e.shape}:${i}`
          return (
            <div key={key} style={{ borderTop: '1px solid #1e1e28', paddingTop: 10 }}>
              <div className="flex items-center gap-2" style={{ marginBottom: 6 }}>
                <span style={{ fontSize: 10, fontFamily: 'monospace', color: '#c8f135' }}>{e.shape}</span>
                <span style={{ fontSize: 9, color: '#3e3e4e', fontFamily: 'monospace' }}>{e.op}</span>
              </div>
              {(e.before_text || e.after_text) && (
                <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8, marginBottom: 8, fontSize: 12 }}>
                  <span style={{ flex: 1, color: '#7a7a8a', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                    {e.before_text || <i style={{ color: '#3e3e4e' }}>(empty)</i>}
                  </span>
                  <ArrowRight size={13} style={{ color: '#5a5a6a', marginTop: 2, flexShrink: 0 }} />
                  <span style={{ flex: 1, color: '#e8e8f0', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                    {e.after_text || <i style={{ color: '#3e3e4e' }}>(empty)</i>}
                  </span>
                </div>
              )}
              <p style={{ fontSize: 9, fontWeight: 700, textTransform: 'uppercase', letterSpacing: 1, color: '#5a5a6a', marginBottom: 4 }}>
                What the meeting said
              </p>
              <textarea
                value={notes[key]}
                onChange={ev => setNotes(n => ({ ...n, [key]: ev.target.value }))}
                rows={2}
                placeholder="What was discussed that drove this change…"
                style={{
                  width: '100%', resize: 'vertical', fontSize: 12, lineHeight: 1.5,
                  background: '#0a0a0f', color: '#c8c8d4', border: '1px solid #1e1e28',
                  borderRadius: 6, padding: '8px 10px', fontFamily: 'inherit',
                }}
              />
            </div>
          )
        })}
      </div>
    </div>
  )
}

// ── main tab ─────────────────────────────────────────────────────
export default function DiffLab({ providers, selProvider, setSelProvider }) {
  const [template, setTemplate] = useState(null)
  const [transcript, setTranscript] = useState('')
  const [running, setRunning] = useState(false)
  const [activeStages, setActiveStages] = useState([])
  const [fileId, setFileId] = useState(null)
  const [slides, setSlides] = useState(null)
  const [verdict, setVerdict] = useState(null)
  const [err, setErr] = useState(null)
  const abortRef = useRef(null)

  const run = useCallback(async () => {
    if (!template || !transcript.trim() || running) return
    setRunning(true); setSlides(null); setFileId(null); setVerdict(null)
    setErr(null); setActiveStages([])

    const fd = new FormData()
    fd.append('template', template)
    fd.append('transcript', transcript)
    if (selProvider) fd.append('provider', selProvider)

    const ctrl = new AbortController()
    abortRef.current = ctrl

    try {
      const res = await fetch('/api/diff/generate', { method: 'POST', body: fd, signal: ctrl.signal })
      if (!res.ok) { setErr(`Server error ${res.status}`); setRunning(false); return }

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
            if (ev.type === 'node_start' && STAGES.includes(ev.node))
              setActiveStages(s => s.includes(ev.node) ? s : [...s, ev.node])
            if (ev.type === 'before_ready') setFileId(ev.file_id)
            if (ev.type === 'reviewer_done') setVerdict(ev.verdict)
            if (ev.type === 'diff_ready') { setFileId(ev.file_id); setSlides(ev.slides) }
            if (ev.type === 'error') { setErr(ev.msg || 'Pipeline error'); setRunning(false) }
            if (ev.type === 'done') setRunning(false)
          } catch {}
        }
      }
    } catch (e) {
      if (e.name !== 'AbortError') setErr(String(e))
    } finally {
      setRunning(false)
    }
  }, [template, transcript, selProvider, running])

  const canRun = template && transcript.trim() && !running

  return (
    <div className="flex flex-col gap-5">
      {/* template upload */}
      <div className="card p-4">
        <p className="text-xs font-semibold uppercase tracking-widest mb-3" style={{ color: '#5a5a6a' }}>PPTX Template</p>
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

      {/* transcript */}
      <div className="card p-4">
        <p className="text-xs font-semibold uppercase tracking-widest mb-3" style={{ color: '#5a5a6a' }}>Meeting Notes / Transcript</p>
        <textarea
          value={transcript}
          onChange={e => setTranscript(e.target.value)}
          rows={6}
          placeholder="Paste the meeting transcript or notes here…"
          style={{
            width: '100%', resize: 'vertical', fontSize: 13, lineHeight: 1.5,
            background: '#0a0a0f', color: '#c8c8d4', border: '1px solid #1e1e28',
            borderRadius: 8, padding: '10px 12px', fontFamily: 'inherit',
          }}
        />
      </div>

      {/* run button — provider is chosen in the header selector above */}
      <button
        onClick={running ? () => { abortRef.current?.abort(); setRunning(false) } : run}
        disabled={!canRun && !running}
        style={{
          width: '100%', padding: '14px', borderRadius: 10, border: 'none',
          cursor: canRun || running ? 'pointer' : 'not-allowed',
          background: running ? '#2e1a1a' : canRun ? 'linear-gradient(135deg, #c8f135, #a3e635)' : '#0a0a0f',
          color: running ? '#f87171' : canRun ? '#000' : '#3e3e4e',
          fontWeight: 700, fontSize: 14, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 8,
        }}>
        {running ? <><Square size={14} /> Stop</> : <><Play size={14} /> Run & Diff</>}
      </button>

      {/* stage chips */}
      {(running || activeStages.length > 0) && (
        <div className="card p-4 flex items-center gap-2 flex-wrap">
          {STAGES.map(s => {
            const on = activeStages.includes(s)
            return (
              <span key={s} style={{
                fontSize: 10, fontWeight: 700, padding: '3px 9px', borderRadius: 6, fontFamily: 'monospace',
                background: on ? '#c8f13518' : '#0a0a0f', color: on ? '#c8f135' : '#3e3e4e',
                border: `1px solid ${on ? '#c8f13530' : '#1e1e28'}`,
              }}>{STAGE_LABELS[s]}</span>
            )
          })}
          {verdict && (
            <span className="flex items-center gap-1" style={{ fontSize: 11, color: verdict === 'PASS' ? '#4ade80' : '#fbbf24', marginLeft: 'auto' }}>
              {verdict === 'PASS' ? <CheckCircle2 size={13} /> : <AlertTriangle size={13} />}{verdict}
            </span>
          )}
        </div>
      )}

      {/* error */}
      {err && (
        <div className="card p-4 flex items-center gap-2" style={{ color: '#f87171', border: '1px solid #ef444430' }}>
          <XCircle size={15} /> <span className="text-sm">{err}</span>
        </div>
      )}

      {/* diff result */}
      {slides && (
        slides.length === 0 ? (
          <div className="card p-6 text-center" style={{ color: '#5a5a6a', fontSize: 13 }}>
            Pipeline produced no slide edits to diff.
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
            <p className="text-xs font-semibold uppercase tracking-widest" style={{ color: '#5a5a6a' }}>
              {slides.length} changed slide{slides.length !== 1 ? 's' : ''}
            </p>
            {slides.map(s => <SlideCard key={s.slide} fileId={fileId} slide={s} />)}
          </div>
        )
      )}

      {/* empty hint */}
      {!slides && !running && !err && (
        <div className="card p-6 text-center" style={{ color: '#3e3e4e', fontSize: 13 }}>
          <Columns size={20} className="mx-auto mb-2" />
          Upload a template + notes, then run to see before / after per slide.
        </div>
      )}
    </div>
  )
}
