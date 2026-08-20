// Сценарий (в): шторм авторизаций — 500 одновременных refresh на /oauth/token
// с ротацией (D6-08). Каждый VU ведёт собственную цепочку: ответ отдаёт новый
// refresh-токен, старый становится недействительным (R-O10), поэтому цепочки
// не пересекаются и повторное использование не срабатывает.
//
//   k6 run -e SCALE=0.1 -e HUB_BASE=http://localhost:8000 loadtest/k6/refresh_storm.js
//
// Число VU не должно превышать (пользователей × alias'ов) из seed: у каждого VU
// своя цепочка. Сеять — loadtest/tools/seed.py --users N.

import http from 'k6/http';
import { check } from 'k6';
import { Trend, Rate, Counter } from 'k6/metrics';
import {
  BASE_URL,
  THRESHOLD_REFRESH_P95,
  THRESHOLD_ERROR_RATE,
  loadSeed,
  scaled,
  insecureSkipTLS,
} from './lib/config.js';

const seed = loadSeed(open(__ENV.SEED_FILE || '../.seed/seed.json'));

const refreshLatency = new Trend('oauth_refresh', true);
const rotations = new Counter('oauth_refresh_rotations');
const errors = new Rate('scenario_errors');

// Спека: 500 одновременных refresh.
const STORM_VUS = parseInt(__ENV.STORM_VUS || String(scaled(500)), 10);
const DURATION = __ENV.DURATION || '60s';
// Сколько ротаций делает каждая цепочка в залповом режиме.
const ROUNDS = parseInt(__ENV.REFRESH_ROUNDS || '5', 10);

// MODE=burst (по умолчанию) — залп: все цепочки стартуют одновременно и делают
// по ROUNDS ротаций. Это и есть «500 одновременных refresh» из спеки, на нём
// проверяется порог p95 ≤ 100 мс. MODE=sustained — постоянная нагрузка без пауз:
// показывает потолок по refresh в секунду, пороги при этом не применяются.
const MODE = __ENV.MODE || 'burst';

// Пары (пользователь, alias): по одной цепочке на VU.
const CHAINS = [];
for (let u = 0; u < seed.users.length; u++) {
  for (let a = 0; a < seed.aliases.length; a++) {
    const alias = seed.aliases[a];
    CHAINS.push({ alias: alias, refresh: seed.users[u].tokens[alias].refresh_token });
  }
}

const VUS = Math.min(STORM_VUS, CHAINS.length);

export const options = {
  insecureSkipTLSVerify: insecureSkipTLS(),
  scenarios: {
    refresh_storm:
      MODE === 'sustained'
        ? { executor: 'constant-vus', vus: VUS, duration: DURATION }
        : {
            executor: 'per-vu-iterations',
            vus: VUS,
            iterations: ROUNDS,
            maxDuration: DURATION,
          },
  },
  thresholds:
    MODE === 'sustained'
      ? {}
      : {
          oauth_refresh: [`p(95)<${THRESHOLD_REFRESH_P95}`],
          scenario_errors: [`rate<${THRESHOLD_ERROR_RATE}`],
          http_req_failed: [`rate<${THRESHOLD_ERROR_RATE}`],
        },
};

// Текущий refresh-токен цепочки этого VU (обновляется после каждой ротации).
let current = null;

export default function () {
  const chain = CHAINS[(__VU - 1) % CHAINS.length];
  if (current === null) current = chain.refresh;

  const response = http.post(
    `${BASE_URL}/oauth/token`,
    {
      grant_type: 'refresh_token',
      refresh_token: current,
      client_id: seed.client_id,
      scope: `${chain.alias}:${seed.preset}`,
    },
    { tags: { op: 'oauth_refresh' } }
  );
  refreshLatency.add(response.timings.duration);

  const ok = check(response, {
    'refresh 200': (r) => r.status === 200,
    'выдан новый refresh': (r) => {
      try {
        return !!r.json().refresh_token;
      } catch (e) {
        return false;
      }
    },
  });
  errors.add(!ok);
  if (ok) {
    current = response.json().refresh_token;
    rotations.add(1);
  } else {
    // Цепочка порвалась (например, сработал rate-limit) — начинаем со стартового
    // токена: если и он уже использован, Hub отзовёт цепочку и это будет видно
    // по scenario_errors, а не по молчаливому «зелёному» прогону.
    current = chain.refresh;
  }
}
