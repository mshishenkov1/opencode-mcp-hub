# Тест-отчёт I-4, ревизия 1.9 спеки (S-V15…S-V19, S-D10, S-T10, S-B13…S-B15)

- **Репозиторий:** `~/Documents/opencode`, ветка `corp/i4-sso-connectors` (только локальные коммиты).
- **Спека:** `corp/docs/spec.md` @ `5efd9491be`, критерии `corp/docs/acceptance-criteria.yaml` @ `a1ad60532d`.
- **Реализация под проверкой:** 15 коммитов `a839e34d52…29f483e968`.
- **Базовый коммит для сравнения:** `a1ad60532d` (критерии без реализации).
- **Коммиты тестов:** `afdd1f0efa`, `4b5f97b262`, `d9e35d509e`, `f3a392e683`, `8cbcc140ca`, `e76b08e08a`.
- **Дата:** 2026-08-25.

## 1. Итог

| Показатель | Значение |
|---|---|
| Критериев ревизии 1.9 покрыто | 31 из 31 (AC-170…AC-199 + переформулированный AC-51) |
| Новых тестов написано | 112 |
| Из них зелёных | 110 |
| Красных (минимальные воспроизведения дефекта) | 2 → `bugs/BUG-I4-011.json` |
| Корп-падений из диспутов до правки | 6 |
| Корп-падений из диспутов после правки | 0 |
| Заведено багов | 1 (`BUG-I4-011`, severity `high`) |
| Диспуты | 2 из 2 разрешены в пользу dev-agent |

## 2. Таблица AC → тест → результат

Файлы: `oc` = `packages/opencode/src/corp/`, `desk` = `packages/desktop/`, `tui` = `packages/tui/src/corp/`,
`app` = `packages/app/src/corp/`, `corp` = `corp/`.

