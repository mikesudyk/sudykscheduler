# Sudyk Spectator Scheduler

Password-protected family sports board for **events.5udyk.com**.
Railway + Cloudflare.

## Go live on Railway + Cloudflare

1. Push this folder to a GitHub repo.
2. Railway → New project → Deploy from GitHub → this repo.
3. Service settings:
   - Builder will pick the Dockerfile.
   - Add a **volume**, mount path `/data`.
4. Variables:

```
SITE_PASSWORD=sudykcrew
SESSION_SECRET=<long random>
CALENDAR_TOKEN=<long random>
FAMILY_NAME=Sudyk
TIMEZONE=America/Detroit
DATA_DIR=/data
XAI_API_KEY=<optional, for reading screenshots>
```

5. Generate the default `*.up.railway.app` domain. Open `/health` — should return `{"ok": true}`.
6. Railway → Custom domain → `events.5udyk.com`. Copy the CNAME and the TXT verify record.
7. Cloudflare DNS for `5udyk.com`:

| Type | Name | Content | Proxy |
| --- | --- | --- | --- |
| CNAME | events | `….up.railway.app` from Railway | Proxied |
| TXT | whatever Railway shows | Railway verify value | DNS only |

8. Cloudflare SSL/TLS → **Full** (not Flexible, not Full Strict).
9. Open `https://events.5udyk.com`. You should see **SUDYK Spectator Scheduler** and the password gate.
10. First boot loads the 15-grandkid roster from `grandkids.csv` if the database is empty.

## Flow

Gate → upcoming board → Upload (parent → child → context → file) → questions → edit/confirm → events on the board with Google / Apple add buttons.
