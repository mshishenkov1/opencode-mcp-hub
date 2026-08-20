#!/bin/bash
# Сквозная проверка целевого сценария: вход → ключ → каталог → подключение ТЭГ токеном → работа инструментов.
# Ничего не меняет в репозиториях; работает против локального стенда.
set -uo pipefail
HUB=${HUB:-http://localhost:8080}
KEY=$(python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/.local/share/opencode/auth.json')))['magnit_prod']['key'])")
MMT=$(grep '^MM_TOKEN=' "$HOME/Documents/magnit-tag-mcp/.env" | cut -d= -f2-)
ok=0; fail=0
step() { printf '\n%s\n' "── $1"; }
check() { if [ "$1" = "$2" ]; then echo "  ✓ $3 ($1)"; ok=$((ok+1)); else echo "  ✗ $3: ожидалось $2, получено $1"; fail=$((fail+1)); fi; }

step "1. Ключ из входа работает против Hub"
code=$(curl -s -o /tmp/e2e_me.json -w '%{http_code}' --max-time 15 -H "Authorization: Bearer $KEY" "$HUB/api/me")
check "$code" 200 "GET /api/me"
python3 -c "import json;d=json.load(open('/tmp/e2e_me.json'));print('  пользователь:',d.get('user_id'),'| тип ключа:',d.get('key_kind'))" 2>/dev/null

step "2. Каталог отдаёт коннектор ТЭГ со способами подключения"
curl -s --max-time 15 -H "Authorization: Bearer $KEY" "$HUB/api/catalog" -o /tmp/e2e_cat.json
python3 - <<'PY'
import json
d = json.load(open('/tmp/e2e_cat.json'))
tag = next((s for s in d.get('servers', []) if s['alias'] == 'tag'), None)
if not tag:
    print('  ✗ коннектора tag нет в каталоге'); raise SystemExit(1)
print('  режим:', tag.get('mode'), '| статус:', (tag.get('connection') or {}).get('status'))
methods = tag.get('auth_methods')
if methods:
    for m in methods:
        s = 'доступен' if m.get('available', True) else f"недоступен: {m.get('unavailable_reason','')}"
        print(f"  способ {m['id']} ({m['type']}) — {s}")
    leaked = [k for m in methods for k in ('verify', 'client_secret', 'client_id', 'scopes') if k in m]
    print('  ✗ наружу утекли поля:', leaked) if leaked else print('  ✓ секретных полей в публичном представлении нет')
else:
    print('  ✗ auth_methods не отдаётся')
PY

step "3. Подключение токеном сессии"
code=$(curl -s -o /tmp/e2e_conn.json -w '%{http_code}' --max-time 30 -X POST \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d "{\"token\":\"$MMT\",\"method\":\"session_token\",\"preset\":\"readonly\"}" \
  "$HUB/api/me/connections/tag/token")
check "$code" 200 "POST /api/me/connections/tag/token"
python3 -c "
import json
d=json.load(open('/tmp/e2e_conn.json'))
print('  ответ:', json.dumps(d, ensure_ascii=False)[:200])
import os
tok=os.popen(\"grep '^MM_TOKEN=' \$HOME/Documents/magnit-tag-mcp/.env | cut -d= -f2-\").read().strip()
print('  ✓ токена в ответе нет' if tok and tok not in json.dumps(d) else '  ✗ ТОКЕН В ОТВЕТЕ')" 2>/dev/null

step "4. Заведомо неверный токен отвергается"
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 -X POST \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"token":"zzzz-заведомо-неверный-zzzz","method":"session_token"}' \
  "$HUB/api/me/connections/tag/token")
check "$code" 400 "неверный токен → 400 token_rejected"

step "5. Недоступный способ (корпоративный OAuth) отвергается"
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 -X POST \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"token":"x","method":"corp_oauth"}' "$HUB/api/me/connections/tag/token")
check "$code" 409 "недоступный способ → 409"

step "6. Инструменты ТЭГ работают через Hub от имени пользователя"
# MCP-клиент авторизуется в Hub собственным токеном OAuth-фасада, а не ключом LiteLLM:
# в приложении этот шаг проходит сам (с браузерным согласием), здесь токен выпускается напрямую.
HUBC=$(docker ps --filter name=hubi3-hub -q | head -1)
docker exec "$HUBC" python - <<'MINT' >/dev/null 2>&1
import asyncio
from hub.app import create_app
from hub.crypto import random_token
async def main():
    app = create_app()
    async with app.router.lifespan_context(app):
        st = app.state
        conn = await st.broker.load_connection("shishenkov_ma", "tag")
        reg = await st.oauth.register_client({"client_name":"e2e","redirect_uris":["http://127.0.0.1:53682/cb"],
            "grant_types":["authorization_code"],"response_types":["code"],
            "token_endpoint_auth_method":"none"}, ip="127.0.0.1")
        cid = reg["client_id"] if isinstance(reg, dict) else reg
        toks = await st.oauth.issue_tokens(client_id=cid, user_id="shishenkov_ma", alias="tag",
            connection_id=conn.id, scope="tag:readonly", chain_id=random_token())
        open("/tmp/tok.txt","w").write(toks["access_token"])
asyncio.run(main())
MINT
docker cp "$HUBC":/tmp/tok.txt /tmp/hubtok.txt >/dev/null 2>&1
KEY=$(cat /tmp/hubtok.txt)
H=$(mktemp)
curl -s --max-time 25 -D "$H" -o /dev/null -X POST "$HUB/mcp/tag" \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"e2e","version":"1"}}}'
SID=$(grep -i '^mcp-session-id:' "$H" | tr -d '\r' | awk '{print $2}'); rm -f "$H"
curl -s --max-time 20 -X POST "$HUB/mcp/tag" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" -H "Mcp-Session-Id: $SID" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null
curl -s --max-time 30 -X POST "$HUB/mcp/tag" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" -H "Mcp-Session-Id: $SID" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' -o /tmp/e2e_tools.txt
python3 - <<'PY'
import json
tools = None
for line in open('/tmp/e2e_tools.txt'):
    if line.startswith('data:'):
        try: d = json.loads(line[5:].strip())
        except Exception: continue
        if 'result' in d and 'tools' in d['result']:
            tools = [t['name'] for t in d['result']['tools']]
