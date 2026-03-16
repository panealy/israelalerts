# israelalerts

Persistent Israeli emergency alert listener based on the [Tzofar](https://www.tzevaadom.co.il)
WebSocket feed. Collects all alert messages in real time, stores them to SQLite, and exposes
a public HTTPS API from outside Israel.

## Live endpoint

```
https://YOUR_API_GATEWAY_URL
```

See `docs/openapi.yaml` for the full API spec, or paste it at [editor.swagger.io](https://editor.swagger.io)
for an interactive browser UI.

## Architecture

```
Tzofar WebSocket (wss://ws.tzevaadom.co.il)
        ↓
EC2 t4g.nano — il-central-1 (Amazon Linux 2023, ARM64)
  ec2/listener.py — stores to SQLite, HTTP API on :8080 (private)
        ↑  VPC private IP
Lambda (il-central-1, same VPC)
  lambda/handler.py — strips API Gateway prefix, proxies to EC2
        ↑
API Gateway HTTP API (il-central-1)
        ↑
Public HTTPS endpoint
```

**Note:** AWS Lambda Function URLs are not available in `il-central-1`. API Gateway
HTTP API is used instead as the public entry point.

## API endpoints

Base URL: `https://YOUR_API_GATEWAY_URL`

| Endpoint | Description |
|---|---|
| `GET /status` | WebSocket connection health |
| `GET /alerts` | Paginated alert history (default 100, max 1000) |
| `GET /alerts?threat=0` | Filter by threat type |
| `GET /alerts?since=2026-01-15T00:00:00Z` | Filter by received time |
| `GET /alerts?limit=50&offset=50` | Pagination |
| `GET /alerts/latest` | Most recent alert |

### Alert schema

All messages are stored with `received_at` added. The `type` field determines the structure.

#### `ALERT` — active siren

Alert details are nested inside the `data` object:

```json
{
  "type": "ALERT",
  "data": {
    "notificationId": "2466caa5-2f52-4c14-baf1-4b37002ae324",
    "time": 1773423099,
    "threat": 0,
    "isDrill": false,
    "cities": ["תל אביב - דרום העיר ויפו", "בת ים"],
    "citiesIds": [1234, 5678],
    "areasIds": [6]
  },
  "received_at": "2026-03-15T14:31:39.123Z"
}
```

#### `SYSTEM_MESSAGE` — two subtypes, distinguished by `data.bodyHe`

**Early warning** (`data.bodyHe` contains `"בדקות הקרובות ייתכן ויופעלו התרעות"`):
Fired when missile launches are detected before sirens activate. This is the closest
Tzofar gets to a pre-alert — there is no separate pre-alert message type.

**Exit / all-clear** (`data.bodyHe` contains `"האירוע הסתיים"`):
Fired when the incident ends.

```json
{
  "type": "SYSTEM_MESSAGE",
  "data": {
    "id": 821,
    "time": "1773595110",
    "titleEn": "Home Front Command - Incident Ended",
    "bodyEn": "The incident has ended at Ramot Naftali",
    "titleHe": "...",
    "bodyHe": "האירוע הסתיים ברמות נפתלי",
    "titleAr": "...", "bodyAr": "...",
    "titleRu": "...", "bodyRu": "...",
    "titleEs": "...", "bodyEs": "...",
    "citiesIds": [1605],
    "areasIds": [6]
  },
  "received_at": "2026-03-15T17:18:30.888Z"
}
```

### Threat types

Only types 0, 2, 5, and 8 have been observed in practice. Types 1, 3, 4, 6, and 7
are theoretically valid but have not appeared in Tzofar's historical archive.

| `threat` | Type |
|---|---|
| 0 | Rockets / missiles (Red Alert) |
| 1 | Unconventional missile |
| 2 | Earthquake |
| 3 | Tsunami |
| 4 | Hostile aircraft / UAV infiltration |
| 5 | Terrorist infiltration |
| 6 | Hazardous materials incident |
| 7 | Radiological incident |
| 8 | Safe to leave shelter (all-clear) |

## Deployment

See `docs/HOWTO-PowerShell.pdf` for the full step-by-step guide (Windows PowerShell).

### Infrastructure summary

| Resource | Value |
|---|---|
| EC2 instance | `YOUR_INSTANCE_ID` (tzofar-listener) |
| EC2 type | `t4g.nano`, Amazon Linux 2023 ARM64 |
| EC2 private IP | `YOUR_EC2_PRIVATE_IP` |
| EC2 public IP | `YOUR_EC2_PUBLIC_IP` |
| Region | `il-central-1` (Tel Aviv) |
| VPC | `YOUR_VPC_ID` |
| EC2 security group | `tzofar-ec2-sg` |
| Lambda security group | `tzofar-lambda-sg` |
| API Gateway | `tzofar-proxy-API` (igzzaecey6) |

### EC2 setup (Amazon Linux 2023)

```bash
# Install deps
sudo mkdir -p /opt/tzofar && sudo chown ec2-user:ec2-user /opt/tzofar
python3 -m venv /opt/tzofar/venv
/opt/tzofar/venv/bin/pip install websockets

# Download files from repo
curl -so /opt/tzofar/listener.py \
  https://raw.githubusercontent.com/panealy/israelalerts/main/ec2/listener.py
curl -so /tmp/tzofar.service \
  https://raw.githubusercontent.com/panealy/israelalerts/main/ec2/tzofar.service
sudo cp /tmp/tzofar.service /etc/systemd/system/tzofar.service

# Generate and set API token
TOKEN=$(python3 -c "import secrets; print(secrets.token_hex(32))")
sudo sed -i "s/CHANGE_ME/$TOKEN/" /etc/systemd/system/tzofar.service
sudo sed -i 's/User=nobody/User=ec2-user/' /etc/systemd/system/tzofar.service

# Enable and start
sudo systemctl daemon-reload && sudo systemctl enable --now tzofar
sudo journalctl -u tzofar -f
```

### Lambda setup

1. Create function in `il-central-1`, Python 3.12, arm64
2. Upload `lambda/handler.py` as a zip
3. Set environment variables:
   - `VM_BASE_URL` = `http://YOUR_EC2_PRIVATE_IP:8080`
   - `API_TOKEN` = same token set in `tzofar.service`
4. Attach to VPC, select all subnets, security group: `tzofar-lambda-sg`
5. Set timeout to 15 seconds
6. Add trigger: **API Gateway → HTTP API → Security: Open**

### API Gateway route fix

The API Gateway trigger creates route `ANY /tzofar-proxy`. You must also add:

```
ANY /tzofar-proxy/{proxy+}
```

Attach the same Lambda integration to the new route. The stage has auto-deploy
enabled so changes are live immediately.

## Cost

~$18/year (EC2 t4g.nano). API Gateway, Lambda, and intra-VPC data transfer
are effectively free at this data volume (~110 MB/year).

## Files

| File | Purpose |
|---|---|
| `ec2/listener.py` | WebSocket listener + HTTP API service |
| `ec2/tzofar.service` | systemd unit for the listener |
| `lambda/handler.py` | Lambda proxy function |
| `docs/openapi.yaml` | OpenAPI 3.0 spec for the API |
| `docs/HOWTO.pdf` | Deployment guide (bash) |
| `docs/HOWTO-PowerShell.pdf` | Deployment guide (Windows PowerShell) |
