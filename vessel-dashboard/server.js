/*
=============================================================
  VESSEL DASHBOARD SERVER  —  Node.js + Express + ws
=============================================================
  Sits between the Python inference bridge and any number of
  browsers viewing the dashboard.

  Python  --POST JSON--> /ingest  --broadcast--> WS clients

  Message shapes sent to browsers (matches dashboard.html):
    {type:'init',   latest, recentLog}   — on new connection
    {type:'update', latest}              — every /ingest hit
    {type:'status', online}              — every 3s watchdog tick
    {type:'pong'}                        — reply to client ping

  GET /history?since=<ISO8601>  → {rows, latest}
=============================================================
*/
const express = require("express");
const http = require("http");
const path = require("path");
const { WebSocketServer, WebSocket } = require("ws");

const PORT = process.env.PORT || 8080;
const MAX_HISTORY = 2000;
const OFFLINE_AFTER_MS = 8000;   // no /ingest hit in this window => "offline"

const app = express();
app.use(express.json());
app.use(express.static(path.join(__dirname, "public")));

const server = http.createServer(app);
const wss = new WebSocketServer({ server });

let history = [];        // array of ingested payloads (rolling window)
let latest = {};
let lastIngestTs = null;

function broadcast(obj) {
  const msg = JSON.stringify(obj);
  wss.clients.forEach((c) => {
    if (c.readyState === WebSocket.OPEN) c.send(msg);
  });
}

app.post("/ingest", (req, res) => {
  const data = req.body || {};
  data.ts = data.ts || new Date().toISOString();

  latest = data;
  lastIngestTs = Date.now();
  history.push(data);
  if (history.length > MAX_HISTORY) history.shift();

  broadcast({ type: "update", latest: data });
  res.json({ ok: true });
});

app.get("/history", (req, res) => {
  const since = req.query.since;
  let rows = history;
  if (since) {
    const t = new Date(since).getTime();
    if (!Number.isNaN(t)) {
      rows = history.filter((r) => new Date(r.ts).getTime() > t);
    }
  }
  res.json({ rows, latest });
});

app.get("/healthz", (req, res) => res.json({ ok: true, lastIngestTs, historyLen: history.length }));

wss.on("connection", (ws) => {
  ws.send(JSON.stringify({
    type: "init",
    latest,
    recentLog: history.slice(-50).reverse(),
  }));

  ws.on("message", (raw) => {
    try {
      const m = JSON.parse(raw);
      if (m.type === "ping") ws.send(JSON.stringify({ type: "pong" }));
    } catch (e) {
      // ignore malformed client messages
    }
  });
});

// online/offline watchdog — tells all browsers if the Python bridge
// has gone quiet, independent of whether the WS connection itself is fine
setInterval(() => {
  const online = !!lastIngestTs && (Date.now() - lastIngestTs < OFFLINE_AFTER_MS);
  broadcast({ type: "status", online });
}, 3000);

server.listen(PORT, () => {
  console.log(`Vessel dashboard server listening on http://localhost:${PORT}`);
  console.log(`Python should POST ticks to  http://localhost:${PORT}/ingest`);
});