| AC | Тест | Рез. |
|---|---|---|
| **AC-51** (переформулирован) | `oc/status.test.ts::AC-51: строка 3 — локальный статус failed показывается как unavailable с текстом ошибки` — пара случаев с одинаковым `local="failed"`: без признака `["connect","open_hub"]`, с признаком `["reconnect","forget","open_hub"]`; статус `unavailable` и текст ошибки — дословно из прежней редакции | ✅ |
| AC-170 | `oc/status.test.ts::AC-170: «Не подключён» — только «Подключить» и «Открыть в Hub»` (три варианта входа) | ✅ |
| AC-171 | `oc/status.test.ts::AC-171: «Подключение не удалось» — «Отключить» и «Убрать из списка» отсутствуют` (`needs_auth` и `needs_client_registration`) | ✅ |
| AC-172 | `oc/orchestration.test.ts::AC-172: после неудачи запись конфига осталась, признак не выставлен, «Отключить» нет`; `app/connectors-view.test.ts::AC-172: слово «Отключить» на карточке появляется только по действию disconnect`; `…::AC-172, S-V19: подпись состояния и объяснение ошибки берутся из состояния и класса` | ✅ |
| AC-173 | `oc/status.test.ts::AC-173: «Соединение потеряно» — «Повторить» и «Убрать из списка», подпись с текстом ошибки` | ✅ |
| AC-174 | `oc/status.test.ts::AC-174: connection.needs_reauth без локальной истории даёт состояние 3 и статус needs_auth` | ✅ |
| AC-174 | `oc/status.test.ts::AC-174: connection.needs_reauth с записью в конфиге — «Соединение потеряно», а не «Отключено вами»` | ❌ **BUG-I4-011** |
| AC-175 | `oc/connectors.test.ts::AC-175: патч удаляет ключ mcp.<alias> целиком, а не гасит его`; `…::AC-175: правятся только ключи mcp.<alias>…`; `oc/routes.test.ts::AC-175: «Убрать из списка» — отдельный маршрут DELETE /corp/connectors/:alias`; `oc/orchestration.test.ts::AC-175: локальные шаги, удаление ключа mcp.<alias> и DELETE подключения в Hub — по порядку`; `…::AC-175: карточка после «Убрать из списка» — «Не подключён» и остаётся в витрине` | ✅ |
| AC-176 | `oc/orchestration.test.ts::AC-176: недоступный Hub не откатывает локальные шаги — hub_error рядом с removed:true`; `…::AC-176: без ключа magnit_prod шаг обращения к Hub пропускается…`; `oc/routes.test.ts::AC-175, AC-176: ответ «Убрать из списка» описан схемой с removed и необязательным hub_error` | ✅ |
| AC-177 | `oc/status.test.ts::AC-177: «Подключено» — «Отключить», «Права», «Открыть в Hub»; «Подключить» нет`; `app/connectors-view.test.ts::AC-177: бейдж «Подключено» — тот же элемент и размер, что бейдж «устаревший»`; `…::AC-177: подпись статуса сохраняется`; `…::AC-177: у подключённой карточки нет кнопки «Подключить»` | ✅ |
| AC-178 | `oc/status.test.ts::AC-178: после «Отключить» подпись — «Отключено вами», действия — «Повторить» и «Убрать из списка»`; `…::AC-178: подписи состояния 3 различают поломку связи и собственное отключение`; `oc/orchestration.test.ts::AC-188: «Отключить» признак не снимает…` (перезапуск смоделирован повторным чтением файла) | ✅ |
| AC-179 | `oc/connections.test.ts` — 6 тестов: файла нет; повреждённый файл (4 вида) трактуется как отсутствующий; перезапись при следующей записи; битые записи внутри целого файла; признак по данным Hub без файла; ложный признак без файла и без `connection` | ✅ |
| AC-180 | `oc/errors.test.ts::AC-180: отказ авторизации во всех трёх видах даёт token_rejected`; `…::AC-180: класс — одно из четырёх слов, тело ответа Hub и секреты в него не попадают`; `oc/routes.test.ts::AC-180: ответ connect несёт класс ошибки подключения`; `oc/orchestration.test.ts::AC-180: отказ авторизации даёт token_rejected во всех трёх видах, секретов в ответе нет` | ✅ |
| AC-181 | `oc/errors.test.ts::AC-181: отказ регистрации клиента и needs_client_registration дают method_unavailable`; `oc/orchestration.test.ts::AC-181: отказ регистрации клиента даёт method_unavailable`; `…::AC-181: карточки нет в каталоге — подключать этим способом нечем` | ✅ |
| AC-182 | `oc/errors.test.ts::AC-182: сеть, таймаут и 5xx дают hub_unreachable`; `oc/orchestration.test.ts::AC-182: сеть и Hub дают hub_unreachable`; `oc/upgrade.test.ts::AC-182, AC-195: недоступный источник отличим от неразобранного ответа` | ✅ |
| AC-183 | `oc/errors.test.ts::AC-183: неотнесённая ошибка получает unknown…`; `…::AC-183: класс определяется при любом входе — состояния «ошибка без объяснения» нет`; `oc/orchestration.test.ts::AC-183: неотнесённая ошибка получает unknown, но объяснение и код остаются` | ✅ |
| AC-184 | `app/connectors-view.test.ts` — 10 тестов в обеих ветках флага: ноль/одна/полсотни карточек дают одну и ту же высоту панели; баннер, предупреждение и пустое состояние высоту не меняют; корп-окно и окно настроек одной ветки объявлены одним размером; список прокручивается внутри окна; `fit` не передаётся; общие стили `packages/ui` не правятся | ✅ |
| AC-185 | `tui/dialog-actions.test.ts::AC-185: четыре состояния приходят из общего модуля и различаются набором действий` (входы AC-170, AC-171, AC-173, AC-177); `…::AC-185: enter доступен в состояниях 1–3 и недоступен у подключённого`; `…::AC-185: подпись enter — «Повторить» в состоянии 3 и «Подключить» в состояниях 1 и 2`; `…::AC-185, S-V18: подключённая карточка помечена маркером, читаемым без цвета`; `…::AC-185: подписи состояний берутся из словаря TUI по состоянию карточки` | ✅ |
| AC-186 | `tui/dialog-actions.test.ts::AC-186: клавиша d действует только в состоянии «Подключено»`; `…::AC-186: клавиша x действует только в состоянии «Соединение потеряно»`; `…::AC-186: нажатие d вне состояния «Подключено» ничего не меняет и объясняет почему`; `…::AC-186: клавиши d и x объявлены и подчинены набору действий карточки` | ✅ |
| AC-187 | `app/dictionary.test.ts::AC-187: все тринадцать ключей есть в en.ts и в ru.ts`; `…::AC-187: значения ru не равны значениям en`; `…::AC-187: те же тексты есть в словаре TUI`; `…::AC-187: у класса unknown есть свой текст` | ✅ |
| AC-188 | `oc/status.test.ts` — 4 теста (каждая строка S-V16 достижима; ни одно состояние 1–3 не содержит `disconnect`; состояния 2 и 3 различаются только признаком при одном локальном статусе; признак по локальному `connected` и по `connection.status`); `oc/connections.test.ts` — 3 теста (запись переживает перезапуск, снятие признака); `oc/orchestration.test.ts::AC-188: «Отключить» признак не снимает, «Убрать из списка» — снимает`; `…::AC-188: признак выставляется и по записи подключения Hub, без локальной истории` | ✅ |
| AC-189 | `desk/electron-builder.config.corp.test.ts::AC-189: канал magnit получает собственное имя пакета и сохраняет desktopName из getBase()`; `…::AC-189: прочие проверки идентичности канала magnit продолжают выполняться` | ✅ |
| AC-190 | `desk/electron-builder.config.corp.test.ts::AC-190, AC-192: extraMetadata ванильных каналов не изменён — поля name в нём нет`; `…::AC-190: правка ограничена веткой magnit` | ✅ |
| AC-191 (manual) | Автоматизирован инструмент ручной проверки: `corp/verify-desktop-bundle.test.ts` — 4 теста (корпоративный каталог кеша не расхождение; общий `@opencode-aidesktop-updater` — расхождение; чужое имя каталога — расхождение; имя выводится из `extraMetadata.name`). Сам осмотр бандла на стенде — вне автотестов | ✅ (инструмент) |
| AC-192 | `desk/electron-builder.config.corp.test.ts::AC-192: случаи каталога кеша добавлены к покрытию AC-152, прежние случаи сохранены` + сами случаи AC-189/AC-190 в том же файле | ✅ |
| AC-193 | `oc/upgrade.test.ts::AC-193: ни одного сетевого запроса, ни одного менеджера пакетов, сообщение и код выхода 1` (реальный процесс `opencode upgrade` под перехватчиком запросов и с заглушками менеджеров пакетов в `PATH`); `…::AC-193: молчаливого «уже последняя версия» не бывает`; `…::AC-193: корп-функции включены, адрес не задан — апгрейд выключен` | ✅ |
| AC-194 | `oc/upgrade.test.ts::AC-194: версия берётся с <адрес>/latest; совпадение с текущей пропускает апгрейд без установки`; `…::AC-194: единственный запрос определения версии — GET <адрес>/latest`; `…::AC-194: артефакт скачивается с того же хоста и заменяет бинарник`; `…::AC-194: адрес задан — источник только внутренний, хвостовые слэши отброшены` | ✅ |
| AC-195 | `oc/upgrade.test.ts::AC-195: ответ источника не по схеме — понятная ошибка, код 1, запасного источника нет`; `…::AC-195: недоступный источник тоже даёт код 1 и не идёт на api.github.com`; `…::AC-195: ответ не по схеме даёт понятную ошибку источника…` (5 форм тела) | ✅ |
| AC-196 | `oc/upgrade.test.ts::AC-196: без корп-функций версия определяется прежним путём, корп-сообщение не печатается` (запрос уходит на `api.github.com`, код выхода прежний); `…::AC-196: без корп-функций план — upstream` | ✅ |
| AC-197 | `desk/src/main/updater.corp.test.ts::AC-197: упакованный канал magnit даёт заголовок «OpenCode Magnit»` | ✅ |
| AC-198 | `desk/src/main/updater.corp.test.ts::AC-198: все четыре канала берут имя из одной таблицы`; `…::AC-198: неупакованный запуск сохраняет прежнее поведение разработки`; `…::AC-198, AC-199: второй копии разбора канала и второй таблицы имён в главном процессе нет` | ✅ |
| AC-199 | `oc/upgrade.test.ts::AC-199: перечень запрещённых хостов объявлен модулем и совпадает с перечнем теста` + проверка всех перехваченных запросов во всех тестах команды (`expectNoForbiddenHosts`); `desk/src/main/updater.corp.test.ts::AC-199: в windows.ts нет литерала «OpenCode» в качестве значения title` | ✅ |
| AC-52 (не менялся ревизией 1.9) | `oc/status.test.ts::AC-52: needs_reauth поверх работающего локального соединения тоже даёт повторную авторизацию` | ❌ **BUG-I4-011** |

