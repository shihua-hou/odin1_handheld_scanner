export function createStatusSocket({url, onStatus, onConnection, onError, reconnectMs=2500}) {
  let socket = null, timer = null, closed = false;
  function connect() {
    clearTimeout(timer); closed = false;
    if (socket) { socket.onclose = null; socket.close(); }
    socket = new WebSocket(url());
    socket.onopen = () => onConnection?.('connected');
    socket.onmessage = event => {
      if (event.data instanceof ArrayBuffer) return;
      try { const data = JSON.parse(event.data); if (data.type === 'status') onStatus(data); }
      catch (error) { onError?.(error); }
    };
    socket.onerror = error => onConnection?.('error', error);
    socket.onclose = () => { onConnection?.('closed'); if (!closed) timer=setTimeout(connect,reconnectMs); };
  }
  return {connect, close(){closed=true;clearTimeout(timer);socket?.close();}};
}
