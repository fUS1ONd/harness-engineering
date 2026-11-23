#!/usr/bin/env python3
"""E2E-стенд проверки VPN-подписок Remnawave (Mihomo + Xray) через домашние аплинки `ssh home`.

Создаёт временного пользователя в панели Remnawave (удаляется в `finally`),
выгружает реальные подписки ClodClash (Mihomo YAML) и Happ (XRAY_JSON),
поднимает на `home` временные инстансы `mihomo` и `xray` и проверяет каждый
хост/конфиг через заданные сетевые интерфейсы (Wi-Fi и/или LTE-модем).

Примеры:
  # Полный прогон: Mihomo + Xray по Wi-Fi и модему (запуск с leaseweb-nl или hetzner-fn):
  python3 /root/harness-engineering/scripts/vpn-e2e-stand.py

  # Только один хост (фильтр по подстроке) и только модем, плюс YouTube + 256 КБ:
  python3 /root/harness-engineering/scripts/vpn-e2e-stand.py --filter "Москва" --uplinks modem --check-youtube --check-bytes 262144

  # Только Mihomo (без Xray):
  python3 /root/harness-engineering/scripts/vpn-e2e-stand.py --clients mihomo
"""

import argparse
import base64
import copy
import datetime as dt
import json
import os
import re
import subprocess
import sys
import urllib.request

import yaml

DEFAULT_PANEL_URL = "https://panel.sarvizza.com"
DEFAULT_SUB_URL = "https://sub.fus1ond.ru"
DEFAULT_SQUADS = [
    "413fd3d2-fa16-4d14-8538-52746e3863b4",  # users_squad
    "7cd2e959-54bb-4935-9462-f28ddd3adce7",  # WL servers
]
DEFAULT_UPLINKS = {
    "wifi": "wlp0s20f3",
    "modem": "enx0c5b8f279a64",
}
DEFAULT_MIHOMO_BIN = "/home/fus1ond/rw-bench/clod/mihomo"
DEFAULT_XRAY_BIN = "/home/fus1ond/rw-bench/xray"
TOKEN_FILES = [
    "/root/nodes-infrastructure/.env",
    "/opt/remnawave/.env",
    "/root/.env",
]
TOKEN_RE = re.compile(r"^(?:REMNAWAVE_API_TOKEN|API_TOKEN|REMNAWAVE_TOKEN)=(.+)$", re.M)


def load_token(panel_ssh):
    env_tok = os.environ.get("REMNAWAVE_API_TOKEN") or os.environ.get("REMNAWAVE_TOKEN")
    if env_tok:
        return env_tok.strip().strip("\"'")
    for p in TOKEN_FILES:
        if os.path.exists(p):
            m = TOKEN_RE.search(open(p).read())
            if m:
                return m.group(1).strip().strip("\"'")
    if panel_ssh:
        cmd = [
            "ssh", "-o", "ConnectTimeout=5", panel_ssh,
            "grep -hE '^(REMNAWAVE_API_TOKEN|API_TOKEN|REMNAWAVE_TOKEN)=' "
            + " ".join(TOKEN_FILES) + " 2>/dev/null | head -n 1",
        ]
        out = subprocess.check_output(cmd, text=True).strip()
        if "=" in out:
            return out.split("=", 1)[1].strip().strip("\"'")
    raise SystemExit("Не найден REMNAWAVE_API_TOKEN (проверьте .env или переменную окружения)")


def detect_ssh_prefix(home_ssh, via_ssh):
    """Возвращает аргументы SSH для запуска команд на `home`."""
    if via_ssh:
        return ["ssh", "-o", "ConnectTimeout=5", via_ssh, "ssh", "-o", "ConnectTimeout=5", home_ssh]
    # Пробуем прямой доступ к `home` (работает на leaseweb-nl); если недоступен — идём через `leaseweb-nl`.
    direct = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3", home_ssh, "true"],
        capture_output=True, text=True, check=False,
    )
    if direct.returncode == 0:
        return ["ssh", "-o", "ConnectTimeout=5", home_ssh]
    return ["ssh", "-o", "ConnectTimeout=5", "leaseweb-nl", "ssh", "-o", "ConnectTimeout=5", home_ssh]


class PanelClient:
    def __init__(self, base_url, token):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Bearer {self.token}", "User-Agent": "vpn-e2e-stand/1.0"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        r = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        with urllib.request.urlopen(r, timeout=20) as resp:
            txt = resp.read().decode()
            return resp.status, (json.loads(txt) if txt.strip() else None)


def fetch_sub(sub_url, short_uuid, user_agent, hwid):
    url = f"{sub_url.rstrip('/')}/{short_uuid}"
    req = urllib.request.Request(url, headers={"User-Agent": user_agent, "x-hwid": hwid})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode()