## 3. Судьба диспутов

### `test-dispute-i4-sv16-card-actions.json` — **принят полностью**

Резолюция: `disputes/test-dispute-i4-sv16-card-actions.resolution.json`.

Пять проверок закрепляли столбец «Действия» прежней редакции S-V6 — ровно то, что ревизия 1.9
объявила дефектом (`BUG-I4-010`). Все пять приведены к контракту S-V16 по предложенным
переформулировкам; ни один идентификатор AC не удалён и не ослаблен — в каждом месте число
проверяемых утверждений выросло:

| Тест | Было | Стало |
|---|---|---|
| AC-51 | `actions === ["reconnect","disconnect"]` | пара случаев по признаку S-V15: `["connect","open_hub"]` / `["reconnect","forget","open_hub"]`, в обоих запрещён `disconnect`; статус и текст ошибки сохранены дословно |
| AC-52 | `actions === ["connect","disconnect"]` | статусы сохранены дословно; действия — парой по признаку; отсутствие `disconnect` в каждом случае |
| AC-44 (stale) | `stale.actions === ["disconnect"]` | `["forget","open_hub"]` у состояния 3 и `["open_hub"]` у сервера без признака; блокировка `connect`/`reconnect`/`permissions` сохранена |
| AC-163 | `actions === ["connect","open_hub"]` | предмет теста (карточка не отброшена, бейджа нет, статус `not_connected`) + `ever_connected === true` и набор состояния 3 |
| AC-156/AC-157 | `actions === ["connect","open_hub"]` | «Права» недоступны, «Открыть в Hub» доступно, подключение доступно; набор состояния 3 с объяснением |

