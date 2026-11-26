# Агентская верификация приложений (UI/UX и функционал)

Регламент запуска ИИ-агента-тестировщика перед выдачей debug-сборки владельцу или перед prod-релизом. Архитектурное решение зафиксировано в [ADR-0001](../docs/adr/0001-agent-app-verification-harness.md).

## Архитектура стенда

1. **Быстрый слой (VPS, без эмулятора):** Compose UI / screenshot-тесты на JVM.
2. **Живой стенд (`ssh home`, KVM):** headless Android Emulator на `home-server` (`/dev/kvm`), развёрнутый строго на HDD `/mnt/data/android/` (SSD `/dev/sda2` не трогаем). Доступен с VPS по SSH-туннелю `adb`.
3. **Опционально:** T3 Device Hub / реальный телефон (только для проверок реальной сети/VPN/SIM).

## Ручки агента

- **Тонкий CLI стенда (`scripts/app-verify.py`):** платформо-независимый верхний уровень (`--platform android`, расширяемый под веб/боты) + адаптер Android KVM на `ssh home` (жёсткий инвариант HDD `/mnt/data/android/`):
  - `python3 scripts/app-verify.py --json platforms` — список зарегистрированных платформенных адаптеров.
  - `python3 scripts/app-verify.py --json prepare` — проверить HDD-инвариант, SDK, AVD и снапшот `clean-base`.
  - `python3 scripts/app-verify.py --json start` (`up`) — поднять или откатить эмулятор к `clean-base`, открыть SSH-туннель ADB (`127.0.0.1:15565`) и дождаться состояния `device`.
  - `python3 scripts/app-verify.py --json status` — статус эмулятора, туннеля и локального `adb`.
  - `python3 scripts/app-verify.py --json install <path/to/app-debug.apk>` — установить debug APK.
  - `python3 scripts/app-verify.py --json launch <package>` — запустить приложение (`am start -W`).
  - `python3 scripts/app-verify.py --json screenshot <out.png>` — снять и провалидировать PNG-скриншот.
  - `python3 scripts/app-verify.py --json logcat [--package <pkg>] [--output <log.txt>] [--fail-on-error]` — выгрузить `logcat` и проверить инвариант `0 crash / 0 ANR`.
  - `python3 scripts/app-verify.py --json snapshot-save [name]` — сохранить чистый базовый снапшот на HDD.
  - `python3 scripts/app-verify.py --json stop` (`down`) — остановить эмулятор и закрыть SSH-туннель.
- **UI-драйвер (`@mobilenext/mobile-mcp@1.0.20`):** чтение компактного accessibility-дерева с `@eN`-ссылками (`ref`, без привязки к координатам), тапы по `ref`, ввод текста, свайпы, аппаратные кнопки (`BACK`/`HOME`/`ENTER`) и снятие скриншотов поверх проброшенного ADB (`127.0.0.1:15565`):
  - `python3 scripts/app-verify.py ui-driver mcp-config` — сгенерировать JSON-фрагмент `mcpServers` для подключения MCP-клиента агента.
  - `python3 scripts/app-verify.py --json ui-driver doctor [--probe-mcp] [--require-device]` — проверить локальный `adb`, `ANDROID_HOME`, Node/`npx` и (с `--probe-mcp`) выполнить реальное MCP-рукопожатие и опрос устройств; при устаревшем фоновом демоне `mobilecli` автоматически перезапускает его, если `adb devices` уже видит устройство `online`.
  - `python3 scripts/app-verify.py ui-driver serve [--listen <host:port>]` — запустить `@mobilenext/mobile-mcp` в режиме `--stdio` (по умолчанию) или Streamable HTTP с гарантированным окружением.
  - `python3 scripts/app-verify.py --json ui-driver call <tool> [--args-json '<json>'] [--save-image <path>]` — выполнить одиночный вызов MCP-инструмента (автоматически подставляет `device=127.0.0.1:15565`, если он не указан явно; `--save-image` сохраняет PNG из ответа `mobile_take_screenshot`).
- **White-box debug-хуки (`ru.rumap.android.debug`):** вызов `am broadcast` к `DebugVerificationReceiver` debug-сборки (в `release` физически отсутствует):
  - `python3 scripts/app-verify.py --json debug navigate --target <target>` — перейти на экран/диалог (`home`, `main`, `check`, `settings`, `region`, `anchors`, `diaglog`, `benchmark`, `calibration`, `calibration_advanced`, `update`, `advanced`, `help`, `battery_onboarding`, `region_onboarding`, `mandatory_update`).
  - `python3 scripts/app-verify.py --json debug mock-state --preset <preset> [--error-message <msg>]` — мок состояния (`clear`, `none`, `offline`, `degraded_cellular`, `vpn_active`, `center_sync_error`).
  - `python3 scripts/app-verify.py --json debug reset-state` — сбросить локальную БД, настройки и debug-overlay.
  - `python3 scripts/app-verify.py --json debug dump-diagnostics [--output <local.json>] [--device-output-path <path>]` — read-only дамп диагностики приложения (локальное сохранение на VPS через `--output`; запись в `cacheDir` на устройстве через `--device-output-path`, алиас `--output-path`).
  - Все подкоманды принимают `--device <serial>` и `--package <pkg>` (по умолчанию `ru.rumap.android.debug`).
