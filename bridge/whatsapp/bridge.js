/**
 * WhatsApp <-> Friday bridge (Baileys).
 *
 * First run prints a QR code — scan it with WhatsApp (Linked Devices).
 * Session credentials persist in ./wa-auth (chmod 700; treat like a private key).
 *
 * Protocol (WebSocket, localhost only, default :8765):
 *   bridge -> friday : {"from": "<jid>", "text": "..."}
 *   friday -> bridge : {"to": "<jid>", "text": "..."}
 *
 * ⚠ Unofficial WhatsApp automation violates WhatsApp ToS (ban risk).
 *   Use a secondary number. Keep this bound to 127.0.0.1.
 */
import makeWASocket, { useMultiFileAuthState, DisconnectReason } from "@whiskeysockets/baileys";
import qrcode from "qrcode-terminal";
import { WebSocketServer } from "ws";

const PORT = Number(process.env.BRIDGE_PORT || 8765);
const clients = new Set();
let sock = null;

const wss = new WebSocketServer({ host: "127.0.0.1", port: PORT });
wss.on("connection", (ws) => {
  clients.add(ws);
  console.log(`[bridge] friday connected (${clients.size} client/s)`);
  ws.on("message", async (raw) => {
    try {
      const { to, text } = JSON.parse(raw.toString());
      if (sock && to && text) await sock.sendMessage(to, { text });
    } catch (e) {
      console.error("[bridge] outbound failed:", e.message);
    }
  });
  ws.on("close", () => clients.delete(ws));
});

const broadcast = (obj) => {
  const raw = JSON.stringify(obj);
  for (const ws of clients) if (ws.readyState === 1) ws.send(raw);
};

async function start() {
  const { state, saveCreds } = await useMultiFileAuthState("wa-auth");
  sock = makeWASocket({ auth: state });
  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("connection.update", ({ connection, lastDisconnect, qr }) => {
    if (qr) qrcode.generate(qr, { small: true });
    if (connection === "open") console.log("[bridge] whatsapp connected");
    if (connection === "close") {
      const code = lastDisconnect?.error?.output?.statusCode;
      if (code !== DisconnectReason.loggedOut) {
        console.log("[bridge] reconnecting…");
        start();
      } else {
        console.log("[bridge] logged out — delete wa-auth/ and re-pair");
      }
    }
  });

  sock.ev.on("messages.upsert", ({ messages, type }) => {
    if (type !== "notify") return;
    for (const m of messages) {
      if (m.key.fromMe) continue;
      const text = m.message?.conversation || m.message?.extendedTextMessage?.text;
      if (text) broadcast({ from: m.key.remoteJid, text });
    }
  });
}

console.log(`[bridge] ws server on 127.0.0.1:${PORT}`);
start();