### `test-dispute-i4-ac38-catalog-card-shape.json` — **принят**

Резолюция: `disputes/test-dispute-i4-ac38-catalog-card-shape.resolution.json`.

Фикстура карточки в тесте AC-38 дополнена обязательными полями `state: "never"` и
`ever_connected: false`; проверка конверта витрины сохранена дословно. Дополнительно закреплено то,
на чём держится запрет S-T10 «оболочка состояние не вычисляет»: карточка **без** `state` или **без**
`ever_connected` схемой не принимается, чужое значение `state` не принимается, множество
`CardAction` пополнено значением `forget` при сохранённых прежних значениях. Заодно закреплены
схемы `ForgetResult` и `error_class` в `ConnectorResult`, а также маршрут `DELETE /corp/connectors/:alias`.

## 4. Найденные дефекты реализации

### `BUG-I4-011` (severity `high`) — состояние карточки не учитывает `connection.status = needs_reauth`

`bugs/BUG-I4-011.json`. Два симптома одного корня — вычисление `state` в
`packages/opencode/src/corp/status.ts` смотрит на `local` и `configured`, но не на `connection.status`:

1. **Потеряна повторная авторизация.** Карточка с `connection.status = "needs_reauth"` при работающем
   локальном соединении (`local = "connected"` — штатный исход S-V9/AC-64: пресет расширен
   `readonly → readwrite`) получает `status = "needs_auth"`, но `state = "connected"` и
   `actions = ["permissions","disconnect","open_hub"]`. Ни `connect`, ни `reconnect` в наборе нет —
   пройти повторную авторизацию нечем ни в Desktop, ни в TUI. AC-52 (ревизией 1.9 не менялся) требует
   «во всех трёх случаях статус `needs_auth` **с действием повторной авторизации**». Дополнительно
   карточка получает бейдж «Подключено» (S-V18 ставит его по `state === "connected"`) рядом с
   подписью «Требуется авторизация» — то самое смешение состояний, ради устранения которого сделана
   ревизия 1.9. До ревизии набор действий считался по статусу и был `["connect","disconnect"]`, то
   есть повторная авторизация была доступна — это регрессия.