- **Программные профили БС (`net`):** эмуляция белых списков на сотовом интерфейсе эмулятора через отдельную `iptables`-цепочку `RUMAP_WL` (подробности ниже).
- **Отчёт и GitHub Issues:** `report` рендерит Markdown/сводку из `run.json`, `issues dedup` сверяет находки с открытыми issue, `issues create` заводит issue только после `--confirm-owner-approval` (см. ниже).

## Программная эмуляция белых списков (`net`)

На стенде `ssh home` нет реальных операторских БС, поэтому `net profile` создаёт отдельную
`iptables`-цепочку `RUMAP_WL` и подключает её к `OUTPUT` строго через сотовый интерфейс
`$CELL_IF` (определяется динамически из `dumpsys connectivity`: `radio0`, `rmnet_data0` и т.п.).
Loopback, Wi-Fi (`lo`/`wlan*`), VPN (`tun*`) и диапазон эмулятора `10.0.2.0/24` не затрагиваются;
внутри цепочки первыми всегда стоят страховочные `RETURN` для `127.0.0.0/8` и `10.0.2.0/24`.
Перед изменением правил CLI сам выполняет `adb root`, ждёт устройство и проверяет `id -u == 0`.

Профили:

| Профиль | Что делает |
| --- | --- |
| `off` | Снимает все ссылки `OUTPUT -> RUMAP_WL`, очищает и удаляет цепочку (идемпотентно). |
| `soft` | Пропускает TCP:443 только к ориентирам из белого списка + `--allow-ip`, остальной TCP:443 режет (`DROP`), включая контролы `1.1.1.1`, `8.8.8.8`, `9.9.9.9`, `208.67.222.222`. |
| `hard` | Режет весь TCP:443; ICMP Echo (`echo-request`) разрешён только к белому списку + `--allow-ip`, остальной режется. |
| `down` | Режет TCP:443 и ICMP Echo полностью. |

```bash
# Применить профиль; --allow-ip можно повторять или перечислять через запятую
python3 scripts/app-verify.py --json net profile soft \
  --allow-ip 1.2.3.4 --allow-ip 5.6.7.8 --wifi off
python3 scripts/app-verify.py --json net profile hard --wifi on --device 127.0.0.1:15565

# Текущий профиль по фактическим правилам, $CELL_IF, Wi-Fi, сотовая сеть и VPN
python3 scripts/app-verify.py --json net status

# Гарантированно снять ограничения и вернуть Wi-Fi
python3 scripts/app-verify.py --json net reset
```

Встроенный белый список (как в `go/orchestrator/anchors.go`): `77.88.55.242`,
`87.240.132.78`, `185.71.66.66`, `178.248.232.209`. `--allow-ip` расширяет его без дублей и
валидируется как IPv4 (некорректный адрес возвращает `StandError`). `--wifi on|off` вызывает
`svc wifi enable|disable`. При переключении профилей старые правила и ссылки удаляются до
создания новой цепочки, поэтому дубли не накапливаются. `net status` возвращает `profile`
(`off`/`soft`/`hard`/`down`/`custom`), `allowed_ips`, `cell_if`, `wifi_enabled`,
`wifi_connected`, `wifi_interface`, `cellular_connected`, `vpn_active` и `vpn_interface`.

## Конфигурация UI-драйвера и переменные окружения

На VPS `adb` установлен в `/opt/android-sdk/platform-tools/adb` и по умолчанию отсутствует в `PATH` и `ANDROID_HOME`. Все команды `ui-driver` автоматически формируют и передают процессу `@mobilenext/mobile-mcp` следующее окружение:

- `PATH` — директория найденного `adb` (`/opt/android-sdk/platform-tools`) добавляется в начало `PATH`.
- `ANDROID_HOME` / `ANDROID_SDK_ROOT` — корень SDK (`/opt/android-sdk`), выводится автоматически из пути к `adb`.
- `MOBILECLI_DEVICE` — целевой ADB serial проброшенного эмулятора (по умолчанию `127.0.0.1:15565`, либо `127.0.0.1:<local_adb_port>` / `--device`).
- `MOBILEMCP_DISABLE_TELEMETRY=1` и `DO_NOT_TRACK=1` — отключение телеметрии драйвера.

Ключевые MCP-инструменты для работы без хрупких координат:
- `mobile_list_elements_on_screen` — получить accessibility-дерево элементов с `@eN` refs.
- `mobile_click_on_screen_at_coordinates` (с аргументом `{"ref": "@eN"}`) — тап по элементу по `ref`.
- `mobile_swipe_on_screen` (`{"direction": "up"|"down"|"left"|"right"}`) — свайп по экрану.
- `mobile_type_keys` (`{"text": "..."}`) — ввод текста в сфокусированное поле (поддерживает и кириллицу).
- `mobile_press_button` (`{"button": "BACK"}`) — нажатие системной кнопки (`BACK`, `HOME`, `ENTER` и др.).
- `mobile_save_screenshot` (`{"saveTo": "shot.png"}`) / `mobile_take_screenshot` — сохранение или получение скриншота.

