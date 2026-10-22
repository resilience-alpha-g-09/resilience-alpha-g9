#!/usr/bin/env python3
"""Incident contrôlé pour le laboratoire RESILIENCE-ALPHA : charge applicative sur notre propre plateforme.
Garde-fou : refuse toute cible hors du hotspot du groupe (192.168.137.0/24) ou de localhost."""
import argparse
import asyncio
import ipaddress
import random
import socket
import string
import sys
import time
from collections import Counter
from urllib.parse import urlparse

import httpx

ALLOWED_NETS = [ipaddress.ip_network("192.168.137.0/24"), ipaddress.ip_network("127.0.0.0/8")]


def check_target(url: str, extra_nets: list[str] | None = None):
    """Périmètre : hotspot Windows du groupe, localhost, et — explicitement, via --lab-net — le sous-réseau d'un hotspot
    de téléphone utilisé pour le labo (ex. 10.242.129.0/24). Jamais d'adresse publique."""
    for n in extra_nets or []:
        net = ipaddress.ip_network(n, strict=False)
        if not net.is_private:
            sys.exit(f"REFUSÉ : {net} n'est pas un réseau privé")
        ALLOWED_NETS.append(net)
    host = urlparse(url).hostname or ""
    try:
        ip = ipaddress.ip_address(socket.gethostbyname(host))
    except (socket.gaierror, ValueError):
        sys.exit(f"Cible illisible : {host}")
    if not any(ip in net for net in ALLOWED_NETS):
        sys.exit(f"REFUSÉ : {host} ({ip}) n'est pas dans le périmètre du labo {[str(n) for n in ALLOWED_NETS]}")


def rand(n=10):
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


async def worker(client, target, mode, stop, stats, lat):
    while time.monotonic() < stop["at"] and not stop["blocked"]:
        m = mode if mode != "mixed" else random.choice(["login", "api"])
        t0 = time.perf_counter()
        try:
            if m == "login":
                r = await client.post(f"{target}/auth/login", json={"email": f"{rand()}@demo.local", "password": rand(12)})
            else:
                r = await client.post(f"{target}/api/transactions", headers={"Authorization": f"Bearer {rand(40)}"},
                                      json={"to_account": 10, "amount": "1.00"})
            stats[r.status_code] += 1
            if r.status_code == 403:
                stop["blocked"] = True
            elif r.status_code == 429:
                stop["throttled"] = stop.get("throttled", 0) + 1
                if stop["throttled"] >= 30:          # étranglé par l'edge (source NAT partagée) : inutile d'insister
                    stop["blocked"] = True
        except httpx.HTTPError as exc:
            stats[exc.__class__.__name__] += 1
        lat.append(time.perf_counter() - t0)


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--target", required=True, help="ex. http://192.168.137.1")
    p.add_argument("--mode", choices=["login", "api", "mixed"], default="login")
    p.add_argument("--rps", type=int, default=60, help="concurrence ≈ connexions simultanées — 60 suffit (bcrypt) ; au-delà de ~150, le relais de ports de Docker Desktop sature avant la plateforme")
    p.add_argument("--duration", type=int, default=45)
    p.add_argument("--lab-net", action="append", default=[], metavar="CIDR",
                   help="sous-réseau privé supplémentaire autorisé (hotspot de téléphone), ex. 10.242.129.0/24")
    a = p.parse_args()
    target = a.target.rstrip("/")
    check_target(target, a.lab_net)

    print(f"[incident] cible={target} mode={a.mode} concurrence={a.rps} durée={a.duration}s — labo uniquement")
    stop = {"at": time.monotonic() + a.duration, "blocked": False}
    stats, lat = Counter(), []
    async with httpx.AsyncClient(timeout=10, limits=httpx.Limits(max_connections=a.rps)) as client:
        tasks = [asyncio.create_task(worker(client, target, a.mode, stop, stats, lat)) for _ in range(a.rps)]
        start = time.monotonic()
        last = Counter()
        while any(not t.done() for t in tasks):
            await asyncio.sleep(1)
            delta = stats - last
            last = stats.copy()
            avg = (sum(lat[-500:]) / len(lat[-500:]) * 1000) if lat else 0
            print(f"t+{time.monotonic() - start:4.0f}s  {sum(delta.values()):5d} req/s  {dict(delta)}  lat≈{avg:.0f} ms")
            if stop["blocked"]:
                print("[incident] " + ("rafale de 429 : notre source est étranglée à l'edge" if stop.get("throttled", 0) >= 30 else "403 reçu : notre IP est bloquée à l'edge") + " — remédiation en cours, arrêt.")
                stop["at"] = 0
        await asyncio.gather(*tasks, return_exceptions=True)
    print(f"[incident] terminé — total {dict(stats)}")


if __name__ == "__main__":
    asyncio.run(main())