2. **Подмена подписи состояния.** Та же карточка с записью `mcp.<alias>` в конфиге, но без локального
   статуса (обычное состояние сразу после перезапуска) получает `state = "disconnected"` —
   «Отключено вами», хотя пользователь ничего не отключал. S-V16 прямо относит `needs_reauth` к
   подписи «Соединение потеряно» и отдельно запрещает путать поломку с собственным решением
   пользователя. Набор действий при этом верный.

Минимальные воспроизводящие падающие тесты — в сьюте (`oc/status.test.ts`, два теста выше в таблице).

### Наблюдение без баг-репорта

`S-D10` говорит «правило относится ко всем корп-окнам **на `CorpDialog`** — витрине, экрану входа,
экрану прав». Экран входа `packages/app/src/components/corp/dialog-corp-login.tsx` на `CorpDialog`
не переведён: он по-прежнему открывается напрямую через `Dialog size="large"`. Формулировка правила
условная («на `CorpDialog`»), AC-184 экрана входа не касается, поэтому дефектом это не оформлено —
но при следующей ревизии стоит либо перевести экран входа на `CorpDialog`, либо убрать его из
перечня в S-D10.

## 5. Прогоны

| Прогон | Результат |
|---|---|
| `bun --cwd packages/opencode test src/corp ../../corp/patches.test.ts ../../corp/verify-desktop-bundle.test.ts ../../packages/desktop/electron-builder.config.corp.test.ts ../../packages/desktop/src/main/updater.corp.test.ts` | 372 теста, 370 pass, 2 fail (оба — минимальные воспроизведения `BUG-I4-011`) |
| `bun --cwd packages/opencode test ../../corp/patches.test.ts` (обязателен) | 12 pass, 0 fail |
| `bun --cwd packages/opencode test ../../corp/verify-desktop-bundle.test.ts` | 19 pass, 0 fail |
| `bun --cwd packages/opencode test` (полный) | 3363 pass, 22 skip, 1 todo, 3 fail: 2 × `BUG-I4-011` + 1 базовая флака `v2 pty HttpApi` |
| `bun --cwd packages/app run test:unit` (полный) | 453 pass, 0 fail |
| `bun --cwd packages/tui test` (полный) | 219 pass, 1 skip, 8 fail — все 8 базовые |

Диспутные падения: было 6, стало 0.

```
до:  (fail) AC-51: строка 3 …; AC-52: строка 4 …; AC-44: строка 2 …;
     (fail) AC-163: карточка со status=sunset …;
     (fail) AC-38: витрина отдаёт источник, признак протухания и карточки;
     (fail) AC-156, AC-157: карточка с непонятой моделью прав …
после: —
```

## 6. Известные базовые падения (не мои и не dev-agent)

Все воспроизводятся на базовом коммите `a1ad60532d`, то есть до реализации ревизии 1.9.

### `packages/tui` — 8 падений, sync/hydration

Прогон на `a1ad60532d`: `bun --cwd packages/tui test` → **210 pass, 1 skip, 8 fail**.
Прогон на `e76b08e08a` (с моими тестами): **219 pass, 1 skip, 8 fail** — тот же список, +9 моих зелёных.

