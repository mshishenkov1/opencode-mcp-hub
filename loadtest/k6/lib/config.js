// Общие настройки нагрузочных сценариев (D6-08, D6-10).
//
// Защита от боевых адресов: любой адрес, который сценарий собирается дёргать,
// проходит через assertLocal(). Хост вне ALLOWED_HOSTS — немедленный fail()
// на этапе инициализации, до первого запроса. Вторая линия защиты —
// loadtest/tools/check_no_prod.sh (грепом по файлам сценариев и конфигурации).

import { fail } from 'k6';

export const ALLOWED_HOSTS = [
  'localhost',
  '127.0.0.1',
  '[::1]',
  'hub',
  'proxy',
  'mock-upstream',
];

export function hostOf(url) {
  const withoutScheme = String(url).replace(/^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//, '');
  const authority = withoutScheme.split('/')[0].split('@').pop();
  const match = authority.match(/^(\[[^\]]+\]|[^:]+)/);
  return match ? match[1] : authority;
}

export function assertLocal(url, name) {
  const host = hostOf(url);
  if (ALLOWED_HOSTS.indexOf(host) === -1) {
    fail(
      `${name}=${url}: хост ${host} не входит в список разрешённых [${ALLOWED_HOSTS}]. ` +
        'Нагрузочные сценарии запрещено направлять в боевые системы (D6-10).'
    );
  }
  return String(url).replace(/\/+$/, '');
}

// Масштаб прогона: 1 — полная нагрузка из спеки, 0.1 — «одна десятая» (D6-09).
export const SCALE = parseFloat(__ENV.SCALE || '0.1');

export function scaled(value) {
  return Math.max(1, Math.round(value * SCALE));
}

export const BASE_URL = assertLocal(__ENV.HUB_BASE || 'http://localhost:8000', 'HUB_BASE');
export const MOCK_URL = assertLocal(__ENV.MOCK_URL || 'http://localhost:8080', 'MOCK_URL');

// Пороги S-01/S-02 (см. loadtest/README.md).
export const THRESHOLD_PROXY_P50 = parseInt(__ENV.THRESHOLD_PROXY_P50 || '15', 10);
export const THRESHOLD_PROXY_P95 = parseInt(__ENV.THRESHOLD_PROXY_P95 || '50', 10);
export const THRESHOLD_API_P95 = parseInt(__ENV.THRESHOLD_API_P95 || '100', 10);
// Отдельного порога на /oauth/token в спеке нет: по умолчанию берётся порог /api/*
// как ориентир. Согласованное значение задаётся переменной окружения.
export const THRESHOLD_REFRESH_P95 = parseInt(
  __ENV.THRESHOLD_REFRESH_P95 || __ENV.THRESHOLD_API_P95 || '100',
  10
);
export const THRESHOLD_ERROR_RATE = parseFloat(__ENV.THRESHOLD_ERROR_RATE || '0.001');

export function loadSeed(raw) {
  const data = JSON.parse(raw);
  if (!data.users || data.users.length === 0) {
    fail('Файл seed пуст: сначала выполните loadtest/tools/seed.py');
  }
  return data;
}

export function insecureSkipTLS() {
  return String(__ENV.INSECURE || '') === '1';
}
