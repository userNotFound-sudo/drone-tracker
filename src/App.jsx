
import { useEffect, useMemo, useRef, useState } from 'react'
import { useLocalStorage } from './useLocalStorage'

const LIVE_WS_URL = 'ws://127.0.0.1:8001/ws/tracks'
const REPLAY_WS_BASE = 'ws://127.0.0.1:8001/ws/replay'

function clamp(n, a, b){ return Math.max(a, Math.min(b, n)) }
function fmt(n, d=0){ return (n===null||n===undefined||Number.isNaN(n)) ? '—' : n.toFixed(d) }

function polarToXY(cx, cy, radius, bearingDeg, rangeU){
  const a = (bearingDeg - 90) * Math.PI / 180
  const rr = radius * rangeU
  return { x: cx + rr*Math.cos(a), y: cy + rr*Math.sin(a) }
}

function makeTrack(id, t){
  const base = (id * 37) % 360
  const phase = (t/1000) * (0.10 + (id%7)*0.012)
  const bearing = (base + phase*160) % 360
  const rangeBase = 0.14 + ((id%10)/10)*0.82
  const wobble = Math.sin(phase*2 + id) * 0.07
  const range = clamp(rangeBase + wobble, 0.06, 0.98)

  const altBand = range < 0.35 ? 'LOW' : range < 0.7 ? 'MED' : 'HIGH'
  const type = id%11===0 ? 'multirotor' : id%7===0 ? 'fixedwing' : 'unknown'
  const conf = clamp(0.55 + (Math.sin(phase + id)+1)*0.20, 0.22, 0.98)
  const relSpeed = clamp(9 + Math.cos(phase*1.5 + id)*7, 0, 28)
  const heading = (bearing + 110 + Math.sin(phase + id)*26) % 360

  const flags = []
  if (conf < 0.5) flags.push('LOW_CONF')
  if (id%13===0 && Math.sin(phase*0.9) > 0.72) flags.push('OCCLUDED')
  if (id%29===0 && Math.sin(phase*0.7) > 0.83) flags.push('LOST')

  return {
    id,
    callsign: `UAV-${String(id).padStart(2,'0')}`,
    type,
    bearing,
    range_u: range,
    heading,
    rel_speed_u: relSpeed,
    alt_band: altBand,
    confidence: conf,
    flags,
  }
}

function groupClusters(tracks){
  const buckets = new Map()
  for (const tr of tracks){
    const b = Math.floor(tr.bearing/20)
    const r = Math.floor(tr.range_u/0.2)
    const key = `${b}-${r}`
    buckets.set(key, (buckets.get(key)||0)+1)
  }
  return [...buckets.entries()].map(([k,n])=>({k,n})).sort((a,b)=>b.n-a.n).slice(0,6)
}

function sev(track){
  if (track.flags.includes('LOST')) return 'bad'
  if (track.flags.includes('OCCLUDED')) return 'warn'
  if (track.confidence < 0.55) return 'warn'
  return 'ok'
}

function typeLabel(t){
  if (t==='fixedwing') return 'Fixed-wing'
  if (t==='multirotor') return 'Multirotor'
  return 'Unknown'
}

function nowTS(){
  const d = new Date()
  return d.toISOString().slice(11,19)
}

