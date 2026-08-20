// Сценарий (а): «холодный старт» — 5 000 клиентов одновременно тянут конфигурацию
// (D6-08). GET /.well-known/opencode без условного заголовка и с If-None-Match
// (ожидается 304), затем GET /remote-config с ключом LiteLLM.
//
//   k6 run -e SCALE=0.1 -e HUB_BASE=http://localhost:8000 loadtest/k6/wellknown.js

import http from 'k6/http';
import { check } from 'k6';
import { Trend, Rate } from 'k6/metrics';
import {
  BASE_URL,
  SCALE,
  THRESHOLD_API_P95,
  THRESHOLD_ERROR_RATE,
  loadSeed,
  scaled,
  insecureSkipTLS,
} from './lib/config.js';

const seed = loadSeed(open(__ENV.SEED_FILE || '../.seed/seed.json'));

const wellknownFresh = new Trend('wellknown_fresh', true);
const wellknown304 = new Trend('wellknown_not_modified', true);
const remoteConfig = new Trend('remote_config', true);
const errors = new Rate('scenario_errors');

// Полная нагрузка спеки — 5 000 клиентов «просыпаются» в течение минуты.
const CLIENTS = scaled(5000);
const DURATION = __ENV.DURATION || '60s';

export const options = {
  insecureSkipTLSVerify: insecureSkipTLS(),
  discardResponseBodies: false,
  scenarios: {
    cold_start: {
      executor: 'constant-arrival-rate',
      rate: Math.max(1, Math.round(CLIENTS / parseInt(DURATION, 10))),
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: Math.max(10, Math.round(CLIENTS / 20)),
      maxVUs: Math.max(20, Math.round(CLIENTS / 5)),
    },
  },
  thresholds: {
    wellknown_fresh: [`p(95)<${THRESHOLD_API_P95}`],
    wellknown_not_modified: [`p(95)<${THRESHOLD_API_P95}`],
    remote_config: [`p(95)<${THRESHOLD_API_P95}`],
    scenario_errors: [`rate<${THRESHOLD_ERROR_RATE}`],
    http_req_failed: [`rate<${THRESHOLD_ERROR_RATE}`],
  },
};

export function setup() {
  return { scale: SCALE, clients: CLIENTS };
}

export default function () {
  const user = seed.users[Math.floor(Math.random() * seed.users.length)];

  const fresh = http.get(`${BASE_URL}/.well-known/opencode`, { tags: { op: 'wellknown' } });
  wellknownFresh.add(fresh.timings.duration);
  const freshOk = check(fresh, {
    'well-known 200': (r) => r.status === 200,
    'well-known отдаёт ETag': (r) => !!(r.headers.Etag || r.headers.ETag),
  });

  const etag = fresh.headers.Etag || fresh.headers.ETag;
  const cached = http.get(`${BASE_URL}/.well-known/opencode`, {
    headers: { 'If-None-Match': etag },
    tags: { op: 'wellknown_304' },
  });
  wellknown304.add(cached.timings.duration);
  const cachedOk = check(cached, { 'условный запрос 304': (r) => r.status === 304 });

  const rc = http.get(`${BASE_URL}/remote-config`, {
    headers: { Authorization: `Bearer ${user.api_key}` },
    tags: { op: 'remote_config' },
  });
  remoteConfig.add(rc.timings.duration);
  const rcOk = check(rc, { 'remote-config 200': (r) => r.status === 200 });

  errors.add(!(freshOk && cachedOk && rcOk));
}