if tools is None:
    print('  ✗ список инструментов не получен'); raise SystemExit
print(f'  ✓ инструментов через Hub: {len(tools)}')
writes = [t for t in tools if t.startswith(('post_', 'create_', 'delete_', 'update_'))]
print('  ✓ пресет readonly: пишущих инструментов нет' if not writes else f'  ✗ в readonly видны пишущие: {writes[:5]}')
PY
curl -s --max-time 30 -X POST "$HUB/mcp/tag" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" -H "Mcp-Session-Id: $SID" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"whoami","arguments":{}}}' -o /tmp/e2e_who.txt
python3 - <<'PY'
import json
for line in open('/tmp/e2e_who.txt'):
    if line.startswith('data:'):
        try: d = json.loads(line[5:].strip())
        except Exception: continue
        if 'result' in d:
            txt = d['result']['content'][0]['text']
            u = json.loads(txt)['user']
            print(f"  ✓ whoami через Hub: {u['username']} ({u['display_name']})")
            break
else:
    print('  ✗ whoami не отработал')
PY

step "7. Токен не утёк в логи Hub и в аудит"
docker logs hubi3-hub-1 --since 10m 2>&1 | grep -qF "$MMT" && echo "  ✗ ТОКЕН НАЙДЕН В ЛОГАХ" || echo "  ✓ в логах Hub токена нет"
docker exec hubi3-postgres-1 psql -U hub -d hub -tAc "select details::text from audit_log order by id desc limit 20" 2>/dev/null | grep -qF "$MMT" && echo "  ✗ ТОКЕН НАЙДЕН В АУДИТЕ" || echo "  ✓ в аудите токена нет"

printf '\n══ итог: успешных проверок %s, провалов %s\n' "$ok" "$fail"