```
(fail) tui sync > refresh scopes sessions by default and lists project sessions when disabled
(fail) tui sync > vcs branch updates only apply for the active workspace
(fail) tui sync (#26560) > entering a session whose messages endpoint errors does not crash sync
(fail) stale session hydration does not overwrite live message parts
(fail) orphan live deltas do not suppress hydrated parts
(fail) hydration does not clear text streamed before it starts
(fail) live messages merged during hydration retain the 100 message window
(fail) a message removed during hydration does not regain stale parts
```

Причина одна и та же: `error: Exit context must be used within a context provider` в
`packages/tui/src/context/sync.tsx:319` — тестовая обвязка не подставляет провайдер контекста
`Exit`. К корп-слою отношения не имеет.

### `packages/opencode` — `v2 pty HttpApi`, флака по времени

Прогон на `a1ad60532d` (изолированно): `bun --cwd packages/opencode test test/server/httpapi-v2-location.test.ts`
→ **1 pass, 1 fail**: `v2 location HttpApi > streams native EventV2 payloads with resolved locations`,
`error: Missing key at ["location"]["project"]`. В полном прогоне того же базового коммита этот тест
проходит — падение зависит от порядка/окружения, а не от кода ревизии 1.9.

Прогоны полного сьюта `packages/opencode` на базовом коммите:

| Прогон базового коммита | Результат |
|---|---|
| №1 | 3286 pass, 0 fail |
| №2 | 3284 pass, 2 fail: `v2 pty HttpApi > applies plugin shell environment before forced PTY values`, `v2 pty HttpApi > serves location-wrapped PTY routes and retains exited sessions` |

То есть тесты `v2 pty HttpApi` **нестабильны на базовом коммите**: они опрашивают порождённый
процесс `env sh -c "exit 4"` в цикле с ограничением по времени и под нагрузкой не успевают увидеть
`status: "exited"` (в отчёте — `Received: {status: "running", …}`). В изолированном прогоне на моей
ветке тест проходит (`4 pass, 0 fail`). Классификация — `flake` (время/нагрузка), к ревизии 1.9 и к
моим тестам отношения не имеет; вслепую не ретраился.

## 7. Что добавлено

| Файл | Тестов было | Стало | Новых |
|---|---|---|---|
| `packages/opencode/src/corp/connections.test.ts` *(новый)* | — | 15 | 15 |
| `packages/opencode/src/corp/status.test.ts` | 21 | 34 | 13 |
| `packages/opencode/src/corp/connectors.test.ts` | 27 | 29 | 2 |
| `packages/opencode/src/corp/errors.test.ts` | 6 | 12 | 6 |
| `packages/opencode/src/corp/routes.test.ts` | 14 | 19 | 5 |
| `packages/opencode/src/corp/orchestration.test.ts` | 24 | 36 | 12 |
| `packages/opencode/src/corp/upgrade.test.ts` *(новый)* | — | 14 | 14 |
| `packages/opencode/test/fixture/corp-upgrade-interceptor.ts` *(новый)* | — | — | перехватчик запросов |
| `packages/desktop/electron-builder.config.corp.test.ts` | 24 | 29 | 5 |
| `packages/desktop/src/main/updater.corp.test.ts` | 15 | 20 | 5 |
| `corp/verify-desktop-bundle.test.ts` | 15 | 19 | 4 |
| `packages/tui/src/corp/dialog-actions.test.ts` | 16 | 25 | 9 |
| `packages/app/src/corp/dictionary.test.ts` | 9 | 13 | 4 |
| `packages/app/src/corp/connectors-view.test.ts` *(новый)* | — | 18 | 18 |
| **Итого** | | | **112** |

Код продукта (`src/`) не правился. Правки в существующих тестах — только по двум разрешённым
диспутам; ни один идентификатор AC не удалён и не ослаблен. Единственная правка тестовой обвязки вне
диспутов: в `orchestration.test.ts` `Global.Path.data` теперь тоже подменяется временным каталогом —
без этого признак «подключение состоялось» (S-V15) писался бы в настоящий профиль пользователя.
