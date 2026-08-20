// Помощники MCP поверх streamable HTTP для нагрузочных сценариев.
import http from 'k6/http';

export const SESSION_HEADER = 'Mcp-Session-Id';

export function rpc(method, params, id) {
  const body = { jsonrpc: '2.0', method: method };
  if (id !== undefined && id !== null) body.id = id;
  if (params !== undefined) body.params = params;
  return JSON.stringify(body);
}

export function mcpHeaders(token, sessionId) {
  const headers = {
    'Content-Type': 'application/json',
    Accept: 'application/json, text/event-stream',
  };
  if (token) headers.Authorization = `Bearer ${token}`;
  if (sessionId) headers[SESSION_HEADER] = sessionId;
  return headers;
}

// Открыть виртуальную MCP-сессию: initialize + notifications/initialized.
// Возвращает клиентский Mcp-Session-Id, выданный Hub, либо null.
export function openSession(url, token, tag) {
  const init = http.post(url, rpc('initialize', {
    protocolVersion: '2025-06-18',
    capabilities: {},
    clientInfo: { name: 'k6-loadtest', version: '1' },
  }, 'init-1'), { headers: mcpHeaders(token, null), tags: { op: 'initialize', scenario: tag } });
  if (init.status !== 200) return null;
  const sessionId = init.headers[SESSION_HEADER] || init.headers['Mcp-Session-Id'];
  if (!sessionId) return null;
  http.post(url, rpc('notifications/initialized'), {
    headers: mcpHeaders(token, sessionId),
    tags: { op: 'initialized', scenario: tag },
  });
  return sessionId;
}

export function isRpcOk(response) {
  if (response.status !== 200) return false;
  try {
    const payload = response.json();
    return !payload.error && !!payload.result;
  } catch (e) {
    return false;
  }
}
