"use strict";

const connectButton = document.getElementById("connect");
const disconnectButton = document.getElementById("disconnect");
const sendButton = document.getElementById("send");
const status = document.getElementById("connection-status");
const message = document.getElementById("message");
const messageLog = document.getElementById("message-log");
let socket = null;

function logMessage(text) {
  messageLog.value += text + "\n";
  messageLog.scrollTop = messageLog.scrollHeight;
}

connectButton.addEventListener("click", () => {
  const url = new URL("/ws/echo", window.location.href);
  url.protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  socket = new WebSocket(url);
  status.textContent = "Connecting";
  connectButton.disabled = true;

  socket.addEventListener("open", () => {
    status.textContent = "Connected";
    disconnectButton.disabled = false;
    sendButton.disabled = false;
    logMessage("Connected to " + url.pathname);
  });
  socket.addEventListener("message", (event) => {
    logMessage("Received: " + event.data);
  });
  socket.addEventListener("error", () => {
    status.textContent = "Connection error";
    logMessage("Connection error; check that the demo server is running.");
  });
  socket.addEventListener("close", (event) => {
    status.textContent = "Disconnected";
    connectButton.disabled = false;
    disconnectButton.disabled = true;
    sendButton.disabled = true;
    socket = null;
    logMessage("Disconnected (code " + event.code + ")");
  });
});

disconnectButton.addEventListener("click", () => {
  status.textContent = "Disconnecting";
  disconnectButton.disabled = true;
  sendButton.disabled = true;
  socket.close(1000, "Demo disconnect");
});

document.getElementById("message-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(message.value);
    logMessage("Sent: " + message.value);
  }
});
