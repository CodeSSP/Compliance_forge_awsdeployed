import React from 'react'
import { S } from './styleStr'
import { Hover } from './Hover'
import { TransactionTable } from './TransactionTable'

// ============================================================================
// React (Vite) port of the Compliance Pipeline regulatory UI.
//
// The class logic below is a faithful copy of the original design-export
// component (regulatory-ui/Compliance Pipeline.dc.html): same state machine,
// same live backend wiring (SSE stream from api.py), same typology-graph SVGs
// (built with React.createElement), same sample data. Only the presentation
// layer changed — the DC template (sc-if / sc-for / {{ }}) is now real JSX in
// render(), and inline-style strings are parsed to style objects via S().
// ============================================================================

export default class App extends React.Component {
  state = {
    view: 'overview', sample: 0, step: 1,
    dstate: {}, graphShow: false, fanIn: false, verdictShow: false,
    demoStop: 0, ingestCount: 0, ingestDone: false, queueArrived: 0, orchShow: false,
    reportProgress: 0, filed: false, reportModal: false,
    statT: 0, openCat: -1, focusArea: null, samplePage: 0,
    userTxns: [], uploadName: '', lastScraped: this.nowStr(),
    circModal: false, circLane: null, explainOpen: false
  }

  fileRef = React.createRef()

  get speed() { return this.props.speed ?? 1 }

  timers = []
  after(ms, fn) { const t = setTimeout(fn, ms / this.speed); this.timers.push(t); return t }
  clearTimers() { this.timers.forEach(clearTimeout); this.timers = [] }
  componentWillUnmount() { this.clearTimers() }

