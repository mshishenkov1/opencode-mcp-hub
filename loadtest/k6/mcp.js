// Сценарий (б): MCP-трафик через /mcp/{alias} против мок-upstream (D6-08).
//
// 30 000 виртуальных сессий, из них 3–5 % активны: setup() открывает сессии
// (initialize + notifications/initialized), рабочие итерации гоняют tools/list и
// tools/call. Параллельно тот же профиль запросов идёт напрямую в мок — это база
// для вычисления добавки proxy (p50 ≤ 15 мс, p95 ≤ 50 мс по S-01).
//
//   k6 run -e SCALE=0.1 -e HUB_BASE=http://localhost:8000 \
//          -e MOCK_URL=http://localhost:8080 loadtest/k6/mcp.js

import http from 'k6/http';
import { check } from 'k6';
import { Trend, Rate } from 'k6/metrics';
import {
  BASE_URL,
  MOCK_URL,
  SCALE,
  THRESHOLD_ERROR_RATE,
  THRESHOLD_PROXY_P50,
  THRESHOLD_PROXY_P95,
  loadSeed,
  scaled,
  insecureSkipTLS,
} from './lib/config.js';
import { isRpcOk, mcpHeaders, openSession, rpc } from './lib/mcp.js';

const seed = loadSeed(open(__ENV.SEED_FILE || '../.seed/seed.json'));

const viaHub = new Trend('mcp_via_hub', true);
const direct = new Trend('mcp_direct', true);
const toolsList = new Trend('mcp_tools_list', true);
const toolsCall = new Trend('mcp_tools_call', true);
const errors = new Rate('scenario_errors');

// Спека: 30 000 виртуальных сессий, активны 3–5 %.
const SESSIONS = parseInt(__ENV.SESSIONS || String(scaled(30000)), 10);
const ACTIVE_RATIO = parseFloat(__ENV.ACTIVE_RATIO || '0.04');
const ACTIVE_SESSIONS = Math.max(1, Math.round(SESSIONS * ACTIVE_RATIO));
// Активная сессия шлёт примерно один запрос в секунду: 30 000 × 4 % ≈ 1 200 rps
// на полной нагрузке, 120 rps на одной десятой.
const RPS_PER_SESSION = parseFloat(__ENV.RPS_PER_SESSION || '1');
const MCP_RPS = Math.max(1, parseInt(__ENV.MCP_RPS || String(Math.round(ACTIVE_SESSIONS * RPS_PER_SESSION)), 10));
const DURATION = __ENV.DURATION || '60s';

// MODE=steady (по умолчанию) — ожидаемая нагрузка с фиксированным темпом: именно на
// ней проверяются пороги S-01. MODE=saturate — открытый цикл на ACTIVE_SESSIONS VU:
// показывает потолок одной реплики, пороги при этом заведомо не выполняются.
const MODE = __ENV.MODE || 'steady';

const steadyScenarios = {
  via_hub: {
    executor: 'constant-arrival-rate',
    exec: 'trafficViaHub',
    rate: MCP_RPS,
    timeUnit: '1s',
    duration: DURATION,
    preAllocatedVUs: Math.max(10, Math.round(MCP_RPS / 4)),
    maxVUs: Math.max(20, MCP_RPS * 2),
  },
  // База для вычитания: тот же профиль запросов прямо в мок, четверть темпа —
  // чтобы измерение базы само не искажало картину.
  direct_baseline: {
    executor: 'constant-arrival-rate',
    exec: 'trafficDirect',
    rate: Math.max(1, Math.round(MCP_RPS / 4)),
    timeUnit: '1s',
    duration: DURATION,
    preAllocatedVUs: Math.max(5, Math.round(MCP_RPS / 16)),
    maxVUs: Math.max(10, Math.round(MCP_RPS / 2)),
  },
};

const saturateScenarios = {
  via_hub: {
    executor: 'constant-vus',
    exec: 'trafficViaHub',
    vus: ACTIVE_SESSIONS,
    duration: DURATION,
  },
  direct_baseline: {
    executor: 'constant-vus',
    exec: 'trafficDirect',
    vus: Math.max(1, Math.round(ACTIVE_SESSIONS / 4)),
    duration: DURATION,
  },
};

export const options = {
  insecureSkipTLSVerify: insecureSkipTLS(),
  setupTimeout: __ENV.SETUP_TIMEOUT || '900s',
  scenarios: MODE === 'saturate' ? saturateScenarios : steadyScenarios,
  thresholds:
    MODE === 'saturate'
      ? {}
      : {
          // Мок в нагрузочном контуре отвечает без искусственной задержки, поэтому
          // mcp_via_hub — это добавка Hub плюс собственное время мока (mcp_direct);
          // точную разницу считает loadtest/tools/overhead.py.
          mcp_via_hub: [`p(50)<${THRESHOLD_PROXY_P50}`, `p(95)<${THRESHOLD_PROXY_P95}`],
          scenario_errors: [`rate<${THRESHOLD_ERROR_RATE}`],
          http_req_failed: [`rate<${THRESHOLD_ERROR_RATE}`],
        },
};

export function setup() {
  const opened = [];
  for (let i = 0; i < SESSIONS; i++) {
    const user = seed.users[i % seed.users.length];
    const alias = seed.aliases[i % seed.aliases.length];
    const token = user.tokens[alias].access_token;
    const url = `${BASE_URL}/mcp/${alias}`;
    const sessionId = openSession(url, token, 'setup');
    if (sessionId) {
      opened.push({ url: url, token: token, sessionId: sessionId, alias: alias });
    }
  }
  const directSession = openSession(`${MOCK_URL}/mcp`, 'mock-token', 'setup-direct');
  console.log(
    `Режим ${MODE}: открыто виртуальных сессий ${opened.length} из ${SESSIONS}, ` +
      `активных ${ACTIVE_SESSIONS}, темп ${MCP_RPS} rps`
  );
  return { sessions: opened, directSession: directSession, scale: SCALE };
}

function pick(data) {
  return data.sessions[Math.floor(Math.random() * data.sessions.length)];
}

function callTools(url, token, sessionId, tag, trend) {
  // 20 % — tools/list (у Hub он кэшируется), 80 % — tools/call.
  const wantList = Math.random() < 0.2;
  const body = wantList
    ? rpc('tools/list', {}, `list-${__ITER}`)
    : rpc('tools/call', { name: 'core_tool_00', arguments: { query: 'нагрузка' } }, `call-${__ITER}`);
  const response = http.post(url, body, {
    headers: mcpHeaders(token, sessionId),
    tags: { op: wantList ? 'tools/list' : 'tools/call', scenario: tag },
  });
  trend.add(response.timings.duration);
  (wantList ? toolsList : toolsCall).add(response.timings.duration);
  const ok = check(response, { 'JSON-RPC result без ошибки': (r) => isRpcOk(r) });
  errors.add(!ok);
  return ok;
}

export function trafficViaHub(data) {
  if (!data.sessions.length) {
    errors.add(true);
    return;
  }
  const session = pick(data);
  callTools(session.url, session.token, session.sessionId, 'via_hub', viaHub);
}

export function trafficDirect(data) {
  callTools(`${MOCK_URL}/mcp`, 'mock-token', data.directSession, 'direct', direct);
}

// Добавка proxy считается по экспортированному итогу:
//   k6 run --summary-export=loadtest/.seed/summary-mcp.json ... loadtest/k6/mcp.js
//   python loadtest/tools/overhead.py loadtest/.seed/summary-mcp.json
