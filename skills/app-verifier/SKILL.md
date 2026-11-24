---
name: app-verifier
description: "Запуск по требованию агентской UI/UX и функциональной верификации приложения на живом стенде (`ssh home`): исследовательский прогон debug-сборки, white-box хуки, отчёт с инвариантами и рубрикой, дедупликация и owner-gated GitHub Issues. Use before handing a debug build to the owner or before a prod release."
---

# Агент-верификатор приложения

Скилл проводит полный исследовательский прогон приложения на стенде и превращает его в
структурированный отчёт с вердиктом готовности. Заведение GitHub Issues — только после
явного подтверждения владельца.

## Когда запускать

- Владелец просит проверить свежую debug-сборку перед ручной проверкой.
- Перед prod-релизом нужен независимый UI/UX и функциональный прогон.
- Нужно воспроизвести сложное сетевое состояние (`offline`, `degraded_cellular`,
  `vpn_active`, `center_sync_error`) без реального радиомодуля.

Не запускай живой прогон в pre-commit или в CI на каждый PR: только по требованию.

## Предусловия

- Репозиторий `harness-engineering` и собранный debug APK `rumap-android`
  (`app/build/outputs/apk/debug/app-debug.apk`).
- Доступ по SSH к `home` (`ssh home`) и запущенный headless KVM-эмулятор на HDD
  `/mnt/data/android/` (SSD не трогаем).
- `python3` и `gh` (для дедупликации/создания issue), Node/`npx` для `@mobilenext/mobile-mcp`.

## Протокол прогона

### 1. Подготовка стенда

```bash
python3 scripts/app-verify.py --json prepare
python3 scripts/app-verify.py --json start
python3 scripts/app-verify.py --json ui-driver doctor --probe-mcp --require-device
```

`start` идемпотентно откатывает эмулятор к чистому снапшоту и открывает ADB-туннель
`127.0.0.1:15565`. Если `prepare`/`start` падают на инварианте HDD — остановись и
сообщи владельцу, не пытайся писать на SSD.

### 2. Установка debug-сборки

```bash
python3 scripts/app-verify.py --json install /root/rumap-android/app/build/outputs/apk/debug/app-debug.apk
python3 scripts/app-verify.py --json launch ru.rumap.android.debug
python3 scripts/app-verify.py --json logcat --clear
```

Зафиксируй в отчёте сборку и коммит под тестом. Все дальнейшие шаги выполняй на пакете
`ru.rumap.android.debug`.

### 3. Исследовательский прогон

Работай через UI-драйвер (accessibility-дерево и `@eN` refs, без хрупких координат):

```bash
python3 scripts/app-verify.py --json ui-driver call mobile_list_elements_on_screen
python3 scripts/app-verify.py --json ui-driver call mobile_click_on_screen_at_coordinates --args-json '{"ref":"@e7"}'
python3 scripts/app-verify.py --json ui-driver call mobile_type_keys --args-json '{"text":"Москва"}'
python3 scripts/app-verify.py --json ui-driver call mobile_swipe_on_screen --args-json '{"direction":"up"}'
python3 scripts/app-verify.py --json ui-driver call mobile_press_button --args-json '{"button":"BACK"}'
python3 scripts/app-verify.py --json ui-driver call mobile_save_screenshot --args-json '{"saveTo":"home.png"}'
```

Минимальный чеклист сценариев и краевых состояний:

- Первый запуск, онбординг батареи и региона, пустые состояния.
- Главный экран, вкладки «Проверка» и «Настройки», переходы назад.
- Диалоги: регион, точки (anchors), журнал диагностики, бенчмарк, калибровка и
  расширенная калибровка, обновление, расширенные настройки, помощь.
- Моки сети/синхронизации: `offline`, `degraded_cellular`, `vpn_active`,
  `center_sync_error`, сброс в `clear`.
- Сброс локального состояния и повторный первый запуск.

White-box хуки debug-сборки (действуют только в `ru.rumap.android.debug`):

```bash
# Переход на экран/диалог (без длинного онбординга)
python3 scripts/app-verify.py --json debug navigate --target settings
python3 scripts/app-verify.py --json debug navigate --target update

# Мок сетевого/синхронизационного состояния
python3 scripts/app-verify.py --json debug mock-state --preset offline
python3 scripts/app-verify.py --json debug mock-state --preset center_sync_error --error-message "Центр недоступен"
python3 scripts/app-verify.py --json debug mock-state --preset clear

# Сброс локальной БД/настроек и debug-overlay
python3 scripts/app-verify.py --json debug reset-state

# Read-only дамп диагностики
python3 scripts/app-verify.py --json debug dump-diagnostics --output-path diag.json
```

