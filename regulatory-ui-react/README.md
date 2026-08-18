# Regulatory UI — React (Vite)

A React port of the Compliance Pipeline regulatory UI. Same design, same live
wiring to the FastAPI backend (`api.py`) as the original design-export
(`regulatory-ui/Compliance Pipeline.dc.html`) — just rebuilt as a standard
React + Vite app instead of the design-tool runtime.

## Run

```bash
cd regulatory-ui-react
npm install        # first time only
npm run dev        # dev server on http://localhost:5173
```

Build for production:

```bash
npm run build      # outputs to dist/
npm run preview    # serve the production build
```

## Backend

The UI streams transactions to the pipeline API at `http://localhost:8000`.
Start it from the repo root:

```bash
uvicorn api:app --reload --port 8000
```

If the backend is unreachable the UI falls back to the built-in demo data, so it
still renders and animates offline. To point at a different backend, set
`window.PIPELINE_API` before the app loads, or edit `BACKEND` in `src/App.jsx`.

## Layout

- `src/App.jsx` — the whole component: state machine, live SSE integration,
  sample data, typology-graph SVGs (`React.createElement`), and the JSX views.
- `src/styleStr.js` — parses the original inline-CSS strings into React style
  objects, so the styling carries over verbatim.
- `src/Hover.jsx` — reproduces the original `style-hover` behaviour.
- `src/index.css` — global reset + keyframes.

The logic is a faithful copy of the original component; only the presentation
layer was converted from the DC template (`sc-if` / `sc-for` / `{{ }}`) to JSX.