def build_mihomo_plan(clod_yaml, uplinks, name_filter):
    cfg = yaml.safe_load(clod_yaml)
    proxies = cfg.get("proxies") or []
    selected = [p for p in proxies if not name_filter or name_filter.lower() in p.get("name", "").lower()]
    listeners = []
    out_proxies = []
    targets = []
    port = 24100
    for uname, iface in uplinks:
        for idx, p in enumerate(selected):
            tag = f"m-{uname}-{idx}"
            p_copy = copy.deepcopy(p)
            p_copy["name"] = tag
            p_copy["interface-name"] = iface
            out_proxies.append(p_copy)
            listeners.append({
                "name": f"in-{tag}",
                "type": "socks",
                "port": port,
                "listen": "127.0.0.1",
                "proxy": tag,
            })
            targets.append({
                "client": "mihomo",
                "uplink": uname,
                "name": p.get("name", tag),
                "server": p.get("server", ""),
                "port": port,
                "pq": bool((p.get("reality-opts") or {}).get("support-x25519mlkem768")),
            })
            port += 1
    mihomo_cfg = {
        "mixed-port": 0,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "warning",
        "ipv6": False,
        "listeners": listeners,
        "proxies": out_proxies,
        "rules": ["MATCH,DIRECT"],
    }
    return yaml.safe_dump(mihomo_cfg, allow_unicode=True, sort_keys=False), targets


def build_xray_plans(happ_json_str, uplinks, name_filter):
    items = json.loads(happ_json_str)
    selected = [it for it in items if not name_filter or name_filter.lower() in it.get("remarks", "").lower()]
    configs = []
    targets = []
    port = 24500
    for uname, iface in uplinks:
        for idx, it in enumerate(selected):
            cfg = copy.deepcopy(it)
            remark = cfg.pop("remarks", f"xray-{idx}")
            cfg["log"] = {"loglevel": "warning"}
            cfg["inbounds"] = [{
                "tag": "socks-in",
                "port": port,
                "listen": "127.0.0.1",
                "protocol": "socks",
                "settings": {"udp": True, "auth": "noauth"},
            }]
            fm_count = 0
            proxy_outbounds = 0
            for ob in cfg.get("outbounds") or []:
                proto = ob.get("protocol")
                if proto in ("vless", "vmess", "trojan", "shadowsocks", "hysteria"):
                    proxy_outbounds += 1
                    ss = ob.setdefault("streamSettings", {})
                    sockopt = ss.setdefault("sockopt", {})
                    sockopt["interface"] = iface
                    if ss.get("finalmask"):
                        fm_count += 1
            # Убираем geosite/domain direct-правила, чтобы api.ipify.org и youtube точно шли в прокси/балансир
            routing = cfg.get("routing") or {}
            balancers = routing.get("balancers") or []
            if balancers:
                routing["rules"] = [{
                    "type": "field",
                    "inboundTag": ["socks-in"],
                    "balancerTag": balancers[0]["tag"],
                }]
            else:
                first_proxy = next(
                    (o["tag"] for o in cfg.get("outbounds") or [] if o.get("protocol") not in ("freedom", "blackhole")),
                    "proxy",
                )
                routing["rules"] = [{
                    "type": "field",
                    "inboundTag": ["socks-in"],
                    "outboundTag": first_proxy,
                }]
            cfg["routing"] = routing
            # Привязываем observatory (если есть) к быстрому интервалу или отключаем ожидание первого замера
            fname = f"x-{uname}-{idx}.json"
            configs.append((fname, json.dumps(cfg, ensure_ascii=False)))
            targets.append({
                "client": "xray",
                "uplink": uname,
                "name": remark,
                "server": f"{proxy_outbounds} outbounds",
                "port": port,
                "fm": f"{fm_count}/{proxy_outbounds}",
            })
            port += 1
    return configs, targets