Допустимые `--target`: `home`, `main`, `check`, `settings`, `region`, `anchors`,
`diaglog`, `benchmark`, `calibration`, `calibration_advanced`, `update`, `advanced`,
`help`, `battery_onboarding`, `region_onboarding`, `mandatory_update`.
Допустимые `--preset`: `clear`, `none`, `offline`, `degraded_cellular`, `vpn_active`,
`center_sync_error`. Если команда `debug` завершилась с ненулевым exit code или
`ok: false`, это дефект хука — зафиксируй его как находку.

Для каждой находки сохраняй: экран, шаги воспроизведения, ожидаемое и фактическое
поведение, скриншот и фрагмент `logcat`. Складывай артефакты в
`.scratch/app-verify-<ts>/`.

### 4. Инварианты стабильности

```bash
python3 scripts/app-verify.py --json logcat --package ru.rumap.android.debug \
  --output .scratch/app-verify-<ts>/logcat.txt --fail-on-error
```

Инвариант: `0` крашей и `0` ANR. Ненулевой код `--fail-on-error` — это `Blocker` в отчёте.

### 5. Обязательный cleanup

Останавливай стенд **всегда**, даже при падении прогона (оберни шаги 2–4 в
`trap`/`try/finally`):

```bash
python3 scripts/app-verify.py --json stop
```

Проверь, что эмулятор и SSH-туннель действительно остановлены.

### 6. Отчёт, сводка и дедупликация

Сформируй `run.json` со схемой:

```json
{
  "app": "rumap-android",
  "platform": "android",
  "build": "debug-<version>",
  "commit": "<sha>",
  "timestamp": "2026-11-24T20:00:00Z",
  "stand": "ssh home (KVM, HDD)",
  "artifact_dir": ".scratch/app-verify-<ts>",
  "scenarios_checked": ["..."],
  "invariants": {"crash_count": 0, "anr_count": 0, "scenarios_passed": 12, "scenarios_failed": 0, "details": []},
  "findings": [
    {
      "id": "B1",
      "title": "...",
      "severity": "Blocker",
      "screen": "settings",
      "steps": ["..."],
      "expected": "...",
      "actual": "...",
      "screenshots": ["artifacts/settings-crash.png"],
      "logcat_excerpt": "...",
      "category": "stability",
      "labels": ["bug"]
    }
  ],
  "rubric": {
    "functional": {"score": 5, "notes": "..."},
    "ui": {"score": 4, "notes": "..."},
    "ux": {"score": 4, "notes": "..."},
    "stability": {"score": 5, "notes": "..."}
  }
}
```

Вердикт считается автоматически: `BLOCKED` при крашах/ANR/проваленных сценариях/находках `Blocker`,
`CONDITIONAL` при `Major` или любой оси рубрики `<= 3`, иначе `READY`.

Выгрузи открытые GitHub Issues, сгенерируй Markdown-отчёт с секцией кандидатов и краткую сводку:

```bash
gh issue list --repo <owner/repo> --state open --limit 200 \
  --json number,title,body,labels,url,state > .scratch/app-verify-<ts>/open-issues.json

python3 scripts/app-verify.py report --input .scratch/app-verify-<ts>/run.json \
  --open-issues .scratch/app-verify-<ts>/open-issues.json \
  --output .scratch/app-verify-<ts>/report.md
python3 scripts/app-verify.py report --input .scratch/app-verify-<ts>/run.json \
  --open-issues .scratch/app-verify-<ts>/open-issues.json --chat-summary

python3 scripts/app-verify.py --json issues dedup \
  --input .scratch/app-verify-<ts>/run.json \
  --open-issues .scratch/app-verify-<ts>/open-issues.json
```

Дедупликатор делит находки на точные дубли (пропускаются всегда), возможные дубли (пропускаются при
`--approve-all-new`, но могут быть явно одобрены владельцем через `--approve <id>`) и новые кандидаты.
Отправь владельцу в чат T3 сводку и ключевые скриншоты.

### 7. Owner-gate для GitHub Issues

**Никогда не заводи issue без явного «ок» владельца.** Покажи список новых кандидатов,
дождись подтверждения и только затем выполни:

```bash
python3 scripts/app-verify.py --json issues create \
  --input .scratch/app-verify-<ts>/run.json \
  --repo <owner/repo> \
  --open-issues .scratch/app-verify-<ts>/open-issues.json \
  --approve M1,U1 \
  --confirm-owner-approval
```

Без `--confirm-owner-approval` команда завершается ошибкой и не вызывает `gh issue create`.
`--dry-run` безопасно показывает план (в том числе с `--approve-all-new`). Точные дубли
не создаются никогда.

## Жёсткие правила

- Живой прогон — только по требованию; не встраивай его в pre-commit или обязательный CI.
- Все Android-артефакты — строго на HDD `/mnt/data/android/`; SSD не трогать.
- Стенд останавливается в cleanup всегда.
- Issue создаются только после явного подтверждения владельца.
- В отчёте и телах issue не должно быть упоминаний инструментов и авторства генерации.