  nowStr() {
    const d = new Date()
    const date = d.toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: 'numeric' })
    const time = d.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })
    return `${date} · ${time}`
  }

  addTxn = () => { if (this.fileRef.current) this.fileRef.current.click() }

  onFile = (e) => {
    const file = e.target.files && e.target.files[0]
    if (!file) { return }
    const reader = new FileReader()
    reader.onload = () => {
      const rows = this.parseCsv(String(reader.result || ''))
      if (rows.length) {
        this.setState(st => {
          const userTxns = st.userTxns.concat(rows)
          const newIdx = this.SAMPLES.length + userTxns.length - 1
          return { userTxns, uploadName: file.name, lastScraped: this.nowStr(), sample: newIdx, samplePage: Math.floor(newIdx / 10) }
        }, () => { if (this.state.step === 1) this.goStep(1) })
      }
    }
    reader.readAsText(file)
    e.target.value = ''
  }

  parseDelimitedRows(text) {
    const rows = []
    let currentField = ''
    let currentRow = []
    let inQuotes = false
    for (let i = 0; i < text.length; i++) {
      const ch = text[i]
      if (inQuotes) {
        if (ch === '"') {
          if (text[i + 1] === '"') {
            currentField += '"'
            i += 1
          } else {
            inQuotes = false
          }
        } else {
          currentField += ch
        }
        continue
      }
      if (ch === '"') {
        inQuotes = true
      } else if (ch === ',') {
        currentRow.push(currentField)
        currentField = ''
      } else if (ch === '\n') {
        currentRow.push(currentField)
        rows.push(currentRow)
        currentRow = []
        currentField = ''
      } else if (ch !== '\r') {
        currentField += ch
      }
    }
    currentRow.push(currentField)
    rows.push(currentRow)
    return rows.map(row => row.map(cell => cell.trim()))
  }

  parseCsv(text) {
    const cols = ['tx_id', 'timestamp', 'channel', 'amount_inr', 'sender_account_id', 'sender_name', 'receiver_name', 'purpose_code']
    const lines = this.parseDelimitedRows(text).filter(row => row.some(cell => cell !== ''))
    if (!lines.length) { return [] }
    let start = 0, header = null
    if (lines[0].some(cell => /tx_id/i.test(cell))) { header = lines[0].map(s => s.trim()); start = 1 }
    const out = []
    for (let i = start; i < lines.length; i++) {
      const parts = lines[i]
      const record = {}
      if (header) {
        header.forEach((name, idx) => { if (name) { record[name.trim()] = parts[idx] || '' } })
      } else {
        cols.forEach((name, idx) => { record[name] = parts[idx] || '' })
      }
      const get = (k) => record[k] || ''
      const amtNum = Number(String(get('amount_inr') || get('amount') || '').replace(/[^0-9.]/g, '')) || 0
      const amt = '₹' + amtNum.toLocaleString('en-IN') + '.00'
      const acct = get('sender_account_id') || get('sender_account') || ''
      const maskAcct = acct.length > 4 ? 'AC****' + acct.slice(-4) : (acct || '—')
      const ch = get('channel') || '—'
      out.push({
        uploaded: true,
        name: (get('sender_name') || 'Uploaded').split(' ')[0],
        tag: amt.replace('.00', '') + ' · ' + ch,
        amount: amt, sender: get('sender_name') || '—', receiver: get('receiver_name') || '—',
        channel: ch + ' transfer', reference: get('tx_id') || ('TX' + Date.now()),
        record,
        fields: [['tx_id', get('tx_id') || '—'], ['timestamp', get('timestamp') || '—'], ['channel', ch], ['amount_inr', amt.replace('.00', '')], ['sender_account_id', maskAcct], ['sender_name', get('sender_name') || '—'], ['receiver_name', get('receiver_name') || '—'], ['purpose_code', get('purpose_code') || '—']],
        results: { sanctions: { v: 'clear', note: 'No name on any watch-list' }, device: { v: 'clear', note: 'Uploaded record' }, split: { v: 'clear', note: 'Single payment' }, behaviour: { v: 'clear', note: 'From uploaded file' }, crossborder: { v: 'clear', note: 'Within limits' }, mule: { v: 'clear', note: 'Direct transfer' } },
        verdict: { tone: 'clear', title: 'AUTO-CLEARED', line: 'Uploaded transaction — no flags raised.' },
        graphType: '_live'
      })
    }
    return out
  }

  // ---------- live backend integration ----------
  BACKEND = (typeof window !== 'undefined' && window.PIPELINE_API) || 'http://localhost:8000'

  txPayload(sample) {
    const f = {}; (sample.fields || []).forEach(([k, v]) => { f[k] = v })
    const r = sample.record || {}
    const read = (...keys) => {
      for (const key of keys) {
        const value = r[key] ?? f[key] ?? sample[key]
        if (value !== undefined && value !== null && value !== '') return value
      }
      return ''
    }
    const amt = Number(String(read('amount_inr', 'amount')).replace(/[^0-9.]/g, '')) || 0
    const senderAccount = String(read('sender_account_id')).replace(/\*/g, 'X')
    const receiverAccountExternal = String(read('receiver_account_external', 'receiver_account_id'))
    return {
      tx_id: read('tx_id') || sample.reference || ('TX' + Date.now()),
      timestamp: read('timestamp'),
      channel: String(read('channel', sample.channel || 'UPI')).split(' ')[0].toUpperCase(),
      amount_inr: amt,
      sender_account_id: senderAccount,
      sender_name: read('sender_name') || sample.sender || '',
      sender_bank: read('sender_bank'),
      sender_ifsc: read('sender_ifsc'),
      sender_vpa: read('sender_vpa'),
      sender_pan: read('sender_pan'),
      sender_dob: read('sender_dob'),
      receiver_name: read('receiver_name') || sample.receiver || '',
      receiver_account_external: receiverAccountExternal || ('EXT' + (String(read('tx_id') || '').slice(-4) || '0000')),
      receiver_account_id: read('receiver_account_id') || receiverAccountExternal,
      receiver_bank: read('receiver_bank'),
      receiver_pan: read('receiver_pan'),
      receiver_dob: read('receiver_dob'),
      receiver_state: read('receiver_state'),
      receiver_city: read('receiver_city'),
      tx_location_state: read('tx_location_state'),
      tx_location_city: read('tx_location_city'),
      tx_location_country: read('tx_location_country'),
      tx_location_lat: read('tx_location_lat'),
      tx_location_lon: read('tx_location_lon'),
      purpose_code: read('purpose_code'),
      device_id: read('device_id'),
      tx_status: read('tx_status'),
      is_cross_border: read('is_cross_border'),
      usd_equiv: read('usd_equiv'),
      fx_usd_inr: read('fx_usd_inr'),
      beneficiary_id: read('beneficiary_id') || receiverAccountExternal
    }
  }

  DET_MATCH = [
    ['structuring', ['structuring', 'smurf']],
    ['sanctions', ['watchlist', 'sanction', 'alias', 'pep']],
    ['mule', ['mule', 'fan-in', 'fanin', 'sweep', 'round-trip', 'roundtrip', 'layering']],
    ['velocity', ['velocity', 'high value', 'credit', 'dormant', 'new account', 'behaviour', 'behavior']],
    ['fema', ['lrs', 'fema', 'ceiling', 'beneficiary split', 'gift', 'cross']],
    ['geo', ['geo', 'device', 'travel', 'jurisdiction', 'fatf', 'takeover', 'location']]
  ]
  detFor(label) {
    const l = String(label).toLowerCase()
    for (const [id, keys] of this.DET_MATCH) { if (keys.some(k => l.includes(k))) return id }
    return null
  }

  mapResult(result) {
    const results = {}
    this.DET.forEach(d => { results[d.id] = { v: 'clear', note: 'No flag raised' } })
    const fired = (result.l2_checks_fired || []).filter(c => c.result === 'fail')
    let firstId = null
    fired.forEach(c => { const id = this.detFor(c.label); if (id) { results[id] = { v: 'hit', note: c.label }; if (!firstId) firstId = id } })
    const v = result.verdict
    let verdict
    if (v === 'clean' || v === 'dismissed') verdict = { tone: 'clear', title: 'AUTO-CLEARED', line: result.verdict_detail || 'No typology or watchlist match.' }
    else if (v === 'str_filed') verdict = { tone: 'hit', title: 'ESCALATE — File STR', line: result.verdict_detail || 'Reportable to FIU-IND under PMLA, 2002.' }
    else if (v === 'human_review') verdict = { tone: 'flag', title: 'HOLD — Human review', line: result.verdict_detail || 'Queued for compliance review.' }
    else verdict = { tone: 'hit', title: 'ESCALATE — Priority', line: result.verdict_detail || 'Escalated to a compliance officer.' }
    // short-circuit: reportable verdict reached (e.g. L1 MinHash match) without L2 checks firing.
    // In that case there are no fresh agent flags, so the caller should keep the authored red results.
    const shortCircuited = fired.length === 0 && verdict.tone !== 'clear'
    return { results, verdict: { ...verdict, confidence: result.confidence || result.composite_score }, clearGraph: verdict.tone === 'clear', shortCircuited }
  }

  streamPipeline(payload) {
    return new Promise((resolve, reject) => {
      fetch(this.BACKEND + '/api/transactions/stream', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
      }).then(res => {
        if (!res.ok || !res.body) throw new Error('stream ' + res.status)
        const reader = res.body.getReader(); const dec = new TextDecoder(); let buf = ''
        const pump = () => reader.read().then(({ done, value }) => {
          if (done) { resolve(null); return }
          buf += dec.decode(value, { stream: true })
          const parts = buf.split('\n\n'); buf = parts.pop() || ''
          for (const chunk of parts) {
            const line = chunk.split('\n').find(l => l.startsWith('data:'))
            if (!line) continue
            try {
              const msg = JSON.parse(line.slice(5).trim())
              if (msg.type === 'result') { resolve(msg.result); return }
              if (msg.type === 'error') { reject(new Error(msg.message)); return }
            } catch (e) { /* skip partial chunk */ }
          }
          pump()
        }).catch(reject)
        pump()
      }).catch(reject)
    })
  }

  fetchReal(idx) {
    const sample = this.allSamples[idx]
    if (!sample) return Promise.resolve()
    if (sample._fetchPromise) return sample._fetchPromise
    
    sample._fetchPromise = this.streamPipeline(this.txPayload(sample)).then(result => {
      if (!result) return
      const m = this.mapResult(result)
      // On a short-circuited reportable verdict the live path fires no agent checks; keep the
      // authored red results so the agents still show their hit/flag states (don't zero them out).
      if (!m.shortCircuited) sample.results = m.results
      sample.verdict = m.verdict
      sample.liveCites = result.regulatory_basis || []
      sample.strPdfUrl = result.str_pdf_url || null
      if (m.clearGraph) sample.graphType = null
      sample.live = true
    }).catch(() => { /* backend unreachable — keep the built-in demo data */ })
    
    return sample._fetchPromise
  }

  // ---------- data ----------
  DET = [
    { id: 'structuring', name: 'Structuring detection', q: 'Is a high-value transfer broken into sub-threshold tranches to evade CTR reporting?' },
    { id: 'sanctions', name: 'Sanctions & PEP screening', q: 'Does any counterparty match a sanctions, PEP or adverse-media watchlist?' },
    { id: 'mule', name: 'Mule-account layering', q: 'Is value layered through pass-through mule accounts to obscure beneficial ownership?' },
    { id: 'velocity', name: 'Velocity & behaviour', q: 'Does transaction velocity deviate sharply from the account\'s behavioural baseline?' },
    { id: 'fema', name: 'FEMA / LRS threshold', q: 'Does cumulative outward remittance breach the FEMA / LRS ceiling?' },
    { id: 'geo', name: 'Geolocation anomaly', q: 'Is the session geo or device inconsistent — indicative of impossible travel?' }
  ]

  SAMPLES = [
    { name: 'Mule-account layering', tag: '₹42,00,000 · RTGS', amount: '₹42,00,000.00', sender: 'Anita Desai', receiver: 'Global Trade Ltd', channel: 'RTGS wire', reference: 'TX20260512000541',
      raw: 'TX20260512000541,2026-05-12 14:22:07,RTGS,4200000,ACXXXX4471,Anita Desai,Global Trade Ltd,P0103',
      fields: [['tx_id', 'TX20260512000541'], ['timestamp', '2026-05-12 14:22:07'], ['channel', 'RTGS'], ['amount_inr', '₹42,00,000'], ['sender_account_id', 'AC****4471'], ['sender_name', 'Anita Desai'], ['receiver_name', 'Global Trade Ltd'], ['purpose_code', 'P0103']],
      results: { sanctions: { v: 'clear', note: 'No sanctions / PEP match' }, geo: { v: 'clear', note: 'Enrolled device, home geo' }, structuring: { v: 'flag', note: 'Value split across 3 tranches' }, velocity: { v: 'flag', note: 'Above 30-day baseline' }, fema: { v: 'flag', note: 'Approaching LRS ceiling' }, mule: { v: 'hit', note: 'Layered via 3 pass-through mules' } },
      verdict: { tone: 'hit', title: 'ESCALATE — File STR', line: 'Mule-account layering network identified. Reportable to FIU-IND under PMLA, 2002.' },
      graphType: 'mule', graphMeta: { title: 'Mule-account layering network', tag: 'placement → layering → integration', trigger: 'Mule-account layering (fan-out / fan-in)', desc: 'A mule account is a third-party account used to obscure beneficial ownership. The ₹42,00,000 is placed, split across three mule accounts (fan-out), layered through pass-through nodes, then reintegrated at the beneficiary (fan-in). This fan-out / fan-in topology is the typology signature.' } },
    { name: 'Structuring', tag: '₹9,49,000 · IMPS', amount: '₹9,49,000.00', sender: 'Vikram Nair', receiver: 'QuickCash Traders', channel: 'IMPS transfer', reference: 'TX20260514000771',
      raw: 'TX20260514000771,2026-05-14 11:07:52,IMPS,949000,ACXXXX2288,Vikram Nair,QuickCash Traders,P0106',
      fields: [['tx_id', 'TX20260514000771'], ['timestamp', '2026-05-14 11:07:52'], ['channel', 'IMPS'], ['amount_inr', '₹9,49,000'], ['sender_account_id', 'AC****2288'], ['sender_name', 'Vikram Nair'], ['receiver_name', 'QuickCash Traders'], ['purpose_code', 'P0106']],
      results: { sanctions: { v: 'clear', note: 'No sanctions / PEP match' }, geo: { v: 'clear', note: 'Consistent device & geo' }, mule: { v: 'flag', note: 'Counterparty in prior alerts' }, velocity: { v: 'flag', note: '6 tranches in 48h' }, fema: { v: 'clear', note: 'Domestic; no LRS impact' }, structuring: { v: 'hit', note: '6 tranches under ₹10L CTR' } },
      verdict: { tone: 'hit', title: 'ESCALATE — File STR', line: 'Structuring below the ₹10,00,000 CTR threshold detected. Reportable to FIU-IND.' },
      graphType: 'structuring', graphMeta: { title: 'Structuring — sub-threshold tranches', tag: 'smurfing · CTR evasion', trigger: 'Structuring below CTR threshold', desc: 'Structuring (smurfing) breaks a large value into multiple tranches kept just below the ₹10,00,000 Cash Transaction Report threshold to evade mandatory reporting. Six correlated tranches totalling ₹56,70,000 cleared inside 48 hours, each engineered to sit under the ceiling.' } },
    { name: 'Velocity spike', tag: '₹6,80,000 · UPI', amount: '₹6,80,000.00', sender: 'Meera Kapoor', receiver: '12 first-time payees', channel: 'UPI', reference: 'TX20260514000903',
      raw: 'TX20260514000903,2026-05-14 15:41:19,UPI,680000,ACXXXX5510,Meera Kapoor,Multiple payees,P0108',
      fields: [['tx_id', 'TX20260514000903'], ['timestamp', '2026-05-14 15:41:19'], ['channel', 'UPI'], ['amount_inr', '₹6,80,000'], ['sender_account_id', 'AC****5510'], ['sender_name', 'Meera Kapoor'], ['receiver_name', 'Multiple payees'], ['purpose_code', 'P0108']],
      results: { sanctions: { v: 'clear', note: 'No sanctions / PEP match' }, geo: { v: 'clear', note: 'Single device' }, structuring: { v: 'flag', note: 'Many small-value debits' }, mule: { v: 'flag', note: 'Several first-time payees' }, fema: { v: 'clear', note: 'Domestic UPI' }, velocity: { v: 'hit', note: '38 debits in 4 min vs ~2/day' } },
      verdict: { tone: 'hit', title: 'ESCALATE — EDD', line: 'Transaction velocity breaches behavioural baseline. Escalated for Enhanced Due Diligence.' },
      graphType: 'velocity', graphMeta: { title: 'Transaction-velocity anomaly', tag: 'behavioural analytics', trigger: 'Velocity deviation from baseline', desc: 'Behavioural analytics profiles each account\'s baseline throughput. This account fired 38 debits across 12 first-time payees inside a 4-minute window against a ~2-transactions/day baseline — a velocity spike consistent with account compromise or a bust-out pattern.' } },
    { name: 'Geolocation anomaly', tag: '₹2,15,000 · Card (CNP)', amount: '₹2,15,000.00', sender: 'Arjun Rao', receiver: 'Horizon Digital', channel: 'Card (CNP)', reference: 'TX20260514001042',
      raw: 'TX20260514001042,2026-05-14 14:31:12,CARD,215000,ACXXXX7734,Arjun Rao,Horizon Digital,P0107',
      fields: [['tx_id', 'TX20260514001042'], ['timestamp', '2026-05-14 14:31:12'], ['channel', 'CARD-CNP'], ['amount_inr', '₹2,15,000'], ['sender_account_id', 'AC****7734'], ['sender_name', 'Arjun Rao'], ['receiver_name', 'Horizon Digital'], ['purpose_code', 'P0107']],
      results: { sanctions: { v: 'clear', note: 'No sanctions / PEP match' }, structuring: { v: 'clear', note: 'Single authorisation' }, mule: { v: 'clear', note: 'Direct merchant settlement' }, velocity: { v: 'flag', note: '2 geos in one session' }, fema: { v: 'flag', note: 'Foreign acquirer' }, geo: { v: 'hit', note: 'Impossible travel: IN → NG in 9 min' } },
      verdict: { tone: 'hit', title: 'BLOCK — Step-up auth', line: 'Impossible-travel geolocation anomaly. Session blocked pending step-up authentication.' },
      graphType: 'geo', graphMeta: { title: 'Geolocation anomaly — impossible travel', tag: 'device & session risk', trigger: 'Impossible-travel geolocation anomaly', desc: 'Two authenticated sessions on the same credential originated ~11,000 km apart within 9 minutes — a physically impossible transit. This impossible-travel signal is a strong indicator of account takeover or credential compromise.' } },
    { name: 'FEMA / LRS breach', tag: '₹1,20,00,000 · SWIFT', amount: '₹1,20,00,000.00', sender: 'Omni Exports', receiver: 'Zenith FZE, Dubai', channel: 'SWIFT wire', reference: 'TX20260512000902',
      raw: 'TX20260512000902,2026-05-12 17:41:12,SWIFT,12000000,ACXXXX3310,Omni Exports,Zenith FZE Dubai,P0803',
      fields: [['tx_id', 'TX20260512000902'], ['timestamp', '2026-05-12 17:41:12'], ['channel', 'SWIFT'], ['amount_inr', '₹1,20,00,000'], ['sender_account_id', 'AC****3310'], ['sender_name', 'Omni Exports'], ['receiver_name', 'Zenith FZE, Dubai'], ['purpose_code', 'P0803']],
      results: { structuring: { v: 'clear', note: 'Single wire instruction' }, mule: { v: 'clear', note: 'Direct to beneficiary' }, geo: { v: 'clear', note: 'Corporate device, Mumbai' }, velocity: { v: 'flag', note: 'Largest wire this FY' }, sanctions: { v: 'flag', note: 'Beneficiary adverse-media hit' }, fema: { v: 'hit', note: 'Cumulative LRS > USD 250k' } },
      verdict: { tone: 'hit', title: 'BLOCK — FEMA breach', line: 'Outward remittance exceeds the LRS ceiling. Blocked; reportable to RBI under FEMA, 1999.' },
      graphType: 'fema', graphMeta: { title: 'FEMA / LRS outward-remittance breach', tag: 'cross-border · Schedule III', trigger: 'FEMA / LRS ceiling breach', desc: 'The Liberalised Remittance Scheme caps outward remittance at USD 250,000 per financial year. Prior remittances of USD 210,000 plus this USD 58,000 wire push the cumulative to USD 268,000 — a ceiling breach requiring AD-bank escalation to RBI.' } },
    { name: 'Clean payroll', tag: '₹85,000 · NEFT', amount: '₹85,000.00', sender: 'Rahul Verma', receiver: 'Sunrise Apartments', channel: 'NEFT transfer', reference: 'TX20260512000188',
      raw: 'TX20260512000188,2026-05-12 09:04:33,NEFT,85000,ACXXXX9920,Rahul Verma,Sunrise Apartments,P1301',
      fields: [['tx_id', 'TX20260512000188'], ['timestamp', '2026-05-12 09:04:33'], ['channel', 'NEFT'], ['amount_inr', '₹85,000'], ['sender_account_id', 'AC****9920'], ['sender_name', 'Rahul Verma'], ['receiver_name', 'Sunrise Apartments'], ['purpose_code', 'P1301']],
      results: { sanctions: { v: 'clear', note: 'No sanctions / PEP match' }, geo: { v: 'clear', note: 'Enrolled device & home geo' }, structuring: { v: 'clear', note: 'Single full-value payment' }, velocity: { v: 'clear', note: 'Consistent with baseline' }, fema: { v: 'clear', note: 'Domestic; no LRS impact' }, mule: { v: 'clear', note: 'Direct to counterparty' } },
      verdict: { tone: 'clear', title: 'AUTO-CLEARED', line: 'No typology or watchlist match. Straight-through processed.' },
      graphType: null }
  ]

  LANES = [
    { code: 'RBI', name: 'Reserve Bank of India', color: '#2f7fd6', tint: 'rgba(47,127,214,.1)',
      circulars: [
        { title: 'Master Direction – KYC (updated)', url: 'https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx' },
        { title: 'Digital Payment Security Controls', url: 'https://www.rbi.org.in/Scripts/BS_CircularIndexDisplay.aspx' },
        { title: 'Regulation of Payment Aggregators', url: 'https://www.rbi.org.in/Scripts/BS_CircularIndexDisplay.aspx' },
        { title: 'FEMA – Liberalised Remittance Scheme', url: 'https://www.rbi.org.in/Scripts/BS_FemaNotifications.aspx' }
      ] },
    { code: 'SEBI', name: 'Securities & Exchange Board of India', color: '#cf861b', tint: 'rgba(207,134,27,.12)',
      circulars: [
        { title: 'Prohibition of Insider Trading Regulations', url: 'https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&sid=1&ssid=6&smid=0' },
        { title: 'LODR – Listing Obligations & Disclosure', url: 'https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&sid=1&ssid=6&smid=0' },
        { title: 'BRSR – Sustainability Reporting', url: 'https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&sid=1&ssid=6&smid=0' },
        { title: 'Master Circular for Mutual Funds', url: 'https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&sid=1&ssid=6&smid=0' }
      ] },
    { code: 'IRDAI', name: 'Insurance Regulatory & Development Authority', color: '#1f9d63', tint: 'rgba(31,157,99,.1)',
      circulars: [
        { title: 'Master Circular on Corporate Governance', url: 'https://irdai.gov.in/circulars' },
        { title: 'AML / CFT Guidelines for Insurers', url: 'https://irdai.gov.in/circulars' },
        { title: 'Health Insurance – Master Regulations', url: 'https://irdai.gov.in/circulars' },
        { title: 'Expenses of Management Regulations', url: 'https://irdai.gov.in/circulars' }
      ] }
  ]

  CATS = [
    { name: 'Banking & Financial Services', color: '#2f7fd6', items: ['Corporate', 'Retail', 'NBFS'] }
  ]

  // past scraper runs of the latest regulatory scan (docs counts mirror LANES circulars)
  SCANS = [
    { src: 'RBI', color: '#2f7fd6', time: '04 Jul 2026 · 12:04:22', docs: 4, size: '1.8 MB', ok: true, note: 'Master Directions & FEMA notifications synced' },
    { src: 'SEBI', color: '#cf861b', time: '04 Jul 2026 · 12:04:19', docs: 4, size: '0.9 MB', ok: true, note: 'LODR + PIT circular index delta' },
    { src: 'IRDAI', color: '#1f9d63', time: '04 Jul 2026 · 12:04:15', docs: 4, size: '0.6 MB', ok: true, note: 'AML/CFT & corporate-governance circulars' },
    { src: 'RBI', color: '#2f7fd6', time: '04 Jul 2026 · 06:00:11', docs: 0, size: '—', ok: false, note: 'Source gateway timeout — auto-retried next window' },
    { src: 'SEBI', color: '#cf861b', time: '03 Jul 2026 · 23:30:04', docs: 2, size: '0.4 MB', ok: true, note: 'Mutual-fund master circular update' }
  ]

  // orchestrator routing — which screening agents the orchestrator invokes per transaction
  ROUTES = {
    mule: { pick: ['sanctions', 'mule', 'structuring', 'velocity'], why: 'High-value RTGS wire to a corporate beneficiary with a prior fan-in signature — orchestrator dispatched the layering, structuring and velocity agents alongside the standing sanctions screen. Geo and FEMA agents were left idle (domestic, enrolled device).' },
    structuring: { pick: ['sanctions', 'structuring', 'velocity', 'mule'], why: 'Several sub-threshold IMPS tranches to a single counterparty — orchestrator dispatched structuring, velocity and mule agents plus the sanctions screen. Geo and FEMA agents not relevant (domestic, single device).' },
    velocity: { pick: ['sanctions', 'velocity', 'mule', 'structuring'], why: 'A burst of low-value UPI debits to many first-time payees — orchestrator dispatched velocity, mule and structuring agents plus sanctions. FEMA and geo agents skipped (domestic UPI, one device).' },
    geo: { pick: ['sanctions', 'geo', 'velocity', 'fema'], why: 'Card-not-present authorisation cleared through a foreign acquirer — orchestrator dispatched the geolocation, velocity and FEMA agents plus sanctions. Structuring and mule agents left idle (single authorisation).' },
    fema: { pick: ['sanctions', 'fema', 'velocity'], why: 'A large cross-border SWIFT remittance — orchestrator dispatched the FEMA/LRS and velocity agents plus a sanctions/adverse-media screen. Structuring, mule and geo agents were not relevant to a single outward wire.' },
    _clean: { pick: ['sanctions', 'structuring'], why: 'A low-value domestic NEFT to a known, previously-paid counterparty — orchestrator invoked only the baseline sanctions and structuring screens. No behavioural, cross-border or network agents were required.' },
    _live: { pick: ['structuring', 'sanctions', 'mule', 'velocity', 'fema', 'geo'], why: 'Live transaction ingested via API. The orchestrator dynamically routes it to all active screening agents for comprehensive real-time risk assessment.' }
  }
  routeFor(sample) { return this.ROUTES[sample && sample.graphType] || this.ROUTES._clean }
  scoreFor(v, i) {
    if (v === 'hit') return 88 + ((i * 2) % 11)
    if (v === 'flag') return 52 + ((i * 5) % 19)
    return 4 + ((i * 3) % 11)
  }

  // regulatory citations applied per typology (shown on the final outcome page)
  CITES = {
    mule: [
      { code: 'PMLA 2002', ref: '§12 · reporting obligations', note: 'Suspicious Transaction Report filed with FIU-IND for suspected layering.', url: 'https://www.fiuindia.gov.in/' },
      { code: 'RBI MD-KYC', ref: 'Master Direction · para 42', note: 'Ongoing monitoring & beneficial-ownership checks on pass-through accounts.', url: 'https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx' },
      { code: 'FATF R.10', ref: 'Customer due diligence', note: 'Placement → layering → integration typology guidance.', url: 'https://www.fatf-gafi.org/' }
    ],
    structuring: [
      { code: 'PMLA 2002', ref: '§12 · CTR / STR', note: 'Structuring below the ₹10,00,000 Cash Transaction Report threshold is reportable.', url: 'https://www.fiuindia.gov.in/' },
      { code: 'RBI MD-KYC', ref: 'Master Direction · para 51', note: 'Alert on transactions structured to avoid reporting thresholds.', url: 'https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx' }
    ],
    velocity: [
      { code: 'RBI MD-KYC', ref: 'Master Direction · para 53', note: 'Behavioural monitoring & Enhanced Due Diligence on anomalous velocity.', url: 'https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx' },
      { code: 'RBI DPSC', ref: 'Digital Payment Security Controls', note: 'Account-takeover / bust-out indicators on payment rails.', url: 'https://www.rbi.org.in/Scripts/BS_CircularIndexDisplay.aspx' }
    ],
    geo: [
      { code: 'RBI DPSC', ref: 'Digital Payment Security Controls', note: 'Step-up authentication on impossible-travel / device anomalies.', url: 'https://www.rbi.org.in/Scripts/BS_CircularIndexDisplay.aspx' },
      { code: 'PMLA 2002', ref: '§12 · STR', note: 'Suspected account takeover reported to FIU-IND.', url: 'https://www.fiuindia.gov.in/' }
    ],
    fema: [
      { code: 'FEMA 1999', ref: '§3 · dealing in forex', note: 'Outward remittance beyond permissible limits blocked pending AD-bank review.', url: 'https://www.rbi.org.in/Scripts/BS_FemaNotifications.aspx' },
      { code: 'RBI LRS', ref: 'Liberalised Remittance Scheme', note: 'USD 250,000 per-FY ceiling breached — escalated to RBI.', url: 'https://www.rbi.org.in/Scripts/BS_FemaNotifications.aspx' },
      { code: 'FEMA Sch. III', ref: 'Current-account transactions', note: 'Permissible-purpose validation on the remittance.', url: 'https://www.rbi.org.in/Scripts/BS_FemaNotifications.aspx' }
    ],
    _clean: [
      { code: 'RBI MD-KYC', ref: 'Master Direction · para 42', note: 'Baseline sanctions & monitoring cleared — straight-through processed.', url: 'https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx' }
    ]
  }
  citesFor(sample) { return this.CITES[sample && sample.graphType] || this.CITES._clean }

  toggleCat = (i) => this.setState(s => ({ openCat: s.openCat === i ? -1 : i }))

  get allSamples() { return this.SAMPLES.concat(this.state.userTxns) }
  get s() { return this.allSamples[this.state.sample] || this.SAMPLES[0] }

  // ---------- navigation ----------
  resetRun() { this.clearTimers(); this.setState({ dstate: {}, graphShow: false, fanIn: false, verdictShow: false, ingestCount: 0, ingestDone: false, queueArrived: 0, orchShow: false, reportProgress: 0, filed: false, reportModal: false, explainOpen: false }) }

  launch = () => { this.resetRun(); this.setState({ view: 'run', step: 1 }); this.after(200, () => this.startFeed()) }
  back = () => { this.clearTimers(); this.setState({ view: 'overview' }) }
  replay = () => { this.goStep(this.state.step) }
  replayAll = () => { this.goStep(1) }
  pickSample = (i) => { this.resetRun(); this.setState({ sample: i, samplePage: Math.floor(i / 10) }); this.after(120, () => this.goStep(this.state.step)) }

  goStep = (i) => {
    this.resetRun()
    this.setState({ step: i })
    this.after(120, () => {
      if (i === 1) this.startFeed()
      else if (i === 2) this.startIngest()
      else if (i === 3) this.startDetection()
      else if (i === 4) this.startReport()
      else if (i === 5) this.startOutcome()
    })
  }
  runReal = () => this.goStep(1)
  toQueue = () => this.goStep(2)
  toDetect = () => this.goStep(3)
  toReport = () => this.goStep(4)
  toOutcome = () => this.goStep(5)
  openReport = () => this.setState({ reportModal: true })
  closeReport = () => this.setState({ reportModal: false })
  openCirc = (src) => this.setState({ circModal: true, circLane: typeof src === 'string' ? src : null })
  closeCirc = () => this.setState({ circModal: false })
  toggleExplain = () => this.setState(s => ({ explainOpen: !s.explainOpen }))
  stopEvt = (e) => { e.stopPropagation() }

  // what each screening agent inspects — used to build the "why was this flagged" explanation
  AGENT_DESC = {
    structuring: 'screens for a large value deliberately broken into multiple sub-threshold tranches to stay under the ₹10,00,000 Cash Transaction Report line',
    sanctions: 'screens every counterparty against sanctions, PEP and adverse-media watchlists',
    mule: 'traces whether value is layered through pass-through / mule accounts to obscure the true beneficiary',
    velocity: 'profiles the account\'s behavioural baseline and flags sharp deviations in transaction velocity or new-payee spread',
    fema: 'checks cumulative outward remittance against the FEMA / LRS USD 250,000 financial-year ceiling',
    geo: 'compares session geolocation, device and timing for impossible-travel and account-takeover signals'
  }
  V_RATIONALE = {
    hit: 'This is a strong, reportable indicator and is the main driver of the escalation.',
    flag: 'This is a secondary concern — it raises the overall risk score but is not conclusive on its own.',
    clear: 'No issue was found on this dimension, so it does not contribute to the escalation.'
  }

  // ---------- step animations ----------
  startFeed() { this.clearTimers() }

  startIngest() {
    this.clearTimers()
    this.setState({ queueArrived: 0, ingestCount: 0, ingestDone: false })
    // transactions stream into the queue one by one
    const qn = Math.min(4, this.allSamples.length)
    for (let k = 1; k <= qn; k++) this.after(k * 280, () => this.setState({ queueArrived: k }))
    // then the front transaction is picked up and its record is read field-by-field
    const base = qn * 280 + 350
    const n = this.s.fields.length
    for (let k = 1; k <= n; k++) this.after(base + k * 190, () => this.setState({ ingestCount: k }))
    this.after(base + n * 190 + 450, () => this.setState({ ingestDone: true }))
  }

  startDetection() {
    this.clearTimers()
    const route = this.routeFor(this.s)
    const dstate = {}
    this.DET.forEach(d => { dstate[d.id] = route.pick.includes(d.id) ? 'queued' : 'skip' })
    this.setState({ dstate, graphShow: false, fanIn: false, verdictShow: false, orchShow: false })
    // the orchestrator agent analyses first, then dispatches the relevant agents
    this.after(200, () => this.setState({ orchShow: true }))
    this.after(1100, () => this.setState(st => {
      const d2 = { ...st.dstate }; route.pick.forEach(id => { d2[id] = 'checking' }); return { dstate: d2 }
    }))
    const idx = this.state.sample
    this.fetchReal(idx).then(() => {
      if (this.state.step !== 3 || this.state.sample !== idx) return
      this.revealDetection(route)
    })
  }

  revealDetection(route) {
    const res = this.s.results
    const picks = route.pick
    picks.forEach((id, i) => this.after(1400 + i * 650, () => this.setState(st => ({ dstate: { ...st.dstate, [id]: res[id].v } }))))
    const doneAt = 1400 + picks.length * 650 + 300
    const hasGraph = !!this.s.graphType
    if (hasGraph) {
      this.after(doneAt, () => this.setState({ graphShow: true }))
      this.after(doneAt + 2400, () => this.setState({ fanIn: true, verdictShow: true }))
    } else {
      this.after(doneAt + 200, () => this.setState({ fanIn: true, verdictShow: true }))
    }
  }

  startReport() {
    this.clearTimers()
    this.setState({ reportProgress: 0, filed: false })
    const idx = this.state.sample
    
    const advance = () => {
      [12, 32, 55, 74, 90, 100].forEach((p, i) => this.after(350 + i * 340, () => this.setState({ reportProgress: p })))
      this.after(350 + 6 * 340 + 400, () => this.setState({ filed: true }))
    }

    this.fetchReal(idx).then(() => {
      if (this.state.step === 4 && this.state.sample === idx) {
        this.setState({})
        advance()
      }
    })
  }

  startOutcome() {
    this.clearTimers()
    const idx = this.state.sample
    
    this.fetchReal(idx).then(() => {
      if (this.state.step === 5 && this.state.sample === idx) {
        this.setState({})
      }
    })
  }

  // ---------- graph geometry ----------
  XS = [9.5, 25.7, 41.9, 58.1, 74.3, 90.5]
  TONE = { clear: '#1f9d63', flag: '#cf861b', hit: '#e0455a', checking: '#cf861b', idle: '#c3bcae' }

  GREY = '#b3ada2'
  PART = '#9c968c'

  detectionEdges() {
    const { dstate, fanIn } = this.state
    const GREY = this.GREY, PART = this.PART
    const DET_Y = 15.5, CARD_TOP = 33.5, CARD_BOT = 48.5, VERD_TOP = 64
    const kids = []
    this.XS.forEach((cx, i) => {
      const d = this.DET[i]; const s0 = dstate[d.id] || 'idle'
      const skipped = s0 === 'skip'
      const resolved = s0 === 'clear' || s0 === 'flag' || s0 === 'hit'
      const p = `M50,${DET_Y} L${cx},${CARD_TOP}`
      // orchestrator → agent branch. Skipped (not-invoked) agents get a faint idle stub.
      kids.push(React.createElement('path', { key: 'o' + i, d: p, fill: 'none', stroke: GREY, strokeWidth: 0.3, strokeLinecap: 'round', strokeDasharray: resolved ? 'none' : '0.9 2', opacity: skipped ? 0.16 : (resolved ? 0.45 : (s0 === 'idle' ? 0.4 : 0.8)), style: (resolved || skipped) ? { transition: 'stroke .4s,opacity .4s' } : { animation: 'flowDash 3.2s linear infinite' } }))
      if (s0 === 'checking') {
        for (let k = 0; k < 2; k++) kids.push(React.createElement('circle', { key: 'op' + i + '_' + k, r: 0.45, fill: PART, style: { offsetPath: `path('${p}')`, offsetDistance: '0%', animation: `particleFlow 5.8s linear ${k * 1.8}s infinite` } }))
      }
    })
    if (fanIn) {
      this.XS.forEach((cx, i) => {
        const d = this.DET[i]; const s0 = dstate[d.id] || 'idle'
        if (s0 === 'skip') return // not-invoked agents don't feed the verdict
        const p = `M${cx},${CARD_BOT} L50,${VERD_TOP}`
        kids.push(React.createElement('path', { key: 'i' + i, d: p, fill: 'none', stroke: GREY, strokeWidth: 0.3, strokeLinecap: 'round', strokeDasharray: '0.9 2', opacity: 0.6, style: { animation: 'flowDash 3.2s linear infinite' } }))
        for (let k = 0; k < 2; k++) kids.push(React.createElement('circle', { key: 'ip' + i + '_' + k, r: 0.45, fill: PART, style: { offsetPath: `path('${p}')`, offsetDistance: '0%', animation: `particleFlow 5.4s linear ${k * 1.8}s infinite` } }))
      })
    }
    return React.createElement('svg', { viewBox: '0 0 100 78', preserveAspectRatio: 'xMidYMid meet', style: { position: 'absolute', inset: 0, width: '100%', height: '100%', overflow: 'visible', pointerEvents: 'none' } }, kids)
  }

  renderGraph(type) {
    if (type === 'mule') return this.muleGraph()
    if (type === 'structuring') return this.structuringGraph()
    if (type === 'velocity') return this.velocityGraph()
    if (type === 'geo') return this.geoGraph()
    if (type === 'fema') return this.femaGraph()
    return null
  }

  gsvg(kids) { return React.createElement('svg', { viewBox: '0 0 100 54', preserveAspectRatio: 'xMidYMid meet', style: { position: 'absolute', inset: 0, width: '100%', height: '100%', overflow: 'visible' } }, kids) }
  gcap(txt) { return React.createElement('text', { key: 'cap', x: 8, y: 6.5, fill: '#8a8478', fontSize: 2, fontFamily: "'IBM Plex Mono',monospace", fontWeight: 600 }, txt) }

  muleGraph() {
    const GREY = this.GREY, PART = this.PART, red = '#e0455a', dim = '#d0cabd', txt = '#1a1712'
    const nodes = [
      { x: 8, y: 27, w: 15, label: 'Originator', sub: 'Anita Desai', flag: false },
      { x: 35, y: 9, w: 15, label: 'Mule A ••4471', sub: '₹14,00,000', flag: true },
      { x: 35, y: 27, w: 15, label: 'Mule B ••8823', sub: '₹14,00,000', flag: true },
      { x: 35, y: 45, w: 15, label: 'Mule C ••1290', sub: '₹14,00,000', flag: true },
      { x: 64, y: 18, w: 16, label: 'Pass-through 1', sub: 'layering', flag: false },
      { x: 64, y: 36, w: 16, label: 'Pass-through 2', sub: 'layering', flag: false },
      { x: 91, y: 27, w: 16, label: 'Beneficiary', sub: 'Global Trade Ltd', flag: false }
    ]
    const edges = [[0, 1], [0, 2], [0, 3], [1, 4], [2, 4], [2, 5], [3, 5], [4, 6], [5, 6]]
    const H = 8
    const kids = []
    edges.forEach(([a, b], i) => {
      const na = nodes[a], nb = nodes[b]
      const x1 = na.x + na.w / 2, y1 = na.y + H / 2, x2 = nb.x - nb.w / 2, y2 = nb.y + H / 2
      const p = `M${x1},${y1} C${(x1 + x2) / 2},${y1} ${(x1 + x2) / 2},${y2} ${x2},${y2}`
      kids.push(React.createElement('path', { key: 'e' + i, d: p, fill: 'none', stroke: GREY, strokeWidth: 0.3, strokeLinecap: 'round', strokeDasharray: '0.9 2', opacity: 0.65, style: { animation: 'flowDash 3.2s linear infinite' } }))
      kids.push(React.createElement('circle', { key: 'ep' + i, r: 0.45, fill: PART, style: { offsetPath: `path('${p}')`, offsetDistance: '0%', animation: `particleFlow 5.8s linear ${(i % 3) * 1.8}s infinite` } }))
    })
    nodes.forEach((n, i) => {
      const stroke = n.flag ? red : dim
      kids.push(React.createElement('g', { key: 'n' + i },
        n.flag && React.createElement('rect', { x: n.x - n.w / 2, y: n.y, width: n.w, height: H, rx: 1.6, fill: 'none', stroke: red, strokeWidth: 0.45, style: { animation: 'glowPulse 2.8s infinite' } }),
        React.createElement('rect', { x: n.x - n.w / 2, y: n.y, width: n.w, height: H, rx: 1.6, fill: n.flag ? '#fdeef0' : '#ffffff', stroke: stroke, strokeWidth: 0.4 }),
        React.createElement('text', { x: n.x, y: n.y + 3.4, textAnchor: 'middle', fill: txt, fontSize: 2.05, fontWeight: 600, fontFamily: "'Space Grotesk',sans-serif" }, n.label),
        React.createElement('text', { x: n.x, y: n.y + 6.1, textAnchor: 'middle', fill: n.flag ? '#b8555f' : '#8a8478', fontSize: 1.7, fontFamily: "'IBM Plex Mono',monospace" }, n.sub)
      ))
    })
    kids.push(React.createElement('text', { key: 'l1', x: 21, y: 5, fill: '#8a8478', fontSize: 1.9, fontFamily: "'IBM Plex Mono',monospace", fontWeight: 600 }, 'FAN-OUT · placement'))
    kids.push(React.createElement('text', { key: 'l2', x: 78, y: 5, fill: '#8a8478', fontSize: 1.9, fontFamily: "'IBM Plex Mono',monospace", fontWeight: 600 }, 'FAN-IN · integration'))
    return this.gsvg(kids)
  }

  structuringGraph() {
    const GREY = this.GREY, amber = '#cf861b', txt = '#1a1712'
    const kids = []
    const bars = [{ l: 'T1', v: 9.49 }, { l: 'T2', v: 9.72 }, { l: 'T3', v: 9.15 }, { l: 'T4', v: 9.90 }, { l: 'T5', v: 9.38 }, { l: 'T6', v: 9.66 }]
    const baseY = 44, maxV = 12, chartH = 30, x0 = 14, gap = (88 - 14) / bars.length, bw = gap * 0.5
    const thV = 10, thY = baseY - (thV / maxV) * chartH
    kids.push(React.createElement('line', { key: 'base', x1: 8, y1: baseY, x2: 94, y2: baseY, stroke: GREY, strokeWidth: 0.4 }))
    bars.forEach((b, i) => {
      const x = x0 + gap * i + (gap - bw) / 2, h = (b.v / maxV) * chartH, y = baseY - h
      kids.push(React.createElement('rect', { key: 'b' + i, x, y, width: bw, height: h, rx: 0.7, fill: '#f4ede2', stroke: amber, strokeWidth: 0.4 }))
      kids.push(React.createElement('text', { key: 'bl' + i, x: x + bw / 2, y: baseY + 3.2, textAnchor: 'middle', fill: '#8a8478', fontSize: 1.9, fontFamily: "'IBM Plex Mono',monospace" }, b.l))
      kids.push(React.createElement('text', { key: 'bv' + i, x: x + bw / 2, y: y - 1.2, textAnchor: 'middle', fill: txt, fontSize: 1.85, fontFamily: "'IBM Plex Mono',monospace", fontWeight: 600 }, '₹' + b.v + 'L'))
    })
    kids.push(React.createElement('line', { key: 'th', x1: 8, y1: thY, x2: 94, y2: thY, stroke: amber, strokeWidth: 0.45, strokeDasharray: '2 1.6' }))
    kids.push(React.createElement('text', { key: 'tht', x: 94, y: thY - 1.2, textAnchor: 'end', fill: amber, fontSize: 2, fontFamily: "'IBM Plex Mono',monospace", fontWeight: 600 }, 'CTR threshold · ₹10,00,000'))
    kids.push(this.gcap('Six tranches structured just below the CTR reporting threshold'))
    return this.gsvg(kids)
  }

  velocityGraph() {
    const GREY = this.GREY, red = '#e0455a'
    const kids = []
    const baseY = 42, x0 = 10, x1 = 92, w = x1 - x0, maxV = 11, chartH = 28
    kids.push(React.createElement('line', { key: 'ax', x1: x0, y1: baseY, x2: x1, y2: baseY, stroke: GREY, strokeWidth: 0.4 }))
    const baseLevel = baseY - (1 / maxV) * chartH
    kids.push(React.createElement('line', { key: 'bl', x1: x0, y1: baseLevel, x2: x1, y2: baseLevel, stroke: GREY, strokeWidth: 0.35, strokeDasharray: '2 1.6', opacity: 0.8 }))
    kids.push(React.createElement('text', { key: 'blt', x: x0, y: baseLevel - 1, fill: '#8a8478', fontSize: 1.8, fontFamily: "'IBM Plex Mono',monospace" }, 'baseline ~2 txns/day'))
    const pts = [[0, 0.2], [0.28, 0.5], [0.44, 1], [0.5, 8], [0.54, 3], [0.58, 9.5], [0.62, 4], [0.66, 10.5], [0.7, 2.5], [0.74, 6], [0.8, 1], [1, 0.4]]
    const coords = pts.map(([fx, fv]) => [x0 + fx * w, baseY - (fv / maxV) * chartH])
    const dPath = 'M' + coords.map(c => c[0].toFixed(1) + ',' + c[1].toFixed(1)).join(' L')
    kids.push(React.createElement('rect', { key: 'win', x: x0 + 0.48 * w, y: baseY - chartH, width: 0.3 * w, height: chartH, fill: red, opacity: 0.06 }))
    kids.push(React.createElement('path', { key: 'spk', d: dPath, fill: 'none', stroke: red, strokeWidth: 0.5, strokeLinejoin: 'round', opacity: 0.9 }))
    coords.forEach((c, i) => { if (pts[i][1] >= 3) kids.push(React.createElement('circle', { key: 'm' + i, cx: c[0], cy: c[1], r: 0.7, fill: red })) })
    kids.push(React.createElement('text', { key: 'wt', x: x0 + 0.63 * w, y: baseY - chartH - 1.2, textAnchor: 'middle', fill: red, fontSize: 2, fontFamily: "'IBM Plex Mono',monospace", fontWeight: 600 }, '38 txns / 4 min'))
    kids.push(React.createElement('text', { key: 'xr', x: x1, y: baseY + 3, textAnchor: 'end', fill: '#8a8478', fontSize: 1.8, fontFamily: "'IBM Plex Mono',monospace" }, 'time →'))
    kids.push(this.gcap('Transaction-velocity spike vs the account behavioural baseline'))
    return this.gsvg(kids)
  }

  geoGraph() {
    const GREY = this.GREY, PART = this.PART, red = '#e0455a', blue = '#2f7fd6', txt = '#1a1712'
    const kids = []
    const A = { x: 24, y: 34, label: 'Mumbai, IN', sub: '14:22:07 · ••4471' }
    const B = { x: 76, y: 22, label: 'Lagos, NG', sub: '14:31:12 · new device' }
    const p = `M${A.x},${A.y} C${(A.x + B.x) / 2 - 6},${A.y - 22} ${(A.x + B.x) / 2 + 6},${B.y - 14} ${B.x},${B.y}`
    kids.push(React.createElement('path', { key: 'arc', d: p, fill: 'none', stroke: GREY, strokeWidth: 0.32, strokeDasharray: '1 2.2', style: { animation: 'flowDash 3.2s linear infinite' } }))
    for (let k = 0; k < 2; k++) kids.push(React.createElement('circle', { key: 'ap' + k, r: 0.5, fill: PART, style: { offsetPath: `path('${p}')`, offsetDistance: '0%', animation: `particleFlow 5.8s linear ${k * 1.8}s infinite` } }))
    ;[A, B].forEach((n, i) => {
      const flag = i === 1, col = flag ? red : blue
      if (flag) kids.push(React.createElement('circle', { key: 'ring' + i, cx: n.x, cy: n.y, r: 2.4, fill: 'none', stroke: red, strokeWidth: 0.4, style: { animation: 'glowPulse 2.8s infinite' } }))
      kids.push(React.createElement('circle', { key: 'pin' + i, cx: n.x, cy: n.y, r: 2.2, fill: flag ? '#fdeef0' : '#eef4fb', stroke: col, strokeWidth: 0.5 }))
      kids.push(React.createElement('circle', { key: 'pd' + i, cx: n.x, cy: n.y, r: 0.8, fill: col }))
      kids.push(React.createElement('text', { key: 'pl' + i, x: n.x, y: n.y - 3.4, textAnchor: 'middle', fill: txt, fontSize: 2.2, fontWeight: 600, fontFamily: "'Space Grotesk',sans-serif" }, n.label))
      kids.push(React.createElement('text', { key: 'ps' + i, x: n.x, y: n.y + 4.8, textAnchor: 'middle', fill: '#8a8478', fontSize: 1.7, fontFamily: "'IBM Plex Mono',monospace" }, n.sub))
    })
    kids.push(React.createElement('text', { key: 'd1', x: 50, y: 8, textAnchor: 'middle', fill: red, fontSize: 2.2, fontWeight: 600, fontFamily: "'IBM Plex Mono',monospace" }, 'Δ 9 min · ~11,000 km  ⇒  impossible travel'))
    kids.push(React.createElement('text', { key: 'cap2', x: 50, y: 50, textAnchor: 'middle', fill: '#8a8478', fontSize: 2, fontFamily: "'IBM Plex Mono',monospace" }, 'Required velocity exceeds any commercial flight'))
    return this.gsvg(kids)
  }

  femaGraph() {
    const GREY = this.GREY, red = '#e0455a', green = '#1f9d63'
    const kids = []
    const x0 = 10, x1 = 92, w = x1 - x0, barY = 26, barH = 8
    const ceilFrac = 250 / 280, priorFrac = 210 / 280, thisFrac = 268 / 280
    kids.push(React.createElement('rect', { key: 'trk', x: x0, y: barY, width: w, height: barH, rx: 1.4, fill: '#f1ece3', stroke: GREY, strokeWidth: 0.35 }))
    kids.push(React.createElement('rect', { key: 'prior', x: x0, y: barY, width: w * priorFrac, height: barH, rx: 1.4, fill: '#e4efe8', stroke: green, strokeWidth: 0.35 }))
    kids.push(React.createElement('rect', { key: 'over', x: x0 + w * priorFrac, y: barY, width: w * (thisFrac - priorFrac), height: barH, fill: '#fdeef0', stroke: red, strokeWidth: 0.4 }))
    const cx = x0 + w * ceilFrac
    kids.push(React.createElement('line', { key: 'ceil', x1: cx, y1: barY - 4, x2: cx, y2: barY + barH + 4, stroke: red, strokeWidth: 0.5, strokeDasharray: '1.6 1.4' }))
    kids.push(React.createElement('text', { key: 'ceilt', x: cx, y: barY - 5, textAnchor: 'middle', fill: red, fontSize: 2, fontWeight: 600, fontFamily: "'IBM Plex Mono',monospace" }, 'LRS ceiling · USD 250,000'))
    kids.push(React.createElement('text', { key: 'lg1', x: x0, y: barY - 5, fill: green, fontSize: 1.9, fontFamily: "'IBM Plex Mono',monospace" }, 'prior FY · USD 210,000'))
    const tx = x0 + w * thisFrac
    kids.push(React.createElement('text', { key: 'cur', x: Math.min(tx, 80), y: barY + barH + 7, textAnchor: 'middle', fill: red, fontSize: 2.1, fontWeight: 600, fontFamily: "'IBM Plex Mono',monospace" }, 'cumulative USD 268,000  ▲ breach'))
    kids.push(this.gcap('FEMA / LRS outward-remittance ceiling breach on this SWIFT wire'))
    return this.gsvg(kids)
  }

  // ---------- render-values (props bag consumed by the JSX below) ----------
  renderVals() {
    const st = this.state; const S = this.s
    const glyph = { clear: '✓', flag: '⚠', hit: '✕' }

    // stepper — run flow is ingestion(1) → detection(2) → report(3)
    const stepMeta = [[1, 'Gather circular feed', 'pull the latest circulars'], [2, 'Transaction Queue', 'queue & read the payment'], [3, 'Compliance check', 'orchestrated screening agents'], [4, 'Report generation', 'file within the deadline'], [5, 'Outcome', 'result, report & citations']]
    const steps = stepMeta.map((m, j) => {
      const idx = m[0]
      const active = idx === st.step, done = idx < st.step
      const border = active ? 'rgba(207,134,27,.55)' : done ? 'rgba(31,157,99,.45)' : 'rgba(0,0,0,.1)'
      const bg = active ? 'rgba(207,134,27,.1)' : done ? 'rgba(31,157,99,.06)' : '#fffdfa'
      return { name: m[1], sub: m[2], onClick: () => this.goStep(idx),
        style: `flex:1;min-width:190px;cursor:pointer;display:flex;align-items:center;gap:11px;text-align:left;background:${bg};border:1px solid ${border};border-radius:13px;padding:13px 15px;transition:.25s` }
    })

    // samples — paginated 10 at a time
    const SAMPLE_PAGE = 10
    const allS = this.allSamples
    const totalPages = Math.max(1, Math.ceil(allS.length / SAMPLE_PAGE))
    const curPage = Math.min(Math.max(0, st.samplePage), totalPages - 1)
    const pageStart = curPage * SAMPLE_PAGE
    const samples = allS.slice(pageStart, pageStart + SAMPLE_PAGE).map((sm, j) => {
      const i = pageStart + j
      const on = i === st.sample
      return { name: sm.name, tag: sm.tag, onClick: () => this.pickSample(i),
        style: `cursor:pointer;text-align:left;padding:10px 15px;border-radius:11px;transition:.2s;border:1px solid ${on ? 'rgba(63,111,214,.55)' : 'rgba(0,0,0,.1)'};background:${on ? 'rgba(63,111,214,.1)' : '#fffdfa'};color:${on ? '#2a56ac' : '#54504a'}` }
    })
    const pageBtn = (enabled) => `cursor:${enabled ? 'pointer' : 'default'};background:rgba(0,0,0,.04);border:1px solid rgba(0,0,0,.1);color:${enabled ? '#54504a' : '#bdb7ad'};font-size:12.5px;padding:7px 12px;border-radius:9px;display:flex;align-items:center;gap:6px`
    const samplePager = {
      show: allS.length > SAMPLE_PAGE,
      label: `${pageStart + 1}–${Math.min(pageStart + SAMPLE_PAGE, allS.length)} of ${allS.length}`,
      prevStyle: pageBtn(curPage > 0), nextStyle: pageBtn(curPage < totalPages - 1),
      prev: () => { if (curPage > 0) this.setState({ samplePage: curPage - 1 }) },
      next: () => { if (curPage < totalPages - 1) this.setState({ samplePage: curPage + 1 }) }
    }

    // compliance-check agents — dispatched by the orchestrator, each reports score + verdict
    const route = this.routeFor(S)
    const vLabel = { hit: 'Escalate', flag: 'Review', clear: 'Clear' }
    const agents = this.DET.map((d, i) => {
      const s0 = st.dstate[d.id] || 'idle'
      const invoked = route.pick.includes(d.id)
      const resolved = s0 === 'clear' || s0 === 'flag' || s0 === 'hit'
      const running = s0 === 'checking'
      const skipped = s0 === 'skip'
      const tone = this.TONE[s0] || this.TONE.idle
      const res = S.results[d.id] || { v: 'clear', note: '' }
      const score = resolved ? this.scoreFor(res.v, i) : null
      const border = resolved ? tone : (running ? 'rgba(207,134,27,.5)' : 'rgba(0,0,0,.1)')
      const glow = s0 === 'hit' ? '0 6px 22px rgba(224,69,90,.22)' : s0 === 'flag' ? '0 6px 18px rgba(207,134,27,.2)' : s0 === 'clear' ? '0 5px 16px rgba(31,157,99,.18)' : '0 1px 3px rgba(0,0,0,.04)'
      return {
        name: d.name, question: d.q, invoked, running, resolved, skipped,
        badge: resolved ? glyph[s0] : '', verdict: resolved ? vLabel[res.v] : '', note: res.note,
        score, scorePct: (score || 0) + '%',
        cardStyle: `background:${skipped ? '#faf8f4' : '#ffffff'};border:1px solid ${border};border-radius:14px;padding:14px 15px 15px;box-shadow:${skipped ? 'none' : glow};opacity:${skipped ? 0.6 : 1};transition:border-color .35s,box-shadow .35s,opacity .35s`,
        treeCardStyle: `position:absolute;left:${this.XS[i]}%;top:41%;transform:translate(-50%,-50%);width:15%;min-width:152px;background:${skipped ? '#faf8f4' : '#ffffff'};border:1px solid ${border};border-radius:12px;padding:11px 12px 12px;box-shadow:${skipped ? 'none' : glow};opacity:${skipped ? 0.55 : 1};transition:border-color .35s,box-shadow .35s,opacity .35s`,
        badgeStyle: `flex:none;width:22px;height:22px;border-radius:7px;background:${resolved ? tone : 'rgba(0,0,0,.06)'};color:${resolved ? '#fff' : '#a9a297'};display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700`,
        verdictStyle: `font-family:'IBM Plex Mono',monospace;font-size:10px;font-weight:700;letter-spacing:.4px;color:${tone};background:${tone}14;border:1px solid ${tone}44;border-radius:7px;padding:3px 8px`,
        scoreStyle: `font-family:'Space Grotesk',sans-serif;font-weight:700;font-size:22px;letter-spacing:-.5px;color:${tone}`,
        treeScoreStyle: `font-family:'Space Grotesk',sans-serif;font-weight:700;font-size:18px;letter-spacing:-.5px;color:${tone}`,
        barStyle: `width:${score || 0}%;height:100%;background:${tone};border-radius:5px;transition:width .5s ease`,
        noteStyle: `font-size:11.5px;line-height:1.4;color:${tone};font-weight:600`
      }
    })
    const anyRunning = Object.values(st.dstate).some(v => v === 'checking')
    const orch = {
      show: st.orchShow, running: anyRunning || !st.verdictShow, why: route.why,
      dispatched: route.pick.length, total: this.DET.length,
      chips: route.pick.map(id => (this.DET.find(d => d.id === id) || {}).name).filter(Boolean),
      nodeStyle: `background:#ffffff;border:1px solid ${anyRunning ? 'rgba(124,91,239,.55)' : 'rgba(124,91,239,.35)'};border-radius:16px;padding:18px 20px;box-shadow:${anyRunning ? '0 8px 26px rgba(124,91,239,.18)' : '0 2px 10px rgba(0,0,0,.05)'};transition:.3s`,
      rootStyle: `position:absolute;left:50%;top:8%;transform:translate(-50%,-50%);text-align:center;min-width:210px;background:linear-gradient(180deg,#f5f2fe,#ffffff);border:1px solid ${anyRunning ? 'rgba(124,91,239,.6)' : 'rgba(124,91,239,.4)'};border-radius:14px;padding:12px 22px;box-shadow:${anyRunning ? '0 6px 24px rgba(124,91,239,.22)' : '0 2px 10px rgba(0,0,0,.06)'};transition:.3s`
    }

    // verdict
    const V = S.verdict; const vtone = this.TONE[V.tone]
    const vIcon = V.tone === 'hit' ? '✕' : V.tone === 'flag' ? '⚠' : '✓'
    const verdict = {
      show: st.verdictShow, title: V.title, line: V.line, icon: vIcon,
      nodeStyle: `position:absolute;left:50%;top:70%;transform:translate(-50%,-50%);text-align:center;min-width:220px;background:${V.tone === 'hit' ? '#fdeef0' : '#eef8f1'};border:1.5px solid ${vtone};color:${vtone};border-radius:14px;padding:12px 22px;box-shadow:0 6px 26px ${vtone}44`,
      bannerStyle: `display:flex;align-items:center;gap:13px;background:${V.tone === 'hit' ? 'rgba(224,69,90,.08)' : 'rgba(31,157,99,.08)'};border:1px solid ${vtone}66;color:${vtone};padding:13px 18px;border-radius:13px;line-height:1.35`,
      iconStyle: `flex:none;width:30px;height:30px;border-radius:9px;background:${vtone};color:#fff;display:flex;align-items:center;justify-content:center;font-size:15px;font-weight:700`
    }

    // scenario typology graph
    const gm = S.graphMeta || {}
    const showGraph = st.graphShow && !!S.graphType
    const graph = {
      show: showGraph, title: gm.title || '', tag: gm.tag || '', desc: gm.desc || '',
      svg: showGraph ? this.renderGraph(S.graphType) : null,
      wrapStyle: `margin-top:22px;background:${vtone}0d;border:1px solid ${vtone}44;border-radius:18px;padding:22px;box-shadow:0 1px 3px rgba(0,0,0,.04)`,
      dotStyle: `flex:none;width:9px;height:9px;border-radius:50%;background:${vtone};box-shadow:0 0 0 4px ${vtone}2b`,
      titleStyle: `font-family:'Space Grotesk',sans-serif;font-weight:600;font-size:17px;color:${vtone}`,
      tagStyle: `margin-left:auto;font-family:'IBM Plex Mono',monospace;font-size:10.5px;color:${vtone}`
    }

    // ingest / transaction queue — fields the verifier reads (PII masked where necessary)
    const MASK_KEYS = new Set(['sender_account_id', 'receiver_account_id', 'sender_account', 'receiver_account', 'sender_pan', 'receiver_pan', 'sender_dob', 'receiver_dob', 'device_id', 'beneficiary_id'])
    const ingFields = S.fields.map((f, i) => {
      const shown = i < st.ingestCount
      const masked = MASK_KEYS.has(f[0])
      return { key: f[0], val: shown ? f[1] : '·········', masked, shown,
        rowStyle: `display:flex;align-items:center;gap:12px;padding:13px 22px;border-radius:10px;transition:.3s;opacity:${shown ? 1 : 0.35};background:${shown ? 'rgba(0,0,0,.025)' : 'transparent'}`,
        checkStyle: `flex:none;width:22px;height:22px;border-radius:7px;background:#1f9d63;color:#fff;display:flex;align-items:center;justify-content:center;font-size:13px;transition:.3s;opacity:${shown ? 1 : 0};transform:scale(${shown ? 1 : .5})` }
    })

    // transaction queue — payments stream in; only the picked-up (front) transaction reveals its detail
    const allQ = this.allSamples
    const queue = []
    const qn = Math.min(4, allQ.length)
    for (let k = 0; k < qn; k++) {
      const q = allQ[(st.sample + k) % allQ.length]
      const active = k === 0
      const arrived = k < st.queueArrived
      const maskedRef = 'TX••••' + String(q.reference || '').slice(-4)
      queue.push({
        active, arrived, ref: q.reference, maskedRef,
        amount: (q.amount || '').replace('.00', ''),
        channel: (q.channel || '').split(' ')[0],
        from: q.sender || '—', to: q.receiver || '—',
        statusLabel: active ? 'Processing' : 'Queued',
        style: `display:flex;align-items:center;gap:12px;padding:12px 15px;border-radius:12px;border:1px solid ${active ? 'rgba(47,127,214,.5)' : 'rgba(0,0,0,.09)'};background:${active ? 'rgba(47,127,214,.07)' : '#fffdfa'};animation:queueIn .42s ease both`,
        badgeStyle: `flex:none;font-family:'IBM Plex Mono',monospace;font-size:10px;font-weight:600;letter-spacing:.4px;padding:4px 9px;border-radius:20px;color:${active ? '#fff' : '#8a8478'};background:${active ? '#2f7fd6' : 'rgba(0,0,0,.05)'};${active ? 'animation:redGlow 1.6s infinite' : ''}`
      })
    }
    const queueWaiting = Math.max(0, (st.queueArrived || 0) - 1)
    // record of the picked-up transaction (party A → party B, sensitive fields masked)
    const senderAcc = (S.fields.find(ff => ff[0] === 'sender_account_id') || [])[1] || '—'
    const verify = {
      txId: S.reference, amount: S.amount.replace('.00', ''), channel: S.channel,
      partyA: S.sender, partyAAcc: senderAcc,
      partyB: S.receiver, partyBRef: 'EXT••' + String(S.reference).slice(-4)
    }
    const uploadBase = this.SAMPLES.length
    const uploadChips = st.userTxns.map((u, i) => {
      const on = st.sample === uploadBase + i
      return { name: u.sender, tag: u.tag, onClick: () => this.pickSample(uploadBase + i),
        style: `cursor:pointer;text-align:left;padding:9px 14px;border-radius:10px;transition:.2s;border:1px solid ${on ? 'rgba(31,157,99,.55)' : 'rgba(0,0,0,.12)'};background:${on ? 'rgba(31,157,99,.1)' : '#faf7f1'};color:${on ? '#178a55' : '#54504a'}` }
    })

    // report
    const hasReport = V.tone !== 'clear'
    let rFields, headerTitle, headerTag, filingTarget, doneText, headerTone
    if (hasReport) {
      headerTone = V.tone === 'hit' ? '#e0455a' : '#cf861b'
      headerTitle = 'Suspicious Transaction Report'
      headerTag = 'STR · auto-generated'
      filingTarget = 'FIU-IND'
      rFields = [
        ['Report type', V.title.includes('BLOCK') ? 'STR + payment block' : 'Suspicious Transaction Report (STR)'],
        ['Filed to', 'FIU-IND · under PMLA 2002'],
        ['Transaction', S.reference + ' · ' + S.amount.replace('.00', '')],
        ['Trigger', S.graphMeta ? S.graphMeta.trigger : 'Multiple risk indicators'],
        ['Deadline', '7 days (regulatory)']
      ]
      doneText = 'Filed within deadline'
    } else {
      headerTone = '#1f9d63'
      headerTitle = 'No report required'
      headerTag = 'auto-cleared'
      filingTarget = 'audit log'
      rFields = [
        ['Outcome', 'Cleared automatically'],
        ['Transaction', S.reference + ' · ' + S.amount.replace('.00', '')],
        ['Human review', 'Not needed'],
        ['Audit trail', 'Recorded for 6 months'],
        ['Action', 'Payment allowed to proceed']
      ]
      doneText = 'Cleared & logged'
    }
    const report = {
      hasReport, noReport: !hasReport, headerTitle, headerTag, filingTarget,
      headerStyle: `display:flex;align-items:center;justify-content:space-between;padding:14px 18px;border-bottom:1px solid rgba(0,0,0,.06);background:${headerTone}16;color:${headerTone}`,
      fields: rFields.map((f, i) => ({ key: f[0], val: f[1], rowStyle: `display:flex;align-items:baseline;gap:12px;padding:11px 18px;${i < rFields.length - 1 ? 'border-bottom:1px solid rgba(0,0,0,.05)' : ''}` })),
      progressPct: st.reportProgress + '%', progressLabel: st.reportProgress + '%',
      filed: st.filed, speed: '2.4 seconds',
      doneText, doneIcon: '✓',
      doneStyle: `display:flex;align-items:center;gap:10px;background:${headerTone}1c;border:1px solid ${headerTone}66;color:${headerTone};font-size:14px;font-weight:600;padding:12px 18px;border-radius:12px`,
      doneIconStyle: `width:22px;height:22px;border-radius:50%;background:${headerTone};color:#fff;display:flex;align-items:center;justify-content:center;font-size:12px`
    }

    const CONFMETA = {
      mule: { conf: 96, rule: 'AML-TYP-014 · Layering (fan-out / fan-in)', summary: '₹42,00,000 fanned out across three mule accounts and reintegrated at the beneficiary — a textbook placement → layering → integration typology.' },
      structuring: { conf: 93, rule: 'AML-TYP-007 · Structuring / smurfing', summary: 'Six correlated tranches totalling ₹56,70,000, each engineered just below the ₹10,00,000 CTR threshold within 48 hours.' },
      velocity: { conf: 88, rule: 'AML-BEH-021 · Velocity deviation', summary: '38 debits across 12 first-time payees inside a 4-minute window against a ~2-transactions/day baseline — bust-out pattern.' },
      geo: { conf: 91, rule: 'FRD-GEO-009 · Impossible travel', summary: 'Two authenticated sessions ~11,000 km apart within 9 minutes on one credential — strong account-takeover indicator.' },
      fema: { conf: 99, rule: 'FEMA-LRS-002 · Outward-remittance ceiling', summary: 'Cumulative outward remittance of USD 268,000 breaches the USD 250,000 LRS financial-year ceiling.' }
    }
    const cm = (S.graphType && CONFMETA[S.graphType]) ? CONFMETA[S.graphType] : { conf: V.tone === 'clear' ? 3 : (S.liveCites && S.liveCites.length ? 92 : 75), rule: V.tone === 'clear' ? 'STP-000 · No typology match' : 'L2/L3 · Dynamic detection', summary: V.line || 'Live transaction processed via the orchestrator pipeline.' }
    const cleared = V.tone === 'clear'
    const confTone = cleared ? '#1f9d63' : (cm.conf >= 90 ? '#e0455a' : '#cf861b')
    report.openReport = this.openReport; report.closeReport = this.closeReport; report.stop = this.stopEvt; report.modalOpen = st.reportModal
    const pdfUrl = S.strPdfUrl ? (/^https?:/.test(S.strPdfUrl) ? S.strPdfUrl : this.BACKEND + S.strPdfUrl) : null
    report.pdfUrl = pdfUrl; report.hasPdf = !!pdfUrl; report.noPdf = !pdfUrl
    report.confPct = cm.conf + '%'
    report.confLabelUp = cleared ? 'TYPOLOGY-MATCH CONFIDENCE' : 'SUSPICION CONFIDENCE'
    report.confSub = cleared ? 'Composite across all six risk engines' : 'Composite score · rules + ML ensemble'
    report.rule = cm.rule; report.summary = cm.summary
    report.amt = S.amount.replace('.00', ''); report.ref = S.reference
    report.confBoxStyle = `background:#fffdfa;border:1px solid ${confTone}44;border-radius:16px;padding:18px 20px;box-shadow:0 1px 3px rgba(0,0,0,.05)`
    report.confValStyle = `font-family:'Space Grotesk',sans-serif;font-weight:600;font-size:40px;line-height:1;letter-spacing:-1.5px;color:${confTone}`
    report.confBarStyle = `width:${cm.conf}%;height:100%;background:${confTone};border-radius:6px`
    report.ruleChipStyle = `display:inline-block;font-family:'IBM Plex Mono',monospace;font-size:12px;font-weight:600;color:${confTone};background:${confTone}14;border:1px solid ${confTone}3a;border-radius:8px;padding:6px 11px`

    const lanes = this.LANES.map(ln => ({
      code: ln.code, name: ln.name, circulars: ln.circulars,
      headStyle: `display:flex;align-items:center;gap:12px;padding:16px 16px 14px;border-bottom:1px solid rgba(0,0,0,.06);background:${ln.tint}`,
      badgeStyle: `flex:none;min-width:48px;height:34px;padding:0 10px;border-radius:9px;background:${ln.color};color:#fff;display:flex;align-items:center;justify-content:center;font-family:'Space Grotesk',sans-serif;font-weight:700;font-size:13px;letter-spacing:.3px`,
      linkStyle: `display:flex;align-items:flex-start;gap:10px;text-decoration:none;padding:10px 10px;border-radius:9px;transition:background .15s`
    }))

    const categories = this.CATS.map((c) => ({
      name: c.name,
      dotStyle: `flex:none;width:10px;height:10px;border-radius:50%;background:${c.color}`,
      items: c.items.map(label => ({ label,
        style: `font-family:'Space Grotesk',sans-serif;font-weight:600;font-size:12.5px;color:#3a352e;background:#faf7f1;border:1px solid rgba(0,0,0,.12);border-radius:9px;padding:7px 13px` }))
    }))

    // regulatory-scan history — past runs of the latest scrape
    const scanOk = this.SCANS.filter(x => x.ok).length
    const scans = {
      runs: this.SCANS.length,
      okRate: Math.round((scanOk / this.SCANS.length) * 100) + '%',
      totalDocs: this.SCANS.reduce((a, x) => a + x.docs, 0),
      totalSize: '3.7 MB',
      rows: this.SCANS.map(x => ({
        src: x.src, time: x.time, docs: x.docs, size: x.size, note: x.note, ok: x.ok,
        statusText: x.ok ? 'Success' : 'Failure',
        clickable: x.ok, onClick: x.ok ? () => this.openCirc(x.src) : null,
        badgeStyle: `flex:none;min-width:44px;text-align:center;font-family:'Space Grotesk',sans-serif;font-weight:700;font-size:11px;letter-spacing:.3px;color:#fff;background:${x.color};padding:4px 9px;border-radius:7px`,
        statusStyle: `display:inline-flex;align-items:center;gap:6px;font-family:'IBM Plex Mono',monospace;font-size:11.5px;font-weight:600;color:${x.ok ? '#178a55' : '#c0392b'}`,
        dotStyle: `width:7px;height:7px;border-radius:50%;background:${x.ok ? '#1f9d63' : '#e0455a'}`
      }))
    }

    // ---------- final outcome (citations + agent outcomes, independent of live animation state) ----------
    const outcomeAgents = route.pick.map(id => {
      const idx = this.DET.findIndex(x => x.id === id)
      const d = this.DET[idx]
      const res = S.results[id] || { v: 'clear', note: '' }
      const tone = this.TONE[res.v]
      const score = this.scoreFor(res.v, idx)
      return {
        name: d.name, verdict: vLabel[res.v], note: res.note, scorePct: score + '%',
        rowStyle: `display:flex;align-items:center;gap:12px;padding:12px 15px;border-radius:11px;border:1px solid ${tone}33;background:${tone}0b`,
        scoreStyle: `flex:none;font-family:'Space Grotesk',sans-serif;font-weight:700;font-size:16px;color:${tone};min-width:44px;text-align:right`,
        verdictStyle: `flex:none;font-family:'IBM Plex Mono',monospace;font-size:10px;font-weight:700;color:${tone};background:${tone}14;border:1px solid ${tone}44;border-radius:7px;padding:3px 8px`
      }
    })
    const citations = this.citesFor(S).map(c => {
      if (typeof c === 'string') {
        return { code: 'CITATION', ref: '', note: c, url: '', badgeStyle: `flex:none;font-family:'Space Grotesk',sans-serif;font-weight:700;font-size:11px;color:#fff;background:#2f7fd6;border-radius:7px;padding:4px 9px;letter-spacing:.2px` }
      }
      return {
        code: (c.clause_no || c.rule_designation || c.chunk_id || 'UNKNOWN').toUpperCase(),
        ref: (c.citation || c.why_it_matters || ''),
        note: (c.clause || c.excerpt || ''),
        url: c.url || '',
        badgeStyle: `flex:none;font-family:'Space Grotesk',sans-serif;font-weight:700;font-size:11px;color:#fff;background:#2f7fd6;border-radius:7px;padding:4px 9px;letter-spacing:.2px`
      }
    })
    const outcome = {
      ref: S.reference, amount: S.amount.replace('.00', ''), sender: S.sender, receiver: S.receiver, channel: S.channel,
      tone: V.tone, vtone, title: V.title, line: V.line, icon: vIcon,
      confPct: cm.conf + '%', confLabel: cleared ? 'TYPOLOGY-MATCH CONFIDENCE' : 'SUSPICION CONFIDENCE',
      rule: cm.rule, summary: cm.summary,
      agents: outcomeAgents, citations,
      dispatched: route.pick.length, total: this.DET.length,
      hasPdf: report.hasPdf, pdfUrl: report.pdfUrl, noReport: !hasReport, headerTitle: report.headerTitle,
      reportFields: report.fields,
      bannerStyle: `display:flex;align-items:center;gap:14px;background:${V.tone === 'hit' ? 'rgba(224,69,90,.08)' : V.tone === 'flag' ? 'rgba(207,134,27,.08)' : 'rgba(31,157,99,.08)'};border:1.5px solid ${vtone}66;color:${vtone};padding:16px 20px;border-radius:16px;line-height:1.4`,
      iconStyle: `flex:none;width:36px;height:36px;border-radius:10px;background:${vtone};color:#fff;display:flex;align-items:center;justify-content:center;font-size:18px;font-weight:700`,
      confValStyle: `font-family:'Space Grotesk',sans-serif;font-weight:600;font-size:34px;line-height:1;letter-spacing:-1px;color:${vtone}`,
      ruleChipStyle: report.ruleChipStyle
    }

    // ---------- "why was this flagged" explanation (shared by compliance-check & outcome) ----------
    const primaryHitId = route.pick.find(id => (S.results[id] || {}).v === 'hit')
    const explainAgents = route.pick.map(id => {
      const i = this.DET.findIndex(x => x.id === id)
      const d = this.DET[i]
      const res = S.results[id] || { v: 'clear', note: '' }
      const tone = this.TONE[res.v]
      const score = this.scoreFor(res.v, i)
      let text = `The ${d.name} agent ${this.AGENT_DESC[id] || 'runs a compliance screen'}. On ${S.reference} it returned ${vLabel[res.v]} at ${score}% — ${res.note}. ${this.V_RATIONALE[res.v]}`
      if (id === primaryHitId && cm && cm.summary) text += ' ' + cm.summary
      return {
        name: d.name, verdict: vLabel[res.v], scorePct: score + '%', text,
        verdictStyle: `flex:none;font-family:'IBM Plex Mono',monospace;font-size:10px;font-weight:700;color:${tone};background:${tone}14;border:1px solid ${tone}44;border-radius:7px;padding:3px 8px`,
        dotStyle: `flex:none;width:9px;height:9px;border-radius:50%;background:${tone};margin-top:6px`
      }
    })
    const explain = {
      open: st.explainOpen, toggle: this.toggleExplain, vtone,
      btnLabel: st.explainOpen ? 'Hide explanation' : (cleared ? 'Explain the decision' : 'Why was this flagged?'),
      orchestrator: {
        headline: `The orchestrator agent triaged ${S.reference} and dispatched ${route.pick.length} of ${this.DET.length} screening agents.`,
        why: route.why,
        verdictLine: `${V.title} — ${V.line}`
      },
      agents: explainAgents,
      skipped: this.DET.filter(d => !route.pick.includes(d.id)).map(d => d.name),
      panelStyle: `margin-top:16px;background:#fffdfa;border:1px solid ${vtone}44;border-radius:16px;padding:20px 22px;box-shadow:0 2px 10px rgba(0,0,0,.05);animation:fadeUp .3s ease both`,
      btnStyle: `cursor:pointer;font-family:'Space Grotesk',sans-serif;font-weight:600;font-size:14px;color:${vtone};background:${vtone}12;border:1px solid ${vtone}55;padding:11px 20px;border-radius:12px;display:flex;align-items:center;gap:9px`
    }

    return {
      lanes, categories, scans,
      showOverview: st.view === 'overview', showRun: st.view === 'run',
      isFeed: st.step === 1, isIngest: st.step === 2, isDetect: st.step === 3, isReport: st.step === 4, isOutcome: st.step === 5,
      payment: { amount: S.amount, sender: S.sender, receiver: S.receiver, channel: S.channel, reference: S.reference },
      samples, samplePager, steps, agents, orch, explain,
      circModal: st.circModal, circLane: st.circLane,
      detectionEdges: this.detectionEdges(),
      graph, verdict, outcome,
      ingest: { fields: ingFields, done: st.ingestDone, lastScraped: st.lastScraped, hasUploads: st.userTxns.length > 0, uploadName: st.uploadName || 'uploaded.csv', uploadChips, queue, queueWaiting, verify },
      report
    }
  }

  // ---------- JSX views ----------
  renderOverview(V) {
    return (
      <div style={S('min-height:100vh;background:radial-gradient(120% 90% at 15% -10%, #f8f5f0 0%, #f6f3ee 45%, #efeae2 100%);background-color:#f5f1ea;color:#1a1712')}>
        <div style={S('display:flex;align-items:center;justify-content:space-between;padding:16px 26px;border-bottom:1px solid rgba(0,0,0,.08);position:sticky;top:0;background:rgba(245,241,234,.82);backdrop-filter:blur(10px);z-index:20')}>
          <div style={S('display:flex;align-items:center;gap:12px')}>
            <div style={S("width:34px;height:34px;border-radius:9px;background:linear-gradient(150deg,#5b8def,#7c5bef);color:#fff;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:13px;font-family:'Space Grotesk',sans-serif;letter-spacing:.5px")}>CP</div>
            <div style={S("font-family:'Space Grotesk',sans-serif;font-weight:600;font-size:16px")}>Compliance Pipeline</div>
          </div>
        </div>

        <div style={S('max-width:1080px;margin:0 auto;padding:28px 28px 36px')}>
          <div style={S("font-family:'IBM Plex Mono',monospace;font-size:11px;letter-spacing:2px;color:#b23a52;font-weight:600;margin-bottom:12px")}>DID YOU KNOW</div>
          <h1 style={S("font-family:'Space Grotesk',sans-serif;font-weight:600;font-size:clamp(24px,3vw,34px);line-height:1.05;letter-spacing:-1px;margin:0 0 10px;max-width:24ch")}>Compliance is getting costlier</h1>
          <p style={S('font-size:14px;line-height:1.5;color:#54504a;max-width:58ch;margin:0')}>Regulators keep raising the bar. The cost of falling behind — and of keeping up — is measured in crores.</p>

          <div style={S('display:flex;flex-direction:column;gap:10px;margin-top:20px;max-width:600px')}>
            {[['₹56 Cr', '#b23a52', 'in penalties were levied on Indian institutions for compliance lapses in a single year.'],
              ['₹12,500 Cr', '#1f8a5b', "is the sector's annual spend on RegTech and manual compliance review."],
              ['1,240+', '#7c5bef', 'new circulars are issued every year by RBI, SEBI & IRDAI — roughly 37 a month.']].map((f, i) => (
              <div key={i} style={S(`display:flex;align-items:baseline;gap:10px;border-left:2px solid ${f[1]};padding:1px 0 1px 12px`)}>
                <span style={S(`flex:none;font-family:'Space Grotesk',sans-serif;font-weight:700;font-size:14px;letter-spacing:-.2px;color:${f[1]}`)}>{f[0]}</span>
                <span style={S('font-size:12px;line-height:1.45;color:#6a655d')}>{f[2]}</span>
              </div>
            ))}
          </div>

          <div style={S("margin-top:48px;font-family:'IBM Plex Mono',monospace;font-size:11px;letter-spacing:2px;color:#2f7fd6;font-weight:600;margin-bottom:12px")}>REGULATORS &amp; CIRCULARS</div>
          <h2 style={S("font-family:'Space Grotesk',sans-serif;font-weight:600;font-size:clamp(20px,2.6vw,28px);line-height:1.05;letter-spacing:-.6px;margin:0 0 8px;max-width:20ch")}>Live regulatory feed</h2>
          <div style={S('display:flex;align-items:center;gap:7px;margin:0 0 14px')}><span style={S('width:8px;height:8px;border-radius:50%;background:#e0455a;box-shadow:0 0 0 3px rgba(224,69,90,.18)')}></span><span style={S("font-family:'IBM Plex Mono',monospace;font-size:11.5px;color:#8a8478")}>Last scraped: {V.ingest.lastScraped}</span></div>
          <p style={S('font-size:13.5px;line-height:1.5;color:#54504a;max-width:60ch;margin:0 0 24px')}>Each lane pulls the latest circulars straight from the source. Click any circular to open it on the regulator's site.</p>

          <div style={S('display:grid;grid-template-columns:repeat(3,1fr);gap:16px')}>
            {V.lanes.map((ln, li) => (
              <div key={li} style={S('background:#fffdfa;border:1px solid rgba(0,0,0,.07);border-radius:16px;overflow:hidden;box-shadow:0 1px 2px rgba(0,0,0,.04)')}>
                <div style={S(ln.headStyle)}>
                  <div style={S(ln.badgeStyle)}>{ln.code}</div>
                  <div>
                    <div style={S("font-family:'Space Grotesk',sans-serif;font-weight:600;font-size:15px;color:#1a1712")}>{ln.code}</div>
                    <div style={S('font-size:11.5px;color:#8a8478;line-height:1.3;margin-top:1px')}>{ln.name}</div>
                  </div>
                </div>
                <div style={S('padding:8px 10px 12px')}>
                  {ln.circulars.map((c, ci) => (
                    <Hover key={ci} tag="a" href={c.url} target="_blank" rel="noopener" style={ln.linkStyle} hoverStyle="background:rgba(0,0,0,.04)">
                      <span style={S('flex:1;font-size:13.5px;line-height:1.4;color:#3a352e')}>{c.title}</span>
                      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#a9a297" strokeWidth="2" style={S('flex:none;margin-top:2px')}><path d="M7 17L17 7M9 7h8v8" /></svg>
                    </Hover>
                  ))}
                </div>
              </div>
            ))}
          </div>

          <div style={S("margin-top:48px;font-family:'IBM Plex Mono',monospace;font-size:11px;letter-spacing:2px;color:#cf861b;font-weight:600;margin-bottom:12px")}>COVERAGE</div>
          <h2 style={S("font-family:'Space Grotesk',sans-serif;font-weight:600;font-size:clamp(20px,2.6vw,28px);line-height:1.05;letter-spacing:-.6px;margin:0 0 8px;max-width:20ch")}>Built for banking &amp; financial services</h2>
          <p style={S('font-size:13.5px;line-height:1.5;color:#54504a;max-width:60ch;margin:0 0 20px')}>Corporate, retail and NBFS compliance — covered end to end.</p>

          <div style={S('display:flex;flex-direction:column;gap:10px')}>
            {V.categories.map((cat, i) => (
              <div key={i} style={S('background:#fffdfa;border:1px solid rgba(0,0,0,.07);border-radius:12px;box-shadow:0 1px 2px rgba(0,0,0,.04);padding:13px 16px 15px')}>
                <div style={S('display:flex;align-items:center;gap:11px;margin-bottom:11px')}>
                  <span style={S(cat.dotStyle)}></span>
                  <span style={S("font-family:'Space Grotesk',sans-serif;font-weight:600;font-size:14px;color:#1a1712")}>{cat.name}</span>
                </div>
                <div style={S('display:flex;flex-wrap:wrap;gap:8px')}>
                  {cat.items.map((it, j) => (<span key={j} style={S(it.style)}>{it.label}</span>))}
                </div>
              </div>
            ))}
          </div>

          <div style={S("margin-top:48px;font-family:'IBM Plex Mono',monospace;font-size:11px;letter-spacing:2px;color:#1f8a5b;font-weight:600;margin-bottom:12px")}>OUR SOLUTION</div>
          <h2 style={S("font-family:'Space Grotesk',sans-serif;font-weight:600;font-size:clamp(22px,3vw,30px);line-height:1.05;letter-spacing:-.8px;margin:0 0 8px;max-width:20ch")}>Automated workflow</h2>
          <p style={S('font-size:13.5px;line-height:1.5;color:#54504a;max-width:60ch;margin:0 0 22px')}>Four steps. Every payment is read, checked by six inspectors at once, and reported inside the deadline — no human bottleneck.</p>

          <div style={S('display:grid;grid-template-columns:repeat(4,1fr);gap:14px')}>
            {[['STEP 1', '#2f7fd6', 'Gather circular feed', 'Pull the latest regulatory circulars in.'],
              ['STEP 2', '#2f7fd6', 'Transaction Queue', 'Queue the payment and read the record.'],
              ['STEP 3', '#cf861b', 'Compliance check', 'An orchestrator dispatches the relevant agents.'],
              ['STEP 4', '#1f9d63', 'Report & outcome', 'File the STR and surface the outcome with citations.']].map((s, i) => (
              <div key={i} style={S('background:#fffdfa;border:1px solid rgba(0,0,0,.07);border-radius:16px;padding:22px 20px;box-shadow:0 1px 2px rgba(0,0,0,.04)')}>
                <div style={S(`font-family:'IBM Plex Mono',monospace;font-size:11px;color:${s[1]};margin-bottom:14px`)}>{s[0]}</div>
                <div style={S("font-family:'Space Grotesk',sans-serif;font-weight:600;font-size:14px;margin-bottom:5px")}>{s[2]}</div>
                <div style={S('color:#6a655d;font-size:12px;line-height:1.45')}>{s[3]}</div>
              </div>
            ))}
          </div>

          <div style={S('display:flex;justify-content:center;margin-top:40px;margin-bottom:20px')}>
            <Hover onClick={this.launch} style="border:none;cursor:pointer;font-family:'Space Grotesk',sans-serif;font-weight:600;font-size:15px;color:#fff;background:linear-gradient(150deg,#5b8def,#7c5bef);padding:14px 28px;border-radius:13px;box-shadow:0 10px 26px rgba(92,110,239,.32);display:flex;align-items:center;gap:11px;transition:transform .18s,box-shadow .18s" hoverStyle="transform:translateY(-3px);box-shadow:0 16px 38px rgba(92,110,239,.42)">
              <span style={S('width:28px;height:28px;border-radius:50%;background:rgba(255,255,255,.2);display:flex;align-items:center;justify-content:center')}><svg width="13" height="13" viewBox="0 0 16 16" fill="#fff"><path d="M4 3l9 5-9 5z" /></svg></span>
              Get started
            </Hover>
          </div>
        </div>
      </div>
    )
  }

  renderRun(V) {
    return (
      <div style={S('min-height:100vh;background:radial-gradient(130% 100% at 80% -10%, #f8f5f0 0%, #f6f3ee 55%, #efeae2 100%);background-color:#f5f1ea;color:#1a1712')}>
        <div style={S('display:flex;align-items:center;justify-content:space-between;padding:14px 24px;border-bottom:1px solid rgba(0,0,0,.08);position:sticky;top:0;background:rgba(245,241,234,.85);backdrop-filter:blur(12px);z-index:30')}>
          <div style={S('display:flex;align-items:center;gap:12px')}>
            <div style={S("width:32px;height:32px;border-radius:9px;background:linear-gradient(150deg,#5b8def,#7c5bef);color:#fff;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:12px;font-family:'Space Grotesk',sans-serif")}>CP</div>
            <div style={S("font-family:'Space Grotesk',sans-serif;font-weight:600;font-size:15px")}>Compliance Pipeline</div>
          </div>
          <div style={S('display:flex;align-items:center;gap:10px')}>
            <Hover onClick={this.replay} style="cursor:pointer;background:rgba(0,0,0,.04);border:1px solid rgba(0,0,0,.1);color:#54504a;font-size:12.5px;padding:8px 14px;border-radius:9px;display:flex;align-items:center;gap:7px" hoverStyle="background:rgba(0,0,0,.07)"><svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2"><path d="M13 8a5 5 0 11-1.5-3.5M13 2v3h-3" /></svg>Replay this step</Hover>
            <Hover onClick={this.back} style="cursor:pointer;background:rgba(0,0,0,.04);border:1px solid rgba(0,0,0,.1);color:#54504a;font-size:13px;padding:8px 14px;border-radius:9px;display:flex;align-items:center;gap:7px" hoverStyle="background:rgba(0,0,0,.07)"><svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2"><path d="M10 3L5 8l5 5" /></svg>Back to overview</Hover>
          </div>
        </div>

        <div style={S('max-width:1120px;margin:0 auto;padding:26px 26px 80px')}>
          <div style={S('display:flex;align-items:stretch;gap:10px;margin-bottom:30px;flex-wrap:wrap')}>
            {V.steps.map((st, i) => (
              <button key={i} onClick={st.onClick} style={S(st.style)}>
                <span style={S('text-align:left')}>
                  <span style={S("display:block;font-family:'Space Grotesk',sans-serif;font-weight:600;font-size:14px")}>{st.name}</span>
                  <span style={S('display:block;font-size:11px;color:#8a8478;margin-top:2px')}>{st.sub}</span>
                </span>
              </button>
            ))}
          </div>

          {V.isFeed && this.renderFeed(V)}
          {V.isIngest && this.renderIngest(V)}
          {V.isDetect && this.renderDetect(V)}
          {V.isReport && this.renderReport(V)}
          {V.isOutcome && this.renderOutcome(V)}
        </div>
      </div>
    )
  }

  renderSamplePicker(V) {
    return (
      <div style={S('margin-bottom:22px')}>
        <div style={S("font-family:'IBM Plex Mono',monospace;font-size:11px;letter-spacing:2px;color:#8a8478;margin-bottom:8px")}>PICK A SAMPLE PAYMENT</div>
        <div style={S('display:flex;gap:8px;flex-wrap:wrap')}>
          {V.samples.map((s, i) => (
            <Hover key={i} onClick={s.onClick} style={s.style} hoverStyle="filter:brightness(.99)">
              <span style={S('font-weight:600;font-size:13px')}>{s.name}</span>
              <span style={S("font-family:'IBM Plex Mono',monospace;font-size:10.5px;opacity:.7;display:block;margin-top:2px")}>{s.tag}</span>
            </Hover>
          ))}
        </div>
        {V.samplePager.show && (
          <div style={S('display:flex;align-items:center;gap:10px;margin-top:10px')}>
            <button onClick={V.samplePager.prev} style={S(V.samplePager.prevStyle)}><svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2"><path d="M10 3L5 8l5 5" /></svg>Prev</button>
            <span style={S("font-family:'IBM Plex Mono',monospace;font-size:11px;color:#8a8478")}>{V.samplePager.label}</span>
            <button onClick={V.samplePager.next} style={S(V.samplePager.nextStyle)}>Next<svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2"><path d="M6 3l5 5-5 5" /></svg></button>
          </div>
        )}
      </div>
    )
  }

  renderExplain(V) {
    const e = V.explain
    return (
      <div style={S('margin-top:20px')}>
        <Hover onClick={e.toggle} style={e.btnStyle} hoverStyle="filter:brightness(.97)">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="12" cy="12" r="9" /><path d="M12 16v-4M12 8h.01" /></svg>{e.btnLabel}
        </Hover>
        {e.open && (
          <div style={S(e.panelStyle)}>
            <div style={S("font-family:'IBM Plex Mono',monospace;font-size:10.5px;letter-spacing:2px;color:#7c5bef;font-weight:600;margin-bottom:8px")}>ORCHESTRATOR AGENT</div>
            <div style={S('font-size:14px;color:#1a1712;font-weight:600;line-height:1.5;margin-bottom:6px')}>{e.orchestrator.headline}</div>
            <div style={S('font-size:13.5px;color:#5a544c;line-height:1.6;margin-bottom:10px')}>{e.orchestrator.why}</div>
            <div style={S(`font-size:13.5px;line-height:1.5;color:${e.vtone};font-weight:600;background:${e.vtone}12;border-left:3px solid ${e.vtone};border-radius:0 8px 8px 0;padding:9px 13px`)}>Disposition · {e.orchestrator.verdictLine}</div>

            <div style={S("font-family:'IBM Plex Mono',monospace;font-size:10.5px;letter-spacing:2px;color:#cf861b;font-weight:600;margin:20px 0 10px")}>SCREENING AGENTS</div>
            <div style={S('display:flex;flex-direction:column;gap:14px')}>
              {e.agents.map((a, i) => (
                <div key={i} style={S('display:flex;align-items:flex-start;gap:11px')}>
                  <span style={S(a.dotStyle)}></span>
                  <div style={S('flex:1;min-width:0')}>
                    <div style={S('display:flex;align-items:center;gap:9px;margin-bottom:4px;flex-wrap:wrap')}>
                      <span style={S("font-family:'Space Grotesk',sans-serif;font-weight:600;font-size:14px;color:#1a1712")}>{a.name}</span>
                      <span style={S(a.verdictStyle)}>{a.verdict}</span>
                      <span style={S("font-family:'IBM Plex Mono',monospace;font-size:11.5px;color:#8a8478")}>{a.scorePct}</span>
                    </div>
                    <div style={S('font-size:13px;color:#5a544c;line-height:1.6')}>{a.text}</div>
                  </div>
                </div>
              ))}
            </div>
            {e.skipped.length > 0 && (
              <div style={S("margin-top:16px;font-size:12.5px;color:#8a8478;line-height:1.5;border-top:1px solid rgba(0,0,0,.06);padding-top:12px")}><b style={S('color:#6a655d')}>Not dispatched:</b> {e.skipped.join(', ')} — the orchestrator judged these typologies not relevant to this transaction, so they were left idle.</div>
            )}
          </div>
        )}
      </div>
    )
  }

  renderCircModal(V) {
    if (!V.circModal) return null
    const lanes = V.circLane ? V.lanes.filter(l => l.code === V.circLane) : V.lanes
    const title = V.circLane ? V.circLane + ' circulars' : 'Scraped circulars'
    const sub = V.circLane ? 'Documents pulled in the successful ' + V.circLane + ' scan' : 'Last scraped · ' + V.ingest.lastScraped
    const single = lanes.length === 1
    return (
      <div onClick={this.closeCirc} style={S('position:fixed;inset:0;z-index:60;background:rgba(20,17,14,.5);backdrop-filter:blur(3px);display:flex;align-items:flex-start;justify-content:center;padding:40px 20px;overflow-y:auto')}>
        <div onClick={this.stopEvt} style={{ ...S('width:100%;background:#f7f4ee;border:1px solid rgba(0,0,0,.1);border-radius:18px;overflow:hidden;box-shadow:0 24px 60px rgba(0,0,0,.28);animation:fadeUp .3s ease both'), maxWidth: single ? '440px' : '820px' }}>
          <div style={S('display:flex;align-items:center;justify-content:space-between;gap:10px;padding:16px 20px;border-bottom:1px solid rgba(0,0,0,.08);background:#fffdfa')}>
            <div>
              <div style={S("font-family:'Space Grotesk',sans-serif;font-weight:600;font-size:17px;color:#1a1712")}>{title}</div>
              <div style={S("font-family:'IBM Plex Mono',monospace;font-size:11px;color:#8a8478;margin-top:2px")}>{sub}</div>
            </div>
            <button onClick={this.closeCirc} style={S('cursor:pointer;background:rgba(0,0,0,.08);border:none;width:30px;height:30px;border-radius:9px;color:#54504a;font-size:15px;display:flex;align-items:center;justify-content:center')}>✕</button>
          </div>
          <div style={{ ...S('padding:16px 20px 22px;display:grid;gap:14px'), gridTemplateColumns: single ? '1fr' : 'repeat(auto-fit,minmax(230px,1fr))' }}>
            {lanes.map((ln, li) => (
              <div key={li} style={S('background:#fffdfa;border:1px solid rgba(0,0,0,.08);border-radius:14px;overflow:hidden;box-shadow:0 1px 2px rgba(0,0,0,.04)')}>
                <div style={S(ln.headStyle)}>
                  <div style={S(ln.badgeStyle)}>{ln.code}</div>
                  <div>
                    <div style={S("font-family:'Space Grotesk',sans-serif;font-weight:600;font-size:14px;color:#1a1712")}>{ln.code}</div>
                    <div style={S('font-size:11px;color:#8a8478;line-height:1.3;margin-top:1px')}>{ln.name}</div>
                  </div>
                </div>
                <div style={S('padding:6px 8px 10px')}>
                  {ln.circulars.map((c, ci) => (
                    <Hover key={ci} tag="a" href={c.url} target="_blank" rel="noopener" style={ln.linkStyle} hoverStyle="background:rgba(0,0,0,.04)">
                      <span style={S('flex:1;font-size:13px;line-height:1.4;color:#3a352e')}>{c.title}</span>
                      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#a9a297" strokeWidth="2" style={S('flex:none;margin-top:2px')}><path d="M7 17L17 7M9 7h8v8" /></svg>
                    </Hover>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
    )
  }

  renderPaymentCard(V) {
    return (
      <div style={S('background:#fffdfa;border:1px solid rgba(0,0,0,.08);border-radius:16px;padding:18px 22px;margin-bottom:22px;box-shadow:0 1px 2px rgba(0,0,0,.04)')}>
        <div style={S("font-family:'IBM Plex Mono',monospace;font-size:11px;letter-spacing:2px;color:#8a8478;margin-bottom:8px")}>PAYMENT UNDER REVIEW</div>
        <div style={S("font-size:21px;font-family:'Space Grotesk',sans-serif;line-height:1.3")}><b>{V.payment.amount}</b> <span style={S('color:#8a8478')}>sent by</span> <b>{V.payment.sender}</b> <span style={S('color:#8a8478')}>to</span> <b>{V.payment.receiver}</b></div>
        <div style={S("color:#8a8478;font-size:13px;margin-top:6px;font-family:'IBM Plex Mono',monospace")}>{V.payment.channel} · reference {V.payment.reference}</div>
      </div>
    )
  }

  renderFeed(V) {
    const sc = V.scans
    return (
      <div style={S('animation:fadeUp .4s ease both')}>
        <div style={S('display:flex;align-items:flex-start;justify-content:space-between;flex-wrap:wrap;gap:14px;margin-bottom:6px')}>
          <div style={S('display:flex;align-items:center;gap:12px')}>
            <span style={S("font-family:'Space Grotesk',sans-serif;font-weight:600;font-size:22px")}>Gather circular feed</span>
          </div>
          <div style={S("display:flex;align-items:center;gap:9px;background:#fffdfa;border:1px solid rgba(47,127,214,.3);border-radius:20px;padding:7px 14px;font-family:'IBM Plex Mono',monospace;font-size:11.5px;color:#3a5f8a")}>
            <span style={S('width:9px;height:9px;border-radius:50%;background:#2f7fd6;animation:redGlow 1.4s infinite')}></span>
            Last scraped · {V.ingest.lastScraped}
          </div>
        </div>
        <div style={S('border-left:3px solid #2f7fd6;padding-left:16px;color:#54504a;font-size:15px;line-height:1.55;max-width:74ch;margin-bottom:22px')}>Every few hours the pipeline scrapes each regulator's portal for new circulars. These are the <b style={S('color:#1a1712')}>latest scan runs</b> — what was pulled, how much, and whether it succeeded.</div>

        <div style={S('display:flex;flex-wrap:wrap;gap:10px;margin-bottom:16px')}>
          {[['Success rate', sc.okRate, '#1f8a5b'], ['Runs logged', String(sc.runs), '#2f7fd6'], ['Documents pulled', String(sc.totalDocs), '#7c5bef'], ['Data fetched', sc.totalSize, '#cf861b']].map((m, i) => (
            <div key={i} style={S(`flex:1;min-width:150px;background:#fffdfa;border:1px solid rgba(0,0,0,.07);border-left:3px solid ${m[2]};border-radius:12px;padding:12px 15px;box-shadow:0 1px 2px rgba(0,0,0,.04)`)}>
              <div style={S("font-family:'IBM Plex Mono',monospace;font-size:10.5px;letter-spacing:1px;color:#8a8478;margin-bottom:6px")}>{m[0].toUpperCase()}</div>
              <div style={S(`font-family:'Space Grotesk',sans-serif;font-weight:700;font-size:22px;letter-spacing:-.5px;color:${m[2]}`)}>{m[1]}</div>
            </div>
          ))}
        </div>

        <div style={S('background:#fffdfa;border:1px solid rgba(0,0,0,.07);border-radius:16px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.05)')}>
          <div style={S("display:grid;grid-template-columns:70px 1fr 1.6fr 80px 80px 120px;gap:12px;align-items:center;padding:11px 18px;border-bottom:1px solid rgba(0,0,0,.06);background:rgba(0,0,0,.02);font-family:'IBM Plex Mono',monospace;font-size:10px;letter-spacing:1px;color:#8a8478")}>
            <span>SOURCE</span><span>TIMESTAMP</span><span>DETAIL</span><span style={S('text-align:right')}>DOCS</span><span style={S('text-align:right')}>SIZE</span><span style={S('text-align:right')}>STATUS</span>
          </div>
          {sc.rows.map((r, i) => {
            const rowStyle = `display:grid;grid-template-columns:70px 1fr 1.6fr 80px 80px 120px;gap:12px;align-items:center;padding:13px 18px;animation:queueIn .4s ease both;animation-delay:${(i * 0.07).toFixed(2)}s;${i < sc.rows.length - 1 ? 'border-bottom:1px solid rgba(0,0,0,.05)' : ''}`
            const cells = (
              <>
                <span style={S(r.badgeStyle)}>{r.src}</span>
                <span style={S("font-family:'IBM Plex Mono',monospace;font-size:12px;color:#54504a")}>{r.time}</span>
                <span style={S('font-size:12.5px;color:#6a655d;line-height:1.35')}>{r.note}</span>
                <span style={S("font-family:'IBM Plex Mono',monospace;font-size:13px;color:#1a1712;font-weight:600;text-align:right")}>{r.docs}</span>
                <span style={S("font-family:'IBM Plex Mono',monospace;font-size:13px;color:#1a1712;font-weight:600;text-align:right")}>{r.size}</span>
                <span style={S('display:flex;align-items:center;justify-content:flex-end;gap:6px')}>
                  <span style={S(r.statusStyle)}><span style={S(r.dotStyle)}></span>{r.statusText}</span>
                  {r.clickable && (<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#178a55" strokeWidth="2" style={S('flex:none')}><path d="M7 17L17 7M9 7h8v8" /></svg>)}
                </span>
              </>
            )
            return r.clickable
              ? (<Hover key={i} tag="div" onClick={r.onClick} title="Open the circulars from this scan" style={`${rowStyle};cursor:pointer`} hoverStyle="background:rgba(31,157,99,.06)">{cells}</Hover>)
              : (<div key={i} style={S(rowStyle)}>{cells}</div>)
          })}
        </div>

        <div style={S('margin-top:24px')}>
          <Hover onClick={this.toQueue} style="cursor:pointer;border:none;font-family:'Space Grotesk',sans-serif;font-weight:600;font-size:15px;color:#fff;background:linear-gradient(150deg,#5b8def,#7c5bef);padding:14px 28px;border-radius:13px;display:flex;align-items:center;gap:11px;box-shadow:0 10px 26px rgba(92,110,239,.32)" hoverStyle="transform:translateY(-2px);box-shadow:0 16px 38px rgba(92,110,239,.42)"><span style={S('width:26px;height:26px;border-radius:50%;background:rgba(255,255,255,.2);display:flex;align-items:center;justify-content:center')}><svg width="12" height="12" viewBox="0 0 16 16" fill="#fff"><path d="M4 3l9 5-9 5z" /></svg></span>Trigger workflow</Hover>
        </div>
      </div>
    )
  }

  renderIngest(V) {
    const q = V.ingest.queue
    return (
      <div style={S('animation:fadeUp .4s ease both')}>
        <div style={S('display:flex;align-items:flex-start;justify-content:space-between;flex-wrap:wrap;gap:14px;margin-bottom:6px')}>
          <div style={S('display:flex;align-items:center;gap:12px')}>
            <span style={S("font-family:'Space Grotesk',sans-serif;font-weight:600;font-size:22px")}>Transaction Queue</span>
          </div>
          <div style={S('display:flex;align-items:center;gap:10px;flex-wrap:wrap')}>
            <div style={S("display:flex;align-items:center;gap:9px;background:#fffdfa;border:1px solid rgba(47,127,214,.3);border-radius:20px;padding:7px 14px;font-family:'IBM Plex Mono',monospace;font-size:11.5px;color:#3a5f8a")}>
              <span style={S('width:9px;height:9px;border-radius:50%;background:#2f7fd6;animation:redGlow 1.4s infinite')}></span>
              Queue live · {V.ingest.lastScraped}
            </div>
            <Hover onClick={this.addTxn} style="cursor:pointer;font-family:'Space Grotesk',sans-serif;font-weight:600;font-size:13px;color:#fff;background:linear-gradient(150deg,#4a90e2,#2f6fd6);border:none;padding:9px 16px;border-radius:10px;display:flex;align-items:center;gap:7px;box-shadow:0 6px 16px rgba(47,111,214,.25)" hoverStyle="filter:brightness(1.06)"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="2.4"><path d="M12 5v14M5 12h14" /></svg>Add transaction</Hover>
            <input type="file" accept=".csv,.xlsx,.xls,text/csv" ref={this.fileRef} onChange={this.onFile} style={S('display:none')} />
          </div>
        </div>
        {/* incoming queue — shown first, at the top */}
        <div style={S("display:flex;align-items:center;gap:8px;font-family:'IBM Plex Mono',monospace;font-size:10.5px;letter-spacing:1.5px;color:#8a8478;margin-bottom:12px")}><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#2f7fd6" strokeWidth="2"><path d="M4 7h16M4 12h16M4 17h10" /></svg>INCOMING QUEUE{V.ingest.queueWaiting > 0 ? ' · ' + V.ingest.queueWaiting + ' waiting' : ''}</div>
        <div style={S('display:flex;flex-direction:column;gap:10px')}>
          {q.map((it, i) => (!it.arrived ? null : (it.active ? (
            <div key={i} style={S('border:1px solid rgba(47,127,214,.5);border-radius:16px;background:#ffffff;box-shadow:0 10px 32px rgba(0,0,0,.08),0 2px 6px rgba(0,0,0,.05);overflow:hidden;animation:queueIn .42s ease both')}>
              <div style={S('display:flex;align-items:center;gap:12px;padding:13px 18px;background:rgba(47,127,214,.06);border-bottom:1px solid rgba(0,0,0,.06)')}>
                <span style={S(it.badgeStyle)}>{it.statusLabel}</span>
                <div style={S('flex:1;min-width:0')}>
                  <div style={S("font-family:'Space Grotesk',sans-serif;font-weight:600;font-size:14px;color:#1a1712;white-space:nowrap;overflow:hidden;text-overflow:ellipsis")}>{it.from} <span style={S('color:#a9a297')}>→</span> {it.to}</div>
                  <div style={S("font-family:'IBM Plex Mono',monospace;font-size:10.5px;color:#8a8478;margin-top:2px")}>{it.ref} · {it.channel}</div>
                </div>
                <span style={S("flex:none;font-family:'IBM Plex Mono',monospace;font-size:10px;color:#7a4a50;background:rgba(224,69,90,.08);border:1px solid rgba(224,69,90,.3);padding:5px 10px;border-radius:20px;display:flex;align-items:center;gap:6px")}><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="#c0392b" strokeWidth="2"><rect x="5" y="11" width="14" height="9" rx="2" /><path d="M8 11V8a4 4 0 018 0v3" /></svg>PII masked</span>
                <span style={S("flex:none;font-family:'Space Grotesk',sans-serif;font-weight:700;font-size:15px;color:#1a1712")}>{it.amount}</span>
              </div>
              {/* the processed transaction's details — a single scrollable row */}
              <div style={S('display:flex;align-items:stretch;overflow-x:auto')}>
                {V.ingest.fields.map((f, j) => (
                  <div key={j} style={{ ...S(`flex:1 0 auto;min-width:118px;padding:11px 14px;transition:opacity .3s;opacity:${f.shown ? 1 : 0.32}`), borderRight: j < V.ingest.fields.length - 1 ? '1px solid rgba(0,0,0,.06)' : 'none' }}>
                    <div style={S("font-family:'IBM Plex Mono',monospace;font-size:9px;letter-spacing:.4px;color:#8a8478;margin-bottom:5px;display:flex;align-items:center;gap:4px;white-space:nowrap")}>{f.key}{f.masked && (<svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="#b8555f" strokeWidth="2.4"><rect x="5" y="11" width="14" height="9" rx="2" /><path d="M8 11V8a4 4 0 018 0v3" /></svg>)}</div>
                    <div style={{ ...S("font-family:'IBM Plex Mono',monospace;font-size:12.5px;font-weight:600;white-space:nowrap"), color: f.masked ? '#b8555f' : '#1a1712' }}>{f.val}</div>
                  </div>
                ))}
              </div>
            </div>
          ) : (
            <div key={i} style={S(it.style)}>
              <span style={S(it.badgeStyle)}>{it.statusLabel}</span>
              <div style={S('flex:1;min-width:0')}>
                <div style={S("font-family:'Space Grotesk',sans-serif;font-weight:600;font-size:13px;color:#8a8478;display:flex;align-items:center;gap:7px")}><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#a9a297" strokeWidth="2"><rect x="5" y="11" width="14" height="9" rx="2" /><path d="M8 11V8a4 4 0 018 0v3" /></svg>Sealed until pickup</div>
              </div>
              <span style={S("flex:none;font-family:'IBM Plex Mono',monospace;font-size:11px;color:#a9a297")}>{it.maskedRef}</span>
            </div>
          ))))}
        </div>

        <div style={S('border-left:3px solid #2f7fd6;padding-left:16px;color:#54504a;font-size:13.5px;line-height:1.5;max-width:74ch;margin:20px 0 22px')}>Payments <b style={S('color:#1a1712')}>stream into the queue</b> and are picked up one at a time. Only the transaction currently being <b style={S('color:#1a1712')}>processed</b> is opened — its record shows in a single row with sensitive details <b style={S('color:#1a1712')}>masked</b>. The rest stay sealed until their turn.</div>

        {V.ingest.hasUploads && (
          <TransactionTable 
            rows={this.state.userTxns} 
            selected={this.state.sample - this.SAMPLES.length} 
            onSelect={(i) => this.pickSample(this.SAMPLES.length + i)} 
            totalCount={this.state.userTxns.length} 
          />
        )}

        {this.renderSamplePicker(V)}

        <div style={S('display:flex;align-items:center;gap:14px;margin-top:8px;flex-wrap:wrap')}>
          {V.ingest.done && (
            <>
              <div style={S('display:flex;align-items:center;gap:9px;background:rgba(31,157,99,.12);border:1px solid rgba(31,157,99,.4);color:#178a55;font-size:14px;font-weight:600;padding:11px 16px;border-radius:11px;animation:popIn .4s ease both')}><span style={S('width:20px;height:20px;border-radius:50%;background:#1f9d63;color:#fff;display:flex;align-items:center;justify-content:center;font-size:12px')}>✓</span>Record read — {V.ingest.fields.length} fields captured</div>
              <Hover onClick={this.toDetect} style="cursor:pointer;border:none;font-family:'Space Grotesk',sans-serif;font-weight:600;font-size:15px;color:#3a2600;background:linear-gradient(150deg,#f5c06e,#e0972a);padding:13px 24px;border-radius:12px;display:flex;align-items:center;gap:9px" hoverStyle="filter:brightness(1.05)">Send to the compliance check<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="#3a2600" strokeWidth="2"><path d="M6 3l5 5-5 5" /></svg></Hover>
            </>
          )}
        </div>
      </div>
    )
  }

  renderDetect(V) {
    const o = V.orch
    return (
      <div style={S('animation:fadeUp .4s ease both')}>
        <div style={S('display:flex;align-items:center;gap:12px;margin-bottom:6px')}>
          <span style={S("font-family:'Space Grotesk',sans-serif;font-weight:600;font-size:22px")}>Compliance check</span>
          <span style={S('color:#8a8478;font-size:13px')}>orchestrated agents · dynamic routing</span>
          <span style={S("margin-left:auto;font-family:'IBM Plex Mono',monospace;font-size:11px;color:#cf861b;display:flex;align-items:center;gap:6px")}><span style={S('width:7px;height:7px;border-radius:50%;background:#cf861b;animation:glowPulse 1s infinite')}></span>live</span>
        </div>
        <div style={S('border-left:3px solid #cf861b;padding-left:16px;color:#54504a;font-size:16px;line-height:1.55;max-width:80ch;margin-bottom:22px')}>An <b style={S('color:#1a1712')}>orchestrator agent</b> reads the record first and <b style={S('color:#1a1712')}>decides which screening agents are relevant</b> — it does not run all of them. Each dispatched agent applies its own AML/CFT typology and returns a <b style={S('color:#1a1712')}>score, verdict and preliminary indicator</b>.</div>

        {this.renderPaymentCard(V)}

        {o.show && (
          <div style={S(o.nodeStyle)}>
            <div style={S('display:flex;align-items:center;gap:11px;margin-bottom:10px')}>
              <span style={S('width:34px;height:34px;border-radius:10px;background:linear-gradient(150deg,#8f7bef,#6a4fe0);color:#fff;display:flex;align-items:center;justify-content:center')}><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="2"><circle cx="12" cy="12" r="3" /><path d="M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M19 5l-2 2M7 17l-2 2" /></svg></span>
              <div style={S('flex:1')}>
                <div style={S("font-family:'Space Grotesk',sans-serif;font-weight:600;font-size:16px;color:#1a1712")}>Orchestrator agent</div>
                <div style={S("font-family:'IBM Plex Mono',monospace;font-size:11px;color:#7c5bef;margin-top:1px")}>{o.running ? 'triaging & dispatching…' : 'dispatch complete'}</div>
              </div>
              <span style={S("font-family:'IBM Plex Mono',monospace;font-size:11px;font-weight:600;color:#6a4fe0;background:rgba(124,91,239,.1);border:1px solid rgba(124,91,239,.3);border-radius:8px;padding:5px 11px")}>{o.dispatched} of {o.total} agents invoked</span>
            </div>
            <div style={S('color:#5a544c;font-size:13.5px;line-height:1.55;margin-bottom:11px')}>{o.why}</div>
            <div style={S('display:flex;flex-wrap:wrap;gap:7px')}>
              {o.chips.map((c, i) => (
                <span key={i} style={S("font-family:'IBM Plex Mono',monospace;font-size:11px;font-weight:600;color:#3a352e;background:#f3f0fb;border:1px solid rgba(124,91,239,.25);border-radius:8px;padding:5px 10px;display:flex;align-items:center;gap:6px")}><span style={S('width:6px;height:6px;border-radius:50%;background:#7c5bef')}></span>{c}</span>
              ))}
            </div>
          </div>
        )}

        <div style={S('background:#ffffff;border:1px solid rgba(0,0,0,.08);border-radius:18px;padding:10px;position:relative;box-shadow:0 1px 3px rgba(0,0,0,.05);margin-top:16px')}>
          <div style={S('position:relative;width:100%;aspect-ratio:100/78;min-height:560px')}>
            {V.detectionEdges}
            {o.show && (
              <div style={S(o.rootStyle)}>
                <div style={S('display:flex;align-items:center;justify-content:center;gap:9px')}>
                  <span style={S('width:26px;height:26px;border-radius:8px;background:linear-gradient(150deg,#8f7bef,#6a4fe0);color:#fff;display:flex;align-items:center;justify-content:center')}><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="2"><circle cx="12" cy="12" r="3" /><path d="M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M19 5l-2 2M7 17l-2 2" /></svg></span>
                  <span style={S("font-family:'Space Grotesk',sans-serif;font-weight:600;font-size:16px;color:#1a1712")}>Orchestrator</span>
                </div>
                <div style={S("font-size:11px;color:#6a4fe0;margin-top:3px;font-family:'IBM Plex Mono',monospace")}>{o.dispatched} of {o.total} agents · {o.running ? 'dispatching…' : 'complete'}</div>
              </div>
            )}
            {V.agents.map((a, i) => (
              <div key={i} style={S(a.treeCardStyle)}>
                <div style={S('display:flex;align-items:flex-start;justify-content:space-between;gap:6px;margin-bottom:4px')}>
                  <span style={S("font-family:'Space Grotesk',sans-serif;font-weight:600;font-size:13px;color:#1a1712;line-height:1.15")}>{a.name}</span>
                  {a.resolved && <span style={S(a.badgeStyle)}>{a.badge}</span>}
                </div>
                <div style={S("font-family:'IBM Plex Mono',monospace;font-size:9px;letter-spacing:.5px;color:#a9a297;margin-bottom:8px")}>SCREENING AGENT</div>
                {a.skipped && (
                  <div style={S("font-family:'IBM Plex Mono',monospace;font-size:10.5px;color:#a9a297;line-height:1.35;display:flex;align-items:center;gap:5px")}><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="#c3bcae" strokeWidth="2" style={S('flex:none')}><path d="M18 6L6 18M6 6l12 12" /></svg>Not invoked</div>
                )}
                {a.running && (
                  <div style={S("font-family:'IBM Plex Mono',monospace;font-size:10.5px;color:#cf861b;display:flex;align-items:center;gap:2px")}>analysing<span style={S('animation:dotBlink 1.2s infinite')}>.</span><span style={S('animation:dotBlink 1.2s .2s infinite')}>.</span><span style={S('animation:dotBlink 1.2s .4s infinite')}>.</span></div>
                )}
                {(a.invoked && !a.running && !a.resolved) && (
                  <div style={S("font-family:'IBM Plex Mono',monospace;font-size:10.5px;color:#b5aea2")}>queued…</div>
                )}
                {a.resolved && (
                  <div style={S('animation:fadeUp .35s ease both')}>
                    <div style={S('display:flex;align-items:center;justify-content:space-between;gap:6px;margin-bottom:6px')}>
                      <span style={S(a.treeScoreStyle)}>{a.scorePct}</span>
                      <span style={S(a.verdictStyle)}>{a.verdict}</span>
                    </div>
                    <div style={S('height:5px;background:rgba(0,0,0,.07);border-radius:5px;overflow:hidden;margin-bottom:7px')}><div style={S(a.barStyle)}></div></div>
                    <div style={S(a.noteStyle)}>{a.note}</div>
                  </div>
                )}
              </div>
            ))}
            {V.verdict.show && (
              <div style={S(V.verdict.nodeStyle)}>
                <div style={S("font-family:'IBM Plex Mono',monospace;font-size:9.5px;letter-spacing:1.5px;opacity:.8")}>VERDICT</div>
                <div style={S("font-family:'Space Grotesk',sans-serif;font-weight:700;font-size:15px;margin-top:3px")}>{V.verdict.title}</div>
              </div>
            )}
          </div>
        </div>

        {V.graph.show && (
          <div style={S(V.graph.wrapStyle)}>
            <div style={S('display:flex;align-items:center;gap:10px;margin-bottom:8px')}>
              <span style={S(V.graph.dotStyle)}></span>
              <span style={S(V.graph.titleStyle)}>{V.graph.title}</span>
              <span style={S(V.graph.tagStyle)}>{V.graph.tag}</span>
            </div>
            <div style={S('color:#5a544c;font-size:14px;line-height:1.55;max-width:92ch;margin-bottom:12px')}>{V.graph.desc}</div>
            <div style={S('position:relative;width:100%;aspect-ratio:100/54;min-height:300px')}>
              {V.graph.svg}
            </div>
          </div>
        )}

        <div style={S('margin-top:22px;display:flex;align-items:center;gap:14px;flex-wrap:wrap')}>
          {V.verdict.show && (
            <>
              <div style={S(V.verdict.bannerStyle)}>
                <span style={S(V.verdict.iconStyle)}>{V.verdict.icon}</span>
                <span><b style={S('font-size:15px')}>{V.verdict.title}</b><br /><span style={S('font-size:13px;opacity:.85')}>{V.verdict.line}</span></span>
              </div>
              <Hover onClick={this.toReport} style="cursor:pointer;border:none;font-family:'Space Grotesk',sans-serif;font-weight:600;font-size:15px;color:#fff;background:linear-gradient(150deg,#2fb377,#1f9d63);padding:13px 24px;border-radius:12px;display:flex;align-items:center;gap:9px" hoverStyle="filter:brightness(1.06)">Generate the report<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="#fff" strokeWidth="2"><path d="M6 3l5 5-5 5" /></svg></Hover>
            </>
          )}
        </div>

        {V.verdict.show && this.renderExplain(V)}
      </div>
    )
  }

  renderReport(V) {
    const r = V.report
    return (
      <div style={S('animation:fadeUp .4s ease both')}>
        <div style={S('display:flex;align-items:center;gap:12px;margin-bottom:6px')}>
          <span style={S("font-family:'Space Grotesk',sans-serif;font-weight:600;font-size:22px")}>Report generation</span>
          <span style={S("margin-left:auto;font-family:'IBM Plex Mono',monospace;font-size:11px;color:#1f9d63;display:flex;align-items:center;gap:6px")}><span style={S('width:7px;height:7px;border-radius:50%;background:#1f9d63;animation:glowPulse 1.2s infinite')}></span>filing</span>
        </div>
        <div style={S('border-left:3px solid #1f9d63;padding-left:16px;color:#54504a;font-size:16px;line-height:1.55;max-width:74ch;margin-bottom:26px')}>The verdict is turned into a proper regulatory report and filed automatically — <b style={S('color:#1a1712')}>inside the deadline the manual team kept missing.</b></div>

        <div style={S('display:grid;grid-template-columns:.85fr 1.35fr;gap:16px;max-width:640px;margin-bottom:22px')}>
          <div style={S(r.confBoxStyle)}>
            <div style={S("font-family:'IBM Plex Mono',monospace;font-size:10px;letter-spacing:1.5px;color:#8a8478;margin-bottom:12px")}>{r.confLabelUp}</div>
            <div style={S(r.confValStyle)}>{r.confPct}</div>
            <div style={S('height:7px;background:rgba(0,0,0,.08);border-radius:6px;overflow:hidden;margin-top:14px')}>
              <div style={S(r.confBarStyle)}></div>
            </div>
            <div style={S('font-size:12px;color:#6a655d;margin-top:10px;line-height:1.4')}>{r.confSub}</div>
          </div>
          <div style={S('background:#fffdfa;border:1px solid rgba(0,0,0,.08);border-radius:16px;padding:18px 20px;box-shadow:0 1px 3px rgba(0,0,0,.05)')}>
            <div style={S("font-family:'IBM Plex Mono',monospace;font-size:10px;letter-spacing:1.5px;color:#8a8478;margin-bottom:12px")}>RULE TRIGGERED &amp; SUMMARY</div>
            <div style={S(r.ruleChipStyle)}>{r.rule}</div>
            <div style={S('font-size:14px;color:#3a352e;line-height:1.55;margin-top:12px')}>{r.summary}</div>
          </div>
        </div>

        <div style={S('max-width:640px;background:#fffdfa;border:1px solid rgba(0,0,0,.08);border-radius:16px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.05)')}>
          <div style={S(r.headerStyle)}>
            <span style={S("font-family:'Space Grotesk',sans-serif;font-weight:600;font-size:16px")}>{r.headerTitle}</span>
            <span style={S("font-family:'IBM Plex Mono',monospace;font-size:11px;opacity:.85")}>{r.headerTag}</span>
          </div>
          <div style={S('padding:6px 4px')}>
            {r.fields.map((f, i) => (
              <div key={i} style={S(f.rowStyle)}>
                <span style={S("font-family:'IBM Plex Mono',monospace;font-size:11.5px;color:#8a8478;width:34%")}>{f.key}</span>
                <span style={S('font-size:13.5px;color:#1a1712;font-weight:500;flex:1')}>{f.val}</span>
              </div>
            ))}
          </div>
          <div style={S('padding:16px 18px;border-top:1px solid rgba(0,0,0,.06)')}>
            <div style={S('display:flex;align-items:center;justify-content:space-between;font-size:12px;color:#8a8478;margin-bottom:8px')}><span>Filing to {r.filingTarget}</span><span style={S("font-family:'IBM Plex Mono',monospace")}>{r.progressLabel}</span></div>
            <div style={S('height:9px;background:rgba(0,0,0,.08);border-radius:6px;overflow:hidden')}>
              <div style={{ width: r.progressPct, height: '100%', background: 'linear-gradient(90deg,#1f9d63,#2fb377)', borderRadius: '6px', transition: 'width .35s ease' }}></div>
            </div>
          </div>
        </div>

        {r.modalOpen && (
          <div style={S('max-width:720px;margin-top:16px')}>
            <div style={S('background:#ffffff;border:1px solid rgba(0,0,0,.1);border-radius:16px;overflow:hidden;box-shadow:0 12px 34px rgba(0,0,0,.1);animation:fadeUp .3s ease both')}>
              <div style={S(r.headerStyle)}>
                <span style={S("font-family:'Space Grotesk',sans-serif;font-weight:600;font-size:17px")}>{r.headerTitle}</span>
                <button onClick={r.closeReport} style={S('cursor:pointer;background:rgba(0,0,0,.08);border:none;width:28px;height:28px;border-radius:8px;color:#54504a;font-size:14px;display:flex;align-items:center;justify-content:center')}>✕</button>
              </div>
              <div style={S('padding:22px 26px 24px')}>
                <div style={S('display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin-bottom:16px')}>
                  <div style={S("font-family:'Space Grotesk',sans-serif;font-weight:700;font-size:24px;color:#1a1712")}>{r.amt}</div>
                  <div style={S("font-family:'IBM Plex Mono',monospace;font-size:12px;color:#8a8478")}>{r.ref}</div>
                </div>
                {r.hasPdf && (
                  <>
                    <div style={S(r.ruleChipStyle)}>{r.rule}</div>
                    <div style={S('border:1px solid rgba(0,0,0,.1);border-radius:12px;overflow:hidden;margin:14px 0 12px;background:#f3f0ea')}>
                      <iframe src={r.pdfUrl} title="STR PDF" style={S('width:100%;height:560px;border:none;display:block')}></iframe>
                    </div>
                    <a href={r.pdfUrl} target="_blank" rel="noopener" style={S("display:inline-flex;align-items:center;gap:7px;font-size:13px;font-weight:600;color:#2f6fd6;text-decoration:none;font-family:'Space Grotesk',sans-serif")}>Open PDF in new tab<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M7 17L17 7M9 7h8v8" /></svg></a>
                  </>
                )}
                {r.noPdf && (
                  <>
                    <div style={S(r.ruleChipStyle)}>{r.rule}</div>
                    <div style={S('font-size:14.5px;color:#3a352e;line-height:1.6;margin:14px 0 18px')}>{r.summary}</div>
                    <div style={S('border:1px solid rgba(0,0,0,.08);border-radius:12px;overflow:hidden;margin-bottom:18px')}>
                      {r.fields.map((f, i) => (
                        <div key={i} style={S(f.rowStyle)}>
                          <span style={S("font-family:'IBM Plex Mono',monospace;font-size:11.5px;color:#8a8478;width:34%")}>{f.key}</span>
                          <span style={S('font-size:13.5px;color:#1a1712;font-weight:500;flex:1')}>{f.val}</span>
                        </div>
                      ))}
                    </div>
                    <div style={S('display:flex;align-items:center;gap:16px;padding:14px 16px;background:rgba(0,0,0,.02);border:1px solid rgba(0,0,0,.06);border-radius:12px')}>
                      <div style={S(r.confValStyle)}>{r.confPct}</div>
                      <div>
                        <div style={S("font-family:'Space Grotesk',sans-serif;font-weight:600;font-size:14px;color:#1a1712")}>{r.confLabelUp}</div>
                        <div style={S('font-size:12px;color:#6a655d;margin-top:2px')}>{r.confSub}</div>
                      </div>
                    </div>
                  </>
                )}
                <div style={S("display:flex;align-items:center;justify-content:space-between;margin-top:18px;padding-top:14px;border-top:1px solid rgba(0,0,0,.07);font-family:'IBM Plex Mono',monospace;font-size:11px;color:#8a8478")}>
                  <span>Compliance Pipeline · {V.ingest.lastScraped}</span>
                  <span>Auto-signed · digitally sealed</span>
                </div>
              </div>
            </div>
          </div>
        )}

        {r.filed && (
          <>
            <div style={S('margin-top:22px;display:flex;align-items:center;gap:14px;flex-wrap:wrap;animation:popIn .45s ease both')}>
              <div style={S(r.doneStyle)}><span style={S(r.doneIconStyle)}>{r.doneIcon}</span>{r.doneText}</div>
              <div style={S('font-size:13px;color:#8a8478')}>Manual layer: up to <b style={S('color:#c0392b')}>7 days</b>. Pipeline: <b style={S('color:#178a55')}>{r.speed}</b>.</div>
            </div>
            <div style={S('margin-top:20px;display:flex;gap:12px;flex-wrap:wrap')}>
              <Hover onClick={this.toOutcome} style="cursor:pointer;border:none;font-family:'Space Grotesk',sans-serif;font-weight:600;font-size:14px;color:#fff;background:linear-gradient(150deg,#5b8def,#7c5bef);padding:12px 22px;border-radius:12px;display:flex;align-items:center;gap:8px;box-shadow:0 8px 20px rgba(92,110,239,.28)" hoverStyle="filter:brightness(1.06)">See the full outcome<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="#fff" strokeWidth="2"><path d="M6 3l5 5-5 5" /></svg></Hover>
              <Hover onClick={() => r.hasPdf ? window.open(r.pdfUrl, '_blank') : r.openReport()} style="cursor:pointer;background:rgba(0,0,0,.04);border:1px solid rgba(0,0,0,.12);color:#1a1712;font-size:14px;font-weight:600;padding:12px 22px;border-radius:12px;display:flex;align-items:center;gap:8px;font-family:'Space Grotesk',sans-serif" hoverStyle="background:rgba(0,0,0,.07)"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7z" /><circle cx="12" cy="12" r="3" /></svg>View report</Hover>
              <button onClick={this.back} style={S('cursor:pointer;background:transparent;border:1px solid rgba(0,0,0,.12);color:#54504a;font-size:14px;padding:12px 22px;border-radius:12px')}>Back to overview</button>
            </div>
          </>
        )}
      </div>
    )
  }

  renderOutcome(V) {
    const o = V.outcome
    return (
      <div style={S('animation:fadeUp .4s ease both')}>
        <div style={S('display:flex;align-items:center;gap:12px;margin-bottom:6px')}>
          <span style={S("font-family:'Space Grotesk',sans-serif;font-weight:600;font-size:22px")}>Outcome</span>
          <span style={S('color:#8a8478;font-size:13px')}>the full result for this transaction</span>
        </div>
        <div style={S('border-left:3px solid #7c5bef;padding-left:16px;color:#54504a;font-size:15px;line-height:1.55;max-width:78ch;margin-bottom:24px')}>Everything the pipeline concluded for this payment — the <b style={S('color:#1a1712')}>disposition</b>, what each <b style={S('color:#1a1712')}>agent</b> found, the filed <b style={S('color:#1a1712')}>report</b>, and the <b style={S('color:#1a1712')}>regulatory citations</b> it relied on.</div>

        {/* verdict banner */}
        <div style={S(o.bannerStyle)}>
          <span style={S(o.iconStyle)}>{o.icon}</span>
          <div style={S('flex:1')}>
            <div style={S("font-family:'Space Grotesk',sans-serif;font-weight:700;font-size:17px")}>{o.title}</div>
            <div style={S('font-size:13.5px;opacity:.9;margin-top:2px;line-height:1.4')}>{o.line}</div>
          </div>
          <div style={S('flex:none;text-align:right')}>
            <div style={S(o.confValStyle)}>{o.confPct}</div>
            <div style={S("font-family:'IBM Plex Mono',monospace;font-size:9.5px;letter-spacing:.5px;opacity:.8;margin-top:2px")}>{o.confLabel}</div>
          </div>
        </div>

        {/* transaction summary */}
        <div style={S('display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-top:16px')}>
          {[['Transaction', o.ref], ['Amount', o.amount], ['From', o.sender], ['To', o.receiver], ['Channel', o.channel]].map((m, i) => (
            <div key={i} style={S('background:#fffdfa;border:1px solid rgba(0,0,0,.07);border-radius:12px;padding:12px 14px;box-shadow:0 1px 2px rgba(0,0,0,.04)')}>
              <div style={S("font-family:'IBM Plex Mono',monospace;font-size:9.5px;letter-spacing:1px;color:#8a8478;margin-bottom:5px")}>{m[0].toUpperCase()}</div>
              <div style={S("font-family:'Space Grotesk',sans-serif;font-weight:600;font-size:13.5px;color:#1a1712;white-space:nowrap;overflow:hidden;text-overflow:ellipsis")}>{m[1]}</div>
            </div>
          ))}
        </div>

        {this.renderExplain(V)}

        <div style={S('display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:22px;align-items:start')}>
          {/* agent outcomes */}
          <div>
            <div style={S("font-family:'IBM Plex Mono',monospace;font-size:11px;letter-spacing:2px;color:#cf861b;font-weight:600;margin-bottom:12px")}>AGENT OUTCOMES · {o.dispatched} OF {o.total}</div>
            <div style={S('display:flex;flex-direction:column;gap:9px')}>
              {o.agents.map((a, i) => (
                <div key={i} style={S(a.rowStyle)}>
                  <div style={S('flex:1;min-width:0')}>
                    <div style={S("font-family:'Space Grotesk',sans-serif;font-weight:600;font-size:13.5px;color:#1a1712")}>{a.name}</div>
                    <div style={S('font-size:11.5px;color:#6a655d;line-height:1.35;margin-top:2px')}>{a.note}</div>
                  </div>
                  <span style={S(a.verdictStyle)}>{a.verdict}</span>
                  <span style={S(a.scoreStyle)}>{a.scorePct}</span>
                </div>
              ))}
            </div>
          </div>

          {/* citations */}
          <div>
            <div style={S("font-family:'IBM Plex Mono',monospace;font-size:11px;letter-spacing:2px;color:#2f7fd6;font-weight:600;margin-bottom:12px")}>CITATIONS · REGULATIONS APPLIED</div>
            <div style={S('display:flex;flex-direction:column;gap:9px')}>
              {o.citations.map((c, i) => (
                <Hover key={i} tag="a" href={c.url} target="_blank" rel="noopener" style="display:flex;align-items:flex-start;gap:11px;text-decoration:none;background:#fffdfa;border:1px solid rgba(0,0,0,.08);border-radius:12px;padding:12px 14px;box-shadow:0 1px 2px rgba(0,0,0,.04)" hoverStyle="background:rgba(47,127,214,.05);border-color:rgba(47,127,214,.3)">
                  <span style={S(c.badgeStyle)}>{c.code}</span>
                  <div style={S('flex:1;min-width:0')}>
                    <div style={S("font-family:'Space Grotesk',sans-serif;font-weight:600;font-size:13px;color:#1a1712")}>{c.ref}</div>
                    <div style={S('font-size:11.5px;color:#6a655d;line-height:1.4;margin-top:2px')}>{c.note}</div>
                  </div>
                  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#a9a297" strokeWidth="2" style={S('flex:none;margin-top:2px')}><path d="M7 17L17 7M9 7h8v8" /></svg>
                </Hover>
              ))}
            </div>
          </div>
        </div>

        {/* filed report */}
        <div style={S("margin-top:26px;font-family:'IBM Plex Mono',monospace;font-size:11px;letter-spacing:2px;color:#1f8a5b;font-weight:600;margin-bottom:12px")}>FILED REPORT</div>
        <div style={S('background:#ffffff;border:1px solid rgba(0,0,0,.1);border-radius:16px;overflow:hidden;box-shadow:0 2px 10px rgba(0,0,0,.05);max-width:760px')}>
          <div style={S(V.report.headerStyle)}>
            <span style={S("font-family:'Space Grotesk',sans-serif;font-weight:600;font-size:16px")}>{o.headerTitle}</span>
            <span style={S("font-family:'IBM Plex Mono',monospace;font-size:11px;opacity:.85")}>{o.rule}</span>
          </div>
          {o.hasPdf && (
            <div style={S('background:#f3f0ea')}>
              <iframe src={o.pdfUrl} title="STR PDF" style={S('width:100%;height:520px;border:none;display:block')}></iframe>
              <div style={S('padding:12px 16px;border-top:1px solid rgba(0,0,0,.06)')}>
                <a href={o.pdfUrl} target="_blank" rel="noopener" style={S("display:inline-flex;align-items:center;gap:7px;font-size:13px;font-weight:600;color:#2f6fd6;text-decoration:none;font-family:'Space Grotesk',sans-serif")}>Open PDF in new tab<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M7 17L17 7M9 7h8v8" /></svg></a>
              </div>
            </div>
          )}
          {!o.hasPdf && (
            <div style={S('padding:6px 4px')}>
              {o.reportFields.map((f, i) => (
                <div key={i} style={S(f.rowStyle)}>
                  <span style={S("font-family:'IBM Plex Mono',monospace;font-size:11.5px;color:#8a8478;width:34%")}>{f.key}</span>
                  <span style={S('font-size:13.5px;color:#1a1712;font-weight:500;flex:1')}>{f.val}</span>
                </div>
              ))}
            </div>
          )}
        </div>

        <div style={S('margin-top:24px;display:flex;gap:12px;flex-wrap:wrap')}>
          <Hover onClick={this.replayAll} style="cursor:pointer;background:rgba(0,0,0,.04);border:1px solid rgba(0,0,0,.12);color:#1a1712;font-size:14px;font-weight:600;padding:12px 22px;border-radius:12px;display:flex;align-items:center;gap:8px;font-family:'Space Grotesk',sans-serif" hoverStyle="background:rgba(0,0,0,.07)"><svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2"><path d="M13 8a5 5 0 11-1.5-3.5M13 2v3h-3" /></svg>Run the whole pipeline again</Hover>
          <button onClick={this.back} style={S('cursor:pointer;background:transparent;border:1px solid rgba(0,0,0,.12);color:#54504a;font-size:14px;padding:12px 22px;border-radius:12px')}>Back to overview</button>
        </div>
      </div>
    )
  }

  render() {
    const V = this.renderVals()
    return (
      <>
        {V.showOverview && this.renderOverview(V)}
        {V.showRun && this.renderRun(V)}
        {this.renderCircModal(V)}
      </>
    )
  }
}