## Базовый end-to-end smoke-сценарий

```bash
# 1. Поднять эмулятор на ssh home (из чистого снапшота) и пробросить ADB
python3 scripts/app-verify.py --json start

# 2. Убедиться, что UI-драйвер и проброшенное устройство online
python3 scripts/app-verify.py --json ui-driver doctor --probe-mcp --require-device

# 3. Установить debug APK и запустить приложение
python3 scripts/app-verify.py --json install /root/rumap-android/app/build/outputs/apk/debug/app-debug.apk
python3 scripts/app-verify.py --json launch ru.rumap.android.debug

# 4. Пройти UI-действия через @eN refs (дерево, тап, свайп, ввод, BACK, скриншот)
python3 scripts/app-verify.py --json ui-driver call mobile_list_elements_on_screen
python3 scripts/app-verify.py --json ui-driver call mobile_save_screenshot --args-json '{"saveTo":"smoke.png"}'
python3 scripts/app-verify.py --json ui-driver call mobile_click_on_screen_at_coordinates --args-json '{"ref":"@e7"}'
python3 scripts/app-verify.py --json ui-driver call mobile_swipe_on_screen --args-json '{"direction":"up"}'
python3 scripts/app-verify.py --json ui-driver call mobile_type_keys --args-json '{"text":"Москва"}'
python3 scripts/app-verify.py --json ui-driver call mobile_press_button --args-json '{"button":"BACK"}'

# 5. Проверить инвариант 0 crash / 0 ANR и обязательно остановить стенд
python3 scripts/app-verify.py --json logcat --package ru.rumap.android.debug --fail-on-error
python3 scripts/app-verify.py --json stop
```

## Порядок прогона (по требованию)

1. Подготовить чистый снапшот эмулятора и установить актуальную `debug`-сборку.
2. Запустить свободное исследование агентом по ключевым сценариям и краевым состояниям (с использованием white-box ручек для моков сети/состояний и сброса БД).
3. Собрать структурированный отчёт:
   - Инварианты (0 крашей, 0 ANR, базовые сценарии).
   - Найденные проблемы с severity (`Blocker` / `Major` / `Minor` / `UX polish`), шагами воспроизведения, скриншотами и фрагментами `logcat`.
   - Оценка по рубрике: **Функционал / UI / UX / Стабильность**.
4. Предложить владельцу список кандидатов в GitHub Issues (после дедупликации по открытым issue). **Заводить issue только после явного «ок» владельца.**

Полный пошаговый протокол прогона (предусловия, чеклист сценариев, обязательный cleanup, схема `run.json`, owner-gate) зафиксирован в скилле [skills/app-verifier/SKILL.md](../skills/app-verifier/SKILL.md).

## Отчёт, дедупликация и owner-gated Issues

```bash
# 1. Markdown-отчёт и краткая сводка для чата T3 (run.json — результат прогона)
python3 scripts/app-verify.py report --input .scratch/app-verify-<ts>/run.json \
  --open-issues .scratch/app-verify-<ts>/open-issues.json \
  --output .scratch/app-verify-<ts>/report.md
python3 scripts/app-verify.py report --input .scratch/app-verify-<ts>/run.json --chat-summary

# 2. Дедупликация по открытым issue (из файла или напрямую через gh)
python3 scripts/app-verify.py --json issues dedup --input .scratch/app-verify-<ts>/run.json \
  --repo <owner/repo>

# 3. План без создания (dry-run) и создание строго после подтверждения владельца
python3 scripts/app-verify.py --json issues create --input .scratch/app-verify-<ts>/run.json \
  --repo <owner/repo> --open-issues .scratch/app-verify-<ts>/open-issues.json \
  --approve-all-new --dry-run
python3 scripts/app-verify.py --json issues create --input .scratch/app-verify-<ts>/run.json \
  --repo <owner/repo> --open-issues .scratch/app-verify-<ts>/open-issues.json \
  --approve M1,U1 --confirm-owner-approval
```

Правила вердикта: `BLOCKED` при крашах/ANR/находках `Blocker`, `CONDITIONAL` при `Major`
или любой оси рубрики `<= 3`, иначе `READY`. Дедупликатор делит находки на точные дубли,
возможные дубли (оба класса пропускаются при `--approve-all-new`, но могут быть явно
переопределены владельцем через `--approve <id>`) и новые кандидаты. Новым багам по умолчанию
добавляется метка `status:needs-triage` без `assignee`. Без `--confirm-owner-approval` команда
`issues create` завершается ошибкой и не вызывает `gh issue create`; `--dry-run` только
показывает план.