function toLocalDt(ms) {
  const d = new Date(ms)
  const p = n => String(n).padStart(2,'0')
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`
}

export default function App(){
  const [playing, setPlaying] = useState(true)
  const [speed, setSpeed] = useState(1)
  const [tick, setTick] = useState(0)
  const [selectedId, setSelectedId] = useState(7)
  const [notesById, setNotesById] = useLocalStorage('commander-notes', {})
  const [search, setSearch] = useState('')
  const [alertsOnly, setAlertsOnly] = useState(false)
  const [showVectors, setShowVectors] = useState(true)
  const [rings, setRings] = useState(5)
  const [wsTracks, setWsTracks] = useState(null)
  const [syncedTracks, setSyncedTracks] = useState(null)
  const [mode, setMode] = useState('live')
  const [replayForm, setReplayForm] = useState(() => {
    const now = Date.now()
    return { startDt: toLocalDt(now - 5*60*1000), endDt: toLocalDt(now), rate: '1', hz: '10' }
  })
  const [wsUrl, setWsUrl] = useState(LIVE_WS_URL)
  const [replayStatus, setReplayStatus] = useState('')
  const [zoneEventLog, setZoneEventLog] = useState([])  // rolling zone events from backend
  const [zones, setZones] = useState([])                // zone list for name lookup + radar display
  const [drawZoneMode, setDrawZoneMode] = useState(false)
  const [drawRect, setDrawRect] = useState(null)        // {x1,y1,x2,y2} in SVG viewBox units while dragging

  const timerRef       = useRef(null)
  const videoRef       = useRef(null)
  const canvasRef      = useRef(null)
  const frameBuffer    = useRef(new Map())  // media_t_sec -> tracks[]
  const rafRef         = useRef(null)
  const lastSyncKey    = useRef(null)
  const eventLogEndRef = useRef(null)
  const svgRef         = useRef(null)
  const drawStartRef   = useRef(null)  // SVG coords where drag started
  const drawCurrRef    = useRef(null)  // current drag rect (avoids stale closure in mouseup)

  useEffect(()=>{
    clearInterval(timerRef.current)
    if (!playing) return
    timerRef.current = setInterval(()=> setTick(t=>t+1), 250 / speed)
    return ()=> clearInterval(timerRef.current)
  }, [playing, speed])

  function fetchZones() {
    fetch('http://127.0.0.1:8001/zones')
      .then(r => r.ok ? r.json() : [])
      .then(setZones)
      .catch(()=>{})
  }

  useEffect(()=>{ fetchZones() }, [])

  // auto-scroll event log to newest entry
  useEffect(()=>{
    eventLogEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [zoneEventLog])

  useEffect(()=>{
    const BUFFER_SECS = 60
    setReplayStatus(s => s === '' ? '' : s)  // don't clear error on reconnect
    const ws = new WebSocket(wsUrl)
    ws.onmessage = (event) => {
      let msg
      try { msg = JSON.parse(event.data) } catch { return }
      if (msg?.type === 'tracks_snapshot' && Array.isArray(msg.tracks)) {
        setWsTracks(msg.tracks)
        const t = msg.media_t_sec ?? 0
        frameBuffer.current.set(t, msg.tracks)
        for (const k of frameBuffer.current.keys()) {
          if (k < t - BUFFER_SECS) frameBuffer.current.delete(k)
          else break
        }
      } else if (msg?.type === 'events' && Array.isArray(msg.events)) {
        setZoneEventLog(prev => {
          const next = [...prev, ...msg.events]
          return next.length > 200 ? next.slice(next.length - 200) : next
        })
      } else if (msg?.type === 'done') {
        setReplayStatus(`Done · ${msg.count} frames replayed`)
        setWsTracks(null)
      } else if (msg?.type === 'error') {
        setReplayStatus(`Error: ${msg.detail}`)
        setWsTracks(null)
      }
    }
    ws.onclose = () => setWsTracks(null)
    ws.onerror = () => ws.close()
    return () => {
      ws.onclose = null
      ws.close()
      setWsTracks(null)
    }
  }, [wsUrl])

  // canvas bbox overlay — runs every animation frame
  useEffect(()=>{
    const draw = () => {
      rafRef.current = requestAnimationFrame(draw)
      const video  = videoRef.current
      const canvas = canvasRef.current
      if (!video || !canvas) return

      const dw = canvas.offsetWidth
      const dh = canvas.offsetHeight
      if (canvas.width !== dw || canvas.height !== dh) {
        canvas.width  = dw
        canvas.height = dh
      }

      const ctx = canvas.getContext('2d')
      ctx.clearRect(0, 0, dw, dh)

      const buf = frameBuffer.current
      if (buf.size === 0) return

      // find buffered frame closest to video.currentTime
      const ct = video.currentTime
      let bestKey = null
      let bestDiff = Infinity
      for (const k of buf.keys()) {
        const d = Math.abs(k - ct)
        if (d < bestDiff) { bestDiff = d; bestKey = k }
      }
      if (bestKey === null) return

      // promote to React state only when the frame actually changes
      if (bestKey !== lastSyncKey.current) {
        lastSyncKey.current = bestKey
        setSyncedTracks(buf.get(bestKey))
      }

      const tracks = buf.get(bestKey)
      const srcW = video.videoWidth  || 1280
      const srcH = video.videoHeight || 720
      const scaleX = dw / srcW
      const scaleY = dh / srcH

      ctx.lineWidth   = 1.5
      ctx.font        = '10px monospace'
      ctx.textBaseline = 'top'

      for (const tr of tracks) {
        if (!tr.bbox) continue
        const [x1, y1, x2, y2] = tr.bbox
        const rx = x1 * scaleX
        const ry = y1 * scaleY
        const rw = (x2 - x1) * scaleX
        const rh = (y2 - y1) * scaleY

        const color = tr.flags?.includes('LOST')     ? 'rgba(239,68,68,.9)'
                    : tr.flags?.includes('OCCLUDED') ? 'rgba(245,158,11,.9)'
                    : 'rgba(34,197,94,.9)'

        ctx.strokeStyle = color
        ctx.strokeRect(rx, ry, rw, rh)
        ctx.fillStyle = color
        ctx.fillText(tr.callsign, rx + 2, ry + 2)
      }
    }
    rafRef.current = requestAnimationFrame(draw)
    return () => cancelAnimationFrame(rafRef.current)
  }, [])

  const t = Date.now() + tick*120
  const tracks = useMemo(()=>{
    if (mode === 'live' && syncedTracks) return syncedTracks  // video timeline is authoritative (live only)
    if (wsTracks) return wsTracks
    const list = []
    for (let i=1;i<=72;i++) list.push(makeTrack(i, t))
    return list
  }, [mode, t, syncedTracks, wsTracks])

  const clusters = useMemo(()=>groupClusters(tracks), [tracks])
  const selected = useMemo(()=> tracks.find(x=>x.id===selectedId) || null, [tracks, selectedId])

  const alertCount = useMemo(()=> tracks.filter(x=> x.flags.length>0 || x.confidence<0.55).length, [tracks])

  const filtered = useMemo(()=>{
    let list = tracks
    if (search.trim()){
      const q = search.trim().toLowerCase()
      list = list.filter(x => x.callsign.toLowerCase().includes(q) || typeLabel(x.type).toLowerCase().includes(q))
    }
    if (alertsOnly){
      list = list.filter(x => x.flags.length>0 || x.confidence<0.55)
    }
    return list
  }, [tracks, search, alertsOnly])

  const logRows = useMemo(()=>{
    // generate a few recent events from current state (prototype)
    const rows = []
    rows.push({ ts: nowTS(), msg: `AI stream active • ${tracks.length} tracks • ${alertCount} flagged` })
    if (clusters[0]) rows.push({ ts: nowTS(), msg: `Cohesion: Cluster 1 ~ ${clusters[0].n} tracks` })
    const lost = tracks.filter(x=>x.flags.includes('LOST')).length
    if (lost) rows.push({ ts: nowTS(), msg: `Alert: ${lost} track(s) marked LOST (reacquire required)` })
    const occ = tracks.filter(x=>x.flags.includes('OCCLUDED')).length
    if (occ) rows.push({ ts: nowTS(), msg: `Info: ${occ} track(s) OCCLUDED (line-of-sight / clutter)` })
    rows.push({ ts: nowTS(), msg: `Mode: Relative range units + inferred altitude bands (no telemetry)` })
    return rows
  }, [tracks, clusters, alertCount])

  function zoneName(id) {
    return zones.find(z => z.id === id)?.name ?? `Zone #${id}`
  }

  function switchToLive() {
    frameBuffer.current.clear()
    setSyncedTracks(null)
    lastSyncKey.current = null
    setReplayStatus('')
    setMode('live')
    setWsUrl(LIVE_WS_URL)
  }

  function startReplay() {
    const startMs = new Date(replayForm.startDt).getTime()
    const endMs   = new Date(replayForm.endDt).getTime()
    const rate    = parseFloat(replayForm.rate)
    const hz      = parseFloat(replayForm.hz)
    if (!replayForm.startDt || !replayForm.endDt || isNaN(startMs) || isNaN(endMs) || startMs >= endMs)
      return setReplayStatus('Error: start must be before end')
    if (isNaN(rate) || rate < 0.1 || rate > 10)
      return setReplayStatus('Error: rate must be 0.1 – 10')
    if (isNaN(hz) || hz < 1 || hz > 30)
      return setReplayStatus('Error: hz must be 1 – 30')
    frameBuffer.current.clear()
    setSyncedTracks(null)
    lastSyncKey.current = null
    setReplayStatus('Connecting…')
    setWsUrl(`${REPLAY_WS_BASE}?start_ms=${startMs}&end_ms=${endMs}&rate=${rate}&hz=${hz}`)
  }

  async function exportAAR() {
    const endMs   = Date.now()
    const startMs = endMs - 5 * 60 * 1000   // last 5 minutes
    const BASE    = 'http://127.0.0.1:8001'
    const nowIso  = new Date(endMs).toISOString()

    const [trackRes, zonesRes, eventsRes] = await Promise.allSettled([
      fetch(`${BASE}/replay?start_ms=${startMs}&end_ms=${endMs}&limit=20000`),
      fetch(`${BASE}/zones`),
      fetch(`${BASE}/events?start_ms=${startMs}&end_ms=${endMs}&limit=20000`),
    ])

    async function safeJson(settled, fallback) {
      if (settled.status !== 'fulfilled' || !settled.value.ok) return fallback
      try { return await settled.value.json() } catch { return fallback }
    }

    const [trackData, zonesData, eventsData] = await Promise.all([
      safeJson(trackRes,  { count: 0, rows: [] }),
      safeJson(zonesRes,  []),
      safeJson(eventsRes, { count: 0, events: [] }),
    ])

    const aar = {
      meta: {
        generated_at:       nowIso,
        generated_by:       'JROTC Swarm Tactical Console',
        version:            '1.0',
        window_start_iso:   new Date(startMs).toISOString(),
        window_end_iso:     new Date(endMs).toISOString(),
        window_start_ms:    startMs,
        window_end_ms:      endMs,
        window_duration_s:  Math.round((endMs - startMs) / 1000),
        active_tracks:      tracks.length,
        flagged_tracks:     alertCount,
      },
      tracks: {
        count:   trackData.count,
        samples: trackData.rows,
      },
      zones: zonesData,
      events: {
        count: eventsData.count,
        items: eventsData.events,
      },
      commander_notes: notesById,
    }

    const blob = new Blob([JSON.stringify(aar, null, 2)], { type: 'application/json' })
    const url  = URL.createObjectURL(blob)
    const a    = document.createElement('a')
    a.href     = url
    a.download = `aar-${nowIso.replace(/:/g, '-').replace(/\./g, '-')}.json`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  }

  const rewind = ()=> setTick(v=> Math.max(0, v-12))
  const stepBack = ()=> setTick(v=> Math.max(0, v-1))
  const stepFwd = ()=> setTick(v=> v+1)
  const fastFwd = ()=> setTick(v=> v+12)

  const W=680, H=520
  const cx=W/2, cy=H/2
  const radius=Math.min(W,H)/2 - 28

  // Convert a mouse event to SVG viewBox coordinates
  function svgCoords(e) {
    const svg = svgRef.current
    if (!svg) return { x: 0, y: 0 }
    const r = svg.getBoundingClientRect()
    return { x: (e.clientX - r.left) / r.width * W, y: (e.clientY - r.top) / r.height * H }
  }

  function handleSvgMouseDown(e) {
    if (!drawZoneMode) return
    e.preventDefault()
    const p = svgCoords(e)
    drawStartRef.current = p
    drawCurrRef.current = { x1: p.x, y1: p.y, x2: p.x, y2: p.y }
    setDrawRect(drawCurrRef.current)
  }

  function handleSvgMouseMove(e) {
    if (!drawStartRef.current) return
    const p = svgCoords(e)
    const s = drawStartRef.current
    const r = { x1: Math.min(s.x, p.x), y1: Math.min(s.y, p.y), x2: Math.max(s.x, p.x), y2: Math.max(s.y, p.y) }
    drawCurrRef.current = r
    setDrawRect(r)
  }

  function handleSvgMouseUp(e) {
    if (!drawStartRef.current) return
    drawStartRef.current = null
    const dr = drawCurrRef.current
    drawCurrRef.current = null
    setDrawRect(null)
    if (!dr || dr.x2 - dr.x1 < 8 || dr.y2 - dr.y1 < 8) return

    const coords = [
      [parseFloat((dr.x1 / W).toFixed(4)), parseFloat((dr.y1 / H).toFixed(4))],
      [parseFloat((dr.x2 / W).toFixed(4)), parseFloat((dr.y2 / H).toFixed(4))],
    ]
    const name = window.prompt('Zone name:')
    if (!name || !name.trim()) return

    fetch('http://127.0.0.1:8001/zones', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: name.trim(), shape: 'rectangle', coords }),
    })
      .then(r => { if (r.ok) fetchZones() })
      .catch(() => {})
  }

  const ringEls = []
  for (let i=1;i<=rings;i++){
    ringEls.push(
      <circle key={i} cx={cx} cy={cy} r={(radius*i)/rings} className="ring" />
    )
  }

  return (
    <div className="shell">
      <div className="topbar">
        <div className="brand">
          <div className="logo">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
              <path d="M12 2v4" stroke="rgba(234,242,255,.9)" strokeWidth="2" strokeLinecap="round"/>
              <path d="M4.93 4.93l2.83 2.83" stroke="rgba(234,242,255,.9)" strokeWidth="2" strokeLinecap="round"/>
              <path d="M2 12h4" stroke="rgba(234,242,255,.9)" strokeWidth="2" strokeLinecap="round"/>
              <path d="M4.93 19.07l2.83-2.83" stroke="rgba(234,242,255,.9)" strokeWidth="2" strokeLinecap="round"/>
              <path d="M12 18v4" stroke="rgba(234,242,255,.9)" strokeWidth="2" strokeLinecap="round"/>
              <path d="M19.07 19.07l-2.83-2.83" stroke="rgba(234,242,255,.9)" strokeWidth="2" strokeLinecap="round"/>
              <path d="M18 12h4" stroke="rgba(234,242,255,.9)" strokeWidth="2" strokeLinecap="round"/>
              <path d="M19.07 4.93l-2.83 2.83" stroke="rgba(234,242,255,.9)" strokeWidth="2" strokeLinecap="round"/>
              <circle cx="12" cy="12" r="4" stroke="rgba(34,197,94,.9)" strokeWidth="2"/>
            </svg>
          </div>
          <div>
            <h1>JROTC Swarm Tactical Console</h1>
            <div className="sub">2026 layout • ATAK-inspired, modernized • training mode</div>
          </div>
        </div>

        <div className="pills">
          <div className="pill"><span className="dot" /> AI <b>LIVE</b></div>
          <div className="pill">Tracks <b>{tracks.length}</b></div>
          <div className="pill">Flagged <b>{alertCount}</b></div>
          <div className="pill">Mode <b>REL</b></div>
          <div className="pill">T+ <b>{tick}s</b></div>
        </div>

        <div className="actions">
          <button className="btn" onClick={()=>setShowVectors(v=>!v)}>{showVectors ? 'Vectors: ON' : 'Vectors: OFF'}</button>
          <button className="btn" onClick={()=>setAlertsOnly(v=>!v)}>{alertsOnly ? 'Alerts: ON' : 'Alerts: OFF'}</button>
          <button className="btn primary" onClick={()=>setPlaying(p=>!p)}>{playing ? 'Pause' : 'Play'}</button>
          <button className="btn" onClick={rewind}>⟲ Rewind</button>
          <button className="btn" onClick={fastFwd}>Fast ⟳</button>
          <button className="btn" onClick={exportAAR}>Export AAR</button>
        </div>
      </div>

      <div className="mid">
        {/* LEFT: Video + playback */}
        <div className="panel">
          <div className="panelHeader">
            <div className="panelTitle">
              <div className="t">Video Feed</div>
              <div className="d">Replace placeholder with MP4/HLS + overlay canvas</div>
            </div>
            <div className="kpi"><span>DVIDS • Perdix demo</span></div>
          </div>

          <div className="panelBody">
            <div className="videoBox" style={{ position: 'relative' }}>
              <video
                ref={videoRef}
                src="http://127.0.0.1:8001/video"
                controls
                style={{ width: '100%', height: '100%', objectFit: 'contain', borderRadius: 12 }}
              />
              <canvas
                ref={canvasRef}
                style={{
                  position: 'absolute', top: 0, left: 0,
                  width: '100%', height: '100%',
                  pointerEvents: 'none', borderRadius: 12,
                }}
              />
            </div>
          </div>
        </div>

        {/* CENTER: Radar */}
        <div className="panel">
          <div className="panelHeader">
            <div className="panelTitle">
              <div className="t">Radar / Tactical Picture</div>
              <div className="d">Bearing + relative range • vectors optional • click a track for details</div>
            </div>
            <div className="kpi">
              <span>Rings: {rings}</span>
              <span>Clusters: {clusters.length}</span>
              <span>Selected: {selected ? selected.callsign : '—'}</span>
            </div>
          </div>

          <div className="panelBody" style={{ overflow:'hidden' }}>
            {/* Data source selector */}
            <div style={{ display:'flex', gap:6, alignItems:'center', marginBottom:8, flexWrap:'wrap' }}>
              <button className={`btn${mode==='live'?' primary':''}`} onClick={switchToLive}>LIVE</button>
              <button className={`btn${mode==='replay'?' primary':''}`} onClick={()=>setMode('replay')}>REPLAY</button>
              <button
                className={`btn${drawZoneMode ? ' bad' : ''}`}
                onClick={() => setDrawZoneMode(v => !v)}
                title="Click and drag on the radar to define a zone"
              >
                {drawZoneMode ? '✕ Cancel Draw' : '⬚ Draw Zone'}
              </button>
              {drawZoneMode && <span className="small" style={{ color:'rgba(245,158,11,.9)' }}>Click &amp; drag to draw a rectangle</span>}
              {mode === 'replay' && (<>
                <input
                  type="datetime-local"
                  className="select"
                  style={{ flex:1, minWidth:140 }}
                  value={replayForm.startDt}
                  onChange={e=>setReplayForm(f=>({...f, startDt:e.target.value}))}
                />
                <input
                  type="datetime-local"
                  className="select"
                  style={{ flex:1, minWidth:140 }}
                  value={replayForm.endDt}
                  onChange={e=>setReplayForm(f=>({...f, endDt:e.target.value}))}
                />
                <input
                  type="number"
                  className="select"
                  style={{ width:56 }}
                  placeholder="Rate"
                  min="0.1" max="10" step="0.1"
                  value={replayForm.rate}
                  onChange={e=>setReplayForm(f=>({...f, rate:e.target.value}))}
                />
                <input
                  type="number"
                  className="select"
                  style={{ width:50 }}
                  placeholder="Hz"
                  min="1" max="30"
                  value={replayForm.hz}
                  onChange={e=>setReplayForm(f=>({...f, hz:e.target.value}))}
                />
                <button className="btn primary" onClick={startReplay}>▶</button>
              </>)}
              {replayStatus && <span className="small" style={{ color: replayStatus.startsWith('Error') ? 'rgba(239,68,68,.9)' : undefined }}>{replayStatus}</span>}
            </div>

            <div className="radarWrap">
              <svg
                ref={svgRef}
                className="radarSvg"
                viewBox={`0 0 ${W} ${H}`}
                style={{ cursor: drawZoneMode ? 'crosshair' : 'default' }}
                onMouseDown={handleSvgMouseDown}
                onMouseMove={handleSvgMouseMove}
                onMouseUp={handleSvgMouseUp}
                onMouseLeave={handleSvgMouseUp}
              >
                {/* Base circle + rings */}
                <circle cx={cx} cy={cy} r={radius} stroke="rgba(255,255,255,.22)" fill="none" />
                {ringEls.map((el, idx)=>{
                  return (
                    <circle key={idx} cx={cx} cy={cy} r={(radius*(idx+1))/rings} stroke="rgba(255,255,255,.10)" fill="none" strokeDasharray={idx+1===rings ? "0" : "3 7"} />
                  )
                })}
                <line x1="20" y1={cy} x2={W-20} y2={cy} stroke="rgba(255,255,255,.07)" />
                <line x1={cx} y1="20" x2={cx} y2={H-20} stroke="rgba(255,255,255,.07)" />

                {/* Sweep wedge */}
                <path d={`M ${cx} ${cy} L ${cx} ${cy-radius} A ${radius} ${radius} 0 0 1 ${cx + radius*0.35} ${cy - radius*0.94} Z`}
                      fill="rgba(34,197,94,.10)" />

                {/* Saved zones */}
                {zones.filter(z => z.shape === 'rectangle').map(z => {
                  const [[x1n, y1n], [x2n, y2n]] = z.coords
                  const zx = x1n * W, zy = y1n * H
                  const zw = (x2n - x1n) * W, zh = (y2n - y1n) * H
                  return (
                    <g key={z.id}>
                      <rect x={zx} y={zy} width={zw} height={zh}
                            fill="rgba(56,189,248,.06)"
                            stroke="rgba(56,189,248,.50)"
                            strokeWidth={1.5}
                            strokeDasharray="5 3"
                            rx={3} />
                      <text x={zx + 5} y={zy + 14} fontSize="11"
                            fill="rgba(56,189,248,.85)"
                            style={{ pointerEvents:'none', userSelect:'none' }}>
                        {z.name}
                      </text>
                    </g>
                  )
                })}

                {/* Tracks */}
                {tracks.map(tr=>{
                  const p = polarToXY(cx, cy, radius, tr.bearing, tr.range_u)
                  const isSel = tr.id===selectedId
                  const s = isSel ? 7 : 5
                  const alpha = clamp(tr.confidence, 0.35, 0.95)
                  const vLen = showVectors ? (18 + tr.rel_speed_u*0.7) : 0
                  const v = polarToXY(p.x, p.y, vLen, tr.heading, 1)

                  const color = tr.flags.includes('LOST') ? 'rgba(239,68,68,.95)'
                              : tr.flags.includes('OCCLUDED') ? 'rgba(245,158,11,.95)'
                              : 'rgba(34,197,94,.95)'

                  return (
                    <g key={tr.id}
                       style={{ cursor: drawZoneMode ? 'crosshair' : 'pointer' }}
                       onClick={()=>{ if (!drawZoneMode) setSelectedId(tr.id) }}>
                      {showVectors ? (
                        <line x1={p.x} y1={p.y} x2={v.x} y2={v.y}
                              stroke={isSel ? 'rgba(56,189,248,.95)' : 'rgba(255,255,255,.22)'} strokeWidth={isSel ? 2 : 1} />
                      ) : null}
                      <circle cx={p.x} cy={p.y} r={s} fill={color} opacity={alpha} />
                      <circle cx={p.x} cy={p.y} r={s+12} fill={color} opacity={isSel ? 0.08 : 0.03} />
                      <text x={p.x+10} y={p.y-10} fontSize="11" fill={isSel ? 'rgba(56,189,248,.95)' : 'rgba(159,178,209,.9)'}>
                        {tr.callsign}
                      </text>
                    </g>
                  )
                })}

                {/* Center */}
                <circle cx={cx} cy={cy} r="4" fill="rgba(34,197,94,.95)" />

                {/* Active draw rectangle */}
                {drawRect && (
                  <rect
                    x={drawRect.x1} y={drawRect.y1}
                    width={drawRect.x2 - drawRect.x1} height={drawRect.y2 - drawRect.y1}
                    fill="rgba(245,158,11,.10)"
                    stroke="rgba(245,158,11,.80)"
                    strokeWidth={1.5}
                    strokeDasharray="4 3"
                    rx={3}
                    style={{ pointerEvents:'none' }}
                  />
                )}
              </svg>

              <div className="legendRow">
                <div className="legend"><span className="swatch"></span> Normal</div>
                <div className="legend"><span className="swatch3"></span> Occluded / low conf</div>
                <div className="legend"><span className="swatch2"></span> Selected vector</div>
                <div className="legend">Altitude shown as <b style={{ marginLeft: 6, fontFamily:'var(--mono)' }}>LOW/MED/HIGH</b> (inferred)</div>
              </div>

              <div style={{ display:'flex', gap:10, marginTop:10, width:'100%', alignItems:'center', justifyContent:'space-between' }}>
                <div className="small">Display</div>
                <div style={{ display:'flex', gap:8, alignItems:'center' }}>
                  <button className="btn" onClick={()=>setRings(r=>clamp(r-1,3,7))}>- Ring</button>
                  <button className="btn" onClick={()=>setRings(r=>clamp(r+1,3,7))}>+ Ring</button>
                </div>
              </div>
            </div>
          </div>
        </div>

        {/* RIGHT: Inspector + table */}
        <div className="panel">
          <div className="panelHeader">
            <div className="panelTitle">
              <div className="t">Track Inspector</div>
              <div className="d">Commander-ready detail + notes per track</div>
            </div>
            <div className="kpi"><span>Integrity: ON</span></div>
          </div>

          <div className="panelBody">
            <div style={{ display:'flex', gap:8, alignItems:'center' }}>
              <input
                className="select"
                style={{ flex:1 }}
                placeholder="Search callsign or type…"
                value={search}
                onChange={(e)=>setSearch(e.target.value)}
              />
              <button className="btn" onClick={()=>setAlertsOnly(v=>!v)}>{alertsOnly ? 'Alerts only' : 'All tracks'}</button>
            </div>

            <div style={{ marginTop: 10 }}>
              {selected ? (
                <>
                  <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between', gap:10 }}>
                    <div style={{ fontSize:16, fontWeight:750 }}>{selected.callsign}</div>
                    <span className={`badge ${sev(selected)}`}>
                      <span className="m">{typeLabel(selected.type)}</span>
                    </span>
                  </div>

                  <div className="inspectorGrid" style={{ marginTop: 10 }}>
                    <div className="cardMini"><div className="k">Bearing</div><div className="v">{fmt(selected.bearing,0)}°</div></div>
                    <div className="cardMini"><div className="k">Range</div><div className="v">{fmt(selected.range_u,2)} u</div></div>
                    <div className="cardMini"><div className="k">Heading</div><div className="v">{fmt(selected.heading,0)}°</div></div>
                    <div className="cardMini"><div className="k">Rel Speed</div><div className="v">{fmt(selected.rel_speed_u,0)} u/s</div></div>
                    <div className="cardMini"><div className="k">Altitude Band</div><div className="v">{selected.alt_band}</div></div>
                    <div className="cardMini"><div className="k">Confidence</div><div className="v">{fmt(selected.confidence,2)}</div></div>
                  </div>

                  <div className="notes">
                    <div style={{ display:'flex', justifyContent:'space-between', alignItems:'center' }}>
                      <div style={{ fontSize:12, fontWeight:700 }}>Commander Notes</div>
                      <div className="small">Saved locally (prototype)</div>
                    </div>
                    <div style={{ marginTop: 8 }}>
                      <textarea
                        value={notesById[selected.id] || ''}
                        onChange={(e)=> setNotesById(n => ({...n, [selected.id]: e.target.value}))}
                        placeholder="Observations, anomalies, cluster notes, confidence issues, cadet tasking…"
                      />
                    </div>
                  </div>
                </>
              ) : (
                <div className="small">Select a track on the radar or table.</div>
              )}
            </div>

            <div style={{ marginTop: 12, display:'flex', justifyContent:'space-between', alignItems:'center' }}>
              <div style={{ fontSize:12, fontWeight:750 }}>Track List</div>
              <div className="small">{filtered.length} shown</div>
            </div>

            <div style={{ marginTop: 8, maxHeight: 260, overflow:'auto', borderRadius: 18, border:'1px solid rgba(255,255,255,.10)', background:'rgba(0,0,0,.10)' }}>
              <table className="table">
                <thead>
                  <tr>
                    <th>Callsign</th>
                    <th>Type</th>
                    <th>Conf</th>
                    <th>Bear</th>
                    <th>Alt</th>
                  </tr>
                </thead>
                <tbody>
                  {filtered.map(tr=>{
                    const isSel = tr.id===selectedId
                    const bClass = sev(tr)
                    return (
                      <tr key={tr.id} onClick={()=>setSelectedId(tr.id)} style={{ background: isSel ? 'rgba(56,189,248,.08)' : undefined }}>
                        <td style={{ fontWeight:650 }}>{tr.callsign}</td>
                        <td><span className="badge"><span className="m">{typeLabel(tr.type)}</span></span></td>
                        <td><span className={`badge ${bClass}`}><span className="m">{fmt(tr.confidence,2)}</span></span></td>
                        <td style={{ fontFamily:'var(--mono)' }}>{fmt(tr.bearing,0)}°</td>
                        <td><span className="badge">{tr.alt_band}</span></td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>

          </div>
        </div>
      </div>

      {/* Zone Event Log */}
      <div className="eventSection">
        <div className="eventSectionHeader">
          <div className="title">Zone Event Log</div>
          <div style={{ display:'flex', gap:10, alignItems:'center' }}>
            <span className="small">{zoneEventLog.length} event{zoneEventLog.length !== 1 ? 's' : ''}</span>
            {zoneEventLog.length > 0 && (
              <button className="btn" style={{ padding:'3px 10px', fontSize:11 }}
                onClick={() => setZoneEventLog([])}>Clear</button>
            )}
          </div>
        </div>

        <div className="eventTable">
          {zoneEventLog.length === 0 ? (
            <div className="emptyLog">No zone events yet — define a zone via POST /zones to begin tracking.</div>
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th>Time (UTC)</th>
                  <th>Track</th>
                  <th>Zone</th>
                  <th>Event</th>
                  <th>Details</th>
                </tr>
              </thead>
              <tbody>
                {zoneEventLog.map((e, idx) => {
                  const evtClass = e.event_type === 'ENTER' ? 'ok'
                                 : e.event_type === 'EXIT'  ? 'warn'
                                 : 'info'
                  const rowClass = e.event_type === 'ENTER' ? 'evtEnter'
                                 : e.event_type === 'EXIT'  ? 'evtExit'
                                 : 'evtDwell'
                  const detail   = e.event_type === 'ENTER' ? 'entered boundary'
                                 : e.event_type === 'EXIT'  ? 'departed boundary'
                                 : '≥55 s continuous presence'
                  return (
                    <tr key={idx} className={rowClass}>
                      <td style={{ fontFamily:'var(--mono)', whiteSpace:'nowrap' }}>
                        {new Date(e.ts_ms).toISOString().slice(11, 19)}Z
                      </td>
                      <td style={{ fontFamily:'var(--mono)' }}>
                        UAV-{String(e.track_id).padStart(2, '0')}
                      </td>
                      <td>{zoneName(e.zone_id)}</td>
                      <td>
                        <span className={`badge ${evtClass}`}>
                          <span className="m">{e.event_type}</span>
                        </span>
                      </td>
                      <td className="small">{detail}</td>
                    </tr>
                  )
                })}
                <tr ref={eventLogEndRef} />
              </tbody>
            </table>
          )}
        </div>
      </div>

      {/* Bottom strip — operational snapshot */}
      <div className="bottom">
        <div className="log">
          {logRows.map((r, idx)=> (
            <div key={idx} className="logRow">
              <div className="ts">{r.ts}</div>
              <div className="msg">{r.msg}</div>
            </div>
          ))}
        </div>

        <div className="rightMini">
          <div style={{ display:'flex', justifyContent:'space-between', alignItems:'center', marginBottom:8 }}>
            <div style={{ fontSize:12, fontWeight:800 }}>Operational Snapshot</div>
            <div className="small">Training mode</div>
          </div>
          <div className="miniKpis">
            <div className="k"><div className="l">Tracks</div><div className="n">{tracks.length}</div></div>
            <div className="k"><div className="l">Flagged</div><div className="n">{alertCount}</div></div>
            <div className="k"><div className="l">Clusters</div><div className="n">{clusters.length}</div></div>
          </div>
        </div>
      </div>
    </div>
  )
}
