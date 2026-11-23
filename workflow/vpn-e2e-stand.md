# E2E-стенд проверки VPN-подписок (`ssh home`)

Стенд проверяет реальные пользовательские подписки Remnawave (`ClodClash/0.1.5` → **Mihomo** и `Happ/4.8.0` → **Xray `26.10.31-h2Flow`**) с домашней машины `home` через оба реальных аплинка:
- **Wi-Fi (`wlp0s20f3`)** — фиксированный широкополосный провайдер;
- **Модем (`enx0c5b8f279a64`)** — мобильный оператор (МТС, `AS8359`, MSS 1400, ТСПУ на трансграничных/не-РФ маршрутах).

## Как устроен скрипт

Скрипт: [scripts/vpn-e2e-stand.py](../scripts/vpn-e2e-stand.py)

1. Автоматически берёт `REMNAWAVE_API_TOKEN` из `.env` (локально или с `leaseweb-nl`).
2. Создаёт временного пользователя `e2e_stand_<HHMMSS>` в сквадах `users_squad` + `WL servers` и **гарантированно удаляет его в `finally`**.
3. Скачивает боевые подписки с `https://sub.fus1ond.ru/<shortUuid>`:
   - `User-Agent: ClodClash/0.1.5` (Mihomo YAML — все прокси-хосты по отдельности);
   - `User-Agent: Happ/4.8.0` (массив XRAY_JSON-конфигов — балансиры локаций с `injectHosts` + прямые хосты).
4. На `home` в `mktemp -d /tmp/vpn-e2e-stand-XXXXXX` поднимает:
   - `/home/fus1ond/rw-bench/clod/mihomo` (`v1.19.33-clod.4`) с отдельным SOCKS5-листенером и `interface-name` на каждую пару `(аплинк, прокси)`;
   - `/home/fus1ond/rw-bench/xray` (`26.10.31-h2Flow`) по отдельному процессу на каждый Happ-конфиг с `sockopt.interface` для каждого proxy-outbound.
5. Прогоняет `curl --socks5-hostname` до `https://api.ipify.org` (плюс опционально `youtube.com/generate_204` и скачивание `N` байт с `speed.cloudflare.com`), печатает сводную таблицу с выходными IP, RTT, флагами `pq=` / `fm=` и зачищает все процессы и `/tmp` по `trap EXIT`.

## Команды запуска

```bash
# Полный прогон всего флота (Mihomo + Xray × Wi-Fi + Модем)
python3 /root/harness-engineering/scripts/vpn-e2e-stand.py

# Быстрая проверка только модема и конкретного хоста (+ YouTube + 256 КБ)
python3 /root/harness-engineering/scripts/vpn-e2e-stand.py \
  --uplinks modem --filter "Москва" --check-youtube --check-bytes 262144

# Только один клиент (mihomo или xray), JSON-вывод для скриптов
python3 /root/harness-engineering/scripts/vpn-e2e-stand.py --clients xray --json
```

## Инвариант DPI для РФ-нод (выявлено 2026-11-23)

На мобильных операторах (в частности МТС → `AS33993 UFO Hosting`) ТСПУ глушит нефрагментированный 2-пакетный Post-Quantum ClientHello (`X25519MLKEM768`, ~1895 Б > MSS 1400) для не-whitelist SNI:
- **Для Mihomo (`ClodClash`):** `mihomoX25519: false` сжимает ClientHello до 1 TCP-пакета (~673 Б).
- **Для Xray `26.10.31` (`Happ`):** uTLS-фингерпринт `firefox` содержит PQ встроенно, поэтому для него обязателен `finalMask` (`tcp fragment: length 10-20, packets: tlshello, interval: 10-20`), который Mihomo игнорирует, а Xray использует для обхода DPI.

После любых правок хостов, профилей или шаблонов Remnawave — прогонять `vpn-e2e-stand.py` перед закрытием задачи.
