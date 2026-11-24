# Агентская верификация приложений (UI/UX и функционал)

Регламент запуска ИИ-агента-тестировщика перед выдачей debug-сборки владельцу или перед prod-релизом. Архитектурное решение зафиксировано в [ADR-0001](../docs/adr/0001-agent-app-verification-harness.md).

## Архитектура стенда

1. **Быстрый слой (VPS, без эмулятора):** Compose UI / screenshot-тесты на JVM.
2. **Живой стенд (`ssh home`, KVM):** headless Android Emulator на `home-server` (`/dev/kvm`), развёрнутый строго на HDD `/mnt/data/android/` (SSD `/dev/sda2` не трогаем). Доступен с VPS по SSH-туннелю `adb`.
3. **Опционально:** T3 Device Hub / реальный телефон (только для проверок реальной сети/VPN/SIM).

## Ручки агента

- **Тонкий CLI стенда (`scripts/app-verify.py`):** платформо-независимый верхний уровень (`--platform android`, расширяемый под веб/боты) + адаптер Android KVM на `ssh home` (жёсткий инвариант HDD `/mnt/data/android/`):
  - `python3 scripts/app-verify.py --json prepare` — проверить HDD-инвариант, SDK, AVD и снапшот `clean-base`.
  - `python3 scripts/app-verify.py --json start` (`up`) — поднять или откатить эмулятор к `clean-base`, открыть SSH-туннель ADB (`127.0.0.1:15565`) и дождаться состояния `device`.
  - `python3 scripts/app-verify.py --json status` — статус эмулятора, туннеля и локального `adb`.
  - `python3 scripts/app-verify.py --json install <path/to/app-debug.apk>` — установить debug APK.
  - `python3 scripts/app-verify.py --json launch <package>` — запустить приложение (`am start -W`).
  - `python3 scripts/app-verify.py --json screenshot <out.png>` — снять и провалидировать PNG-скриншот.
  - `python3 scripts/app-verify.py --json logcat [--package <pkg>] [--output <log.txt>] [--fail-on-error]` — выгрузить `logcat` и проверить инвариант `0 crash / 0 ANR`.
  - `python3 scripts/app-verify.py --json stop` (`down`) — остановить эмулятор и закрыть SSH-туннель.
- **UI-драйвер (`@mobilenext/mobile-mcp` или `Maestro`):** чтение accessibility-дерева, тапы, ввод, свайпы, снятие скриншотов.

## Порядок прогона (по требованию)

1. Подготовить чистый снапшот эмулятора и установить актуальную `debug`-сборку.
2. Запустить свободное исследование агентом по ключевым сценариям и краевым состояниям (с использованием white-box ручек для моков сети/состояний и сброса БД).
3. Собрать структурированный отчёт:
   - Инварианты (0 крашей, 0 ANR, базовые сценарии).
   - Найденные проблемы с severity (`Blocker` / `Major` / `Minor` / `UX polish`), шагами воспроизведения, скриншотами и фрагментами `logcat`.
   - Оценка по рубрике: **Функционал / UI / UX / Стабильность**.
4. Предложить владельцу список кандидатов в GitHub Issues (после дедупликации по открытым issue). **Заводить issue только после явного «ок» владельца.**
