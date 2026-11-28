# Deploying TrailForge Privately with Tailscale Serve

[Tailscale Serve](https://tailscale.com/kb/1312/serve) is the fastest zero-port-forwarding way to expose your self-hosted **TrailForge** instance over valid, trusted HTTPS to your phones, laptops, and in-cab tablets across your private Tailnet.

## Why HTTPS Is Required for In-Cab Phones & Tablets

Even on a private home network or 4WD touring rig, plain `http://192.168.x.x:5173` breaks critical mobile browser APIs that require a [Secure Context (`https://`)](https://developer.mozilla.org/en-US/docs/Web/Security/Secure_Contexts):

- **PWA Service Worker & Offline Pack Caching:** iOS Safari and Android Chrome refuse to register `/sw.js`, install TrailForge to the home screen, or cache offline VIC/NSW regional packs over non-HTTPS LAN addresses.
- **Geolocation API (`navigator.geolocation`):** Live GPS positioning and the in-cab **Breadcrumb Recorder** are blocked on insecure origins.
- **Screen Wake Lock API (`navigator.wakeLock`):** Keeping your phone or tablet screen awake while driving a track requires HTTPS.
- **Secure Session Cookies (`AUTH_COOKIE_SECURE=true`):** Browsers only transmit `Secure` `HttpOnly` auth cookies (`tf_session`) over encrypted TLS connections.

Tailscale Serve gives you real Let's Encrypt TLS certificates for `https://<your-host>.<your-tailnet>.ts.net` with zero open router ports and zero public internet exposure.

---

## 1. Configure `.env` for Tailnet HTTPS

Generate strong secrets and configure your Tailnet origin:

```bash
cp .env.example .env
```

Set the following values in `.env` (generate secrets with `openssl rand -hex 32`):

```dotenv
POSTGRES_PASSWORD=<strong-db-password>
DATABASE_URL=postgresql+psycopg://trailforge:<strong-db-password>@db:5432/trailforge

AUTH_SECRET_KEY=<64-hex-chars-from-openssl-rand-hex-32>
TRAILFORGE_PRIVACY_SECRET=<64-hex-chars-from-openssl-rand-hex-32>
AUTH_COOKIE_SECURE=true

CORS_ORIGINS=https://<your-host>.<your-tailnet>.ts.net
TRAILFORGE_ENABLE_LIVE_FEEDS=true
```

---

## 2. Start TrailForge Bound to Loopback

`docker-compose.ghcr.yml` binds strictly to `127.0.0.1:5173` by default so the unencrypted HTTP port is never exposed to the LAN:

```bash
docker compose -f docker-compose.ghcr.yml up -d
```

Verify the web proxy, API, and PostGIS database are healthy:

```bash
curl -fsS http://127.0.0.1:5173/api/health
```

Expected response:

```json
{"status":"ok","db_ok":true,"postgis_version":"3.4 ...","schema_revision":"..."}
```

---

## 3. Publish Over Tailnet HTTPS

Enable **MagicDNS** and **HTTPS Certificates** in the Tailscale admin console.

On a Linux Docker host, use a private Unix socket so each Tailnet client gets its own
tile and pack download allowance. This path requires Tailscale 1.104.4 or newer.
Use the web container's mapped UID as directory owner if Docker uses rootless mode
or user namespace remapping. [Tailscale Unix backend security guidance](https://tailscale.com/security-bulletins#ts-2026-016)

```bash
sudo install -d -m 0700 -o 101 -g 101 /var/lib/trailforge-ingress
docker compose -f docker-compose.ghcr.yml -f deploy/download-socket-compose.override.yml up -d
sudo tailscale serve --bg unix:/var/lib/trailforge-ingress/web.sock
```

If the host proxy and the web container use different kernels, put both in the same
Linux VM with a protected shared socket to retain per-client limits. The loopback
command below remains available on other platforms. Its download allowance is shared
among requests from the same proxy peer.

```bash
tailscale serve --bg http://127.0.0.1:5173
```

Confirm the active HTTPS route:

```bash
tailscale serve status
```

Open `https://<your-host>.<your-tailnet>.ts.net` on your phone or in-cab tablet.

Optional — confirm HTTP `Range` (`206 Partial Content`) streaming works through Tailscale Serve for PMTiles once tiles are built:

```bash
curl -sSI -H "Range: bytes=0-127" \
  "https://<your-host>.<your-tailnet>.ts.net/api/tiles/tracks.pmtiles" | head -n 5
```

---

## 4. Prepare Your Phone / Tablet Before Leaving Reception

1. Open `https://<your-host>.<your-tailnet>.ts.net` while still on Wi-Fi or mobile reception.
2. Sign in (or register your self-hosted account) and set up your active 4WD vehicle profile.
3. Add TrailForge to your home screen (**Install App** / **Add to Home Screen**) so it runs full-screen as a PWA.
4. Open the **Offline Packs** panel and download your target region (e.g. `australian-alps` or `blue-mountains`).
5. Once out of coverage, TrailForge serves cached map tiles, track intelligence, elevation profiles, closures, and local GPS breadcrumb recording directly from the device, syncing recorded GPX passes back when reception returns.

---

## 5. Security Note on Tailscale Funnel

> **Caution:** Prefer `tailscale serve` (private Tailnet only) over `tailscale funnel` (public internet). If you intentionally expose TrailForge publicly via Funnel or a public reverse proxy, make sure `AUTH_SECRET_KEY`, `TRAILFORGE_PRIVACY_SECRET`, and `POSTGRES_PASSWORD` are strong random values, `AUTH_COOKIE_SECURE=true` is set, and consider adding rate limiting or an access gateway in front of administrative pack-build endpoints.

---

## 6. Teardown

```bash
tailscale serve --https=443 off
docker compose -f docker-compose.ghcr.yml down
```
