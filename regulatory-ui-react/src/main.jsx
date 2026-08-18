import React from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.jsx'

// NOTE: not wrapped in <React.StrictMode> on purpose — the pipeline animations
// use setTimeout chains, and StrictMode's dev double-invoke would fire them twice.
createRoot(document.getElementById('root')).render(<App />)