def run_remote_stand(ssh_prefix, mihomo_yaml, xray_configs, targets, args):
    runner_py = r"""
import concurrent.futures, json, os, subprocess, sys, time

workdir = sys.argv[1]
meta = json.load(open(os.path.join(workdir, "meta.json")))
targets = meta["targets"]
timeout_s = meta["timeout"]
check_yt = meta["check_youtube"]
check_bytes = meta["check_bytes"]

def probe(t):
    port = t["port"]
    res = dict(t)
    cmd = [
        "curl", "-4", "-sS", "--max-time", str(timeout_s),
        "--socks5-hostname", f"127.0.0.1:{port}",
        "-w", "\n%{http_code}\n%{time_total}",
        "https://api.ipify.org",
    ]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode == 0:
        lines = p.stdout.strip().splitlines()
        res["ip"] = lines[0].strip() if len(lines) >= 3 else ""
        res["code"] = lines[-2] if len(lines) >= 2 else ""
        res["total"] = float(lines[-1]) if len(lines) >= 1 else 0.0
        res["ok"] = (res["code"] == "200" and bool(res["ip"]))
    else:
        res["ok"] = False
        res["err"] = (p.stderr or p.stdout).strip().splitlines()[-1] if (p.stderr or p.stdout).strip() else f"rc={p.returncode}"

    if res["ok"] and check_yt:
        py = subprocess.run([
            "curl", "-4", "-sS", "--max-time", str(timeout_s),
            "--socks5-hostname", f"127.0.0.1:{port}",
            "-o", "/dev/null", "-w", "%{http_code}:%{time_total}",
            "https://www.youtube.com/generate_204",
        ], capture_output=True, text=True)
        res["yt"] = py.stdout.strip() if py.returncode == 0 else f"ERR({py.returncode})"

    if res["ok"] and check_bytes > 0:
        pd = subprocess.run([
            "curl", "-4", "-sS", "--max-time", str(max(timeout_s, 12)),
            "--socks5-hostname", f"127.0.0.1:{port}",
            "-o", "/dev/null", "-w", "%{http_code}:%{size_download}:%{time_total}",
            f"https://speed.cloudflare.com/__down?bytes={check_bytes}",
        ], capture_output=True, text=True)
        res["dl"] = pd.stdout.strip() if pd.returncode == 0 else f"ERR({pd.returncode})"
    return res

with concurrent.futures.ThreadPoolExecutor(max_workers=meta["parallel"]) as ex:
    results = list(ex.map(probe, targets))
print(json.dumps(results, ensure_ascii=False))
"""
    payload = {
        "mihomo_yaml": mihomo_yaml,
        "xray_configs": xray_configs,
        "meta": {
            "targets": targets,
            "timeout": args.timeout,
            "check_youtube": args.check_youtube,
            "check_bytes": args.check_bytes,
            "parallel": args.parallel,
        },
        "runner_py": runner_py,
    }
    b64_payload = base64.b64encode(json.dumps(payload).encode()).decode()

    remote_bash = f"""
set -e
TMPDIR=$(mktemp -d /tmp/vpn-e2e-stand-XXXXXX)
PIDS=()
cleanup() {{
  for pid in "${{PIDS[@]}}"; do
    kill "$pid" 2>/dev/null || true
  done
  pkill -f "$TMPDIR/" 2>/dev/null || true
  rm -rf "$TMPDIR"
}}
trap cleanup EXIT

cat << 'PYLOAD_B64' > "$TMPDIR/payload.b64"
{b64_payload}
PYLOAD_B64

python3 - "$TMPDIR" << 'PYUNPACK'
import base64, json, os, sys
w = sys.argv[1]
with open(os.path.join(w, "payload.b64"), "r", encoding="utf-8") as fh:
    d = json.loads(base64.b64decode(fh.read()))
if d["mihomo_yaml"]:
    open(os.path.join(w, "mihomo.yaml"), "w", encoding="utf-8").write(d["mihomo_yaml"])
for fn, content in d["xray_configs"]:
    open(os.path.join(w, fn), "w", encoding="utf-8").write(content)
open(os.path.join(w, "meta.json"), "w", encoding="utf-8").write(json.dumps(d["meta"]))
open(os.path.join(w, "runner.py"), "w", encoding="utf-8").write(d["runner_py"])
PYUNPACK

if [ -f "$TMPDIR/mihomo.yaml" ]; then
  "{args.mihomo_bin}" -f "$TMPDIR/mihomo.yaml" > "$TMPDIR/mihomo.log" 2>&1 &
  PIDS+=($!)
fi
for xf in "$TMPDIR"/x-*.json; do
  [ -e "$xf" ] || continue
  "{args.xray_bin}" run -c "$xf" > "$xf.log" 2>&1 &
  PIDS+=($!)
done

sleep {args.warmup}
python3 "$TMPDIR/runner.py" "$TMPDIR"
"""
    cmd = ssh_prefix + ["bash -s"]
    res = subprocess.run(cmd, input=remote_bash, text=True, capture_output=True, timeout=180)
    if res.returncode != 0:
        sys.stderr.write(f"Remote stand failed (rc={res.returncode}):\n{res.stderr}\n{res.stdout}\n")
        sys.exit(res.returncode)
    return json.loads(res.stdout.strip().splitlines()[-1])


def main():
    ap = argparse.ArgumentParser(description="E2E-стенд проверки VPN-подписок Remnawave на ssh home")
    ap.add_argument("--panel-url", default=DEFAULT_PANEL_URL)
    ap.add_argument("--sub-url", default=DEFAULT_SUB_URL)
    ap.add_argument("--panel-ssh", default="leaseweb-nl", help="SSH-хост, где искать .env с токеном (если нет локально)")
    ap.add_argument("--home-ssh", default="home", help="SSH-алиас домашнего стенда")
    ap.add_argument("--via-ssh", default=None, help="Промежуточный SSH-хост до home (по умолчанию авто)")
    ap.add_argument("--clients", default="mihomo,xray", help="Какие клиенты тестировать: mihomo,xray")
    ap.add_argument("--uplinks", default="wifi,modem", help="Какие аплинки: wifi,modem или имя=интерфейс")
    ap.add_argument("--filter", default="", help="Фильтр по подстроке имени хоста/конфига")
    ap.add_argument("--timeout", type=int, default=10, help="Таймаут curl на один прокси (сек)")
    ap.add_argument("--warmup", type=int, default=3, help="Пауза прогрева клиентов (сек)")
    ap.add_argument("--parallel", type=int, default=6, help="Параллельность curl-запросов")
    ap.add_argument("--check-youtube", action="store_true", help="Дополнительно проверять youtube.com/generate_204")
    ap.add_argument("--check-bytes", type=int, default=0, help="Дополнительно скачать N байт с speed.cloudflare.com")
    ap.add_argument("--mihomo-bin", default=DEFAULT_MIHOMO_BIN)
    ap.add_argument("--xray-bin", default=DEFAULT_XRAY_BIN)
    ap.add_argument("--json", action="store_true", help="Вывести результаты в JSON")
    args = ap.parse_args()

    clients = {c.strip().lower() for c in args.clients.split(",") if c.strip()}
    uplinks = []
    for item in args.uplinks.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" in item:
            k, v = item.split("=", 1)
            uplinks.append((k.strip(), v.strip()))
        elif item in DEFAULT_UPLINKS:
            uplinks.append((item, DEFAULT_UPLINKS[item]))
        else:
            uplinks.append((item, item))

    token = load_token(args.panel_ssh)
    panel = PanelClient(args.panel_url, token)
    ssh_prefix = detect_ssh_prefix(args.home_ssh, args.via_ssh)

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%H%M%S")
    exp = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _, created = panel.req("POST", "/api/users", {
        "username": f"e2e_stand_{stamp}",
        "status": "ACTIVE",
        "expireAt": exp,
        "activeInternalSquads": DEFAULT_SQUADS,
        "hwidDeviceLimit": 1,
    })
    u = created["response"]
    del_id = u.get("uuid") or u.get("id")
    short_uuid = u["shortUuid"]
    hwid = f"e2e-stand-{stamp}"

    try:
        mihomo_yaml = None
        xray_configs = []
        targets = []

        if "mihomo" in clients:
            clod_body = fetch_sub(args.sub_url, short_uuid, "ClodClash/0.1.5", hwid)
            mihomo_yaml, m_targets = build_mihomo_plan(clod_body, uplinks, args.filter)
            targets.extend(m_targets)

        if "xray" in clients:
            happ_body = fetch_sub(args.sub_url, short_uuid, "Happ/4.8.0", hwid)
            xray_configs, x_targets = build_xray_plans(happ_body, uplinks, args.filter)
            targets.extend(x_targets)

        if not targets:
            raise SystemExit("Нет хостов, подходящих под фильтр")

        results = run_remote_stand(ssh_prefix, mihomo_yaml, xray_configs, targets, args)
    finally:
        panel.req("DELETE", f"/api/users/{del_id}")

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print(f"{'CLIENT':<7} | {'UPLINK':<6} | {'OK':<4} | {'EXIT_IP':<16} | {'TIME':>6} | {'EXTRA':<12} | NAME")
        print("-" * 100)
        for r in results:
            ok_str = "OK" if r["ok"] else "FAIL"
            ip_str = r.get("ip", "-") if r["ok"] else r.get("err", "error")[:16]
            tm_str = f"{r['total']:.2f}s" if r["ok"] else "-"
            extra = []
            if "pq" in r:
                extra.append(f"pq={int(r['pq'])}")
            if "fm" in r:
                extra.append(f"fm={r['fm']}")
            if "yt" in r:
                extra.append(f"yt={r['yt'].split(':')[0]}")
            if "dl" in r:
                extra.append(f"dl={r['dl'].split(':')[0]}")
            print(f"{r['client']:<7} | {r['uplink']:<6} | {ok_str:<4} | {ip_str:<16} | {tm_str:>6} | {' '.join(extra):<12} | {r['name']}")
            if not r["ok"] and r.get("err"):
                print(f"        err: {r['err']}")

        print("-" * 100)
        for cl in sorted({r["client"] for r in results}):
            for up in [u_name for u_name, _ in uplinks]:
                sub = [r for r in results if r["client"] == cl and r["uplink"] == up]
                if sub:
                    passed = sum(1 for r in sub if r["ok"])
                    print(f"SUMMARY {cl:<6} / {up:<5}: {passed}/{len(sub)} OK")

    sys.exit(0 if all(r["ok"] for r in results) else 1)


if __name__ == "__main__":
    main()
