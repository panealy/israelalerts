# israelalerts

Persistent Israeli alert listener based on the [Tzofar](https://www.tzevaadom.co.il) WebSocket feed.

## Architecture

```
Tzofar WebSocket (wss://ws.tzevaadom.co.il)
        ↓
EC2 t4g.nano — il-central-1 (private subnet)
  ec2/listener.py — stores to SQLite, exposes HTTP API on :8080
        ↑
Lambda — il-central-1 (same VPC)
  lambda/handler.py — public HTTPS proxy via Function URL
        ↑
Your external caller
```

## Components

### `ec2/listener.py`

Connects to Tzofar's WebSocket, stores every message to SQLite, and
exposes an HTTP API on port 8080. Runs as a systemd service.

**Endpoints** (all require `X-API-Token` header):

| Endpoint | Description |
|---|---|
| `GET /alerts` | Paginated alert history |
| `GET /alerts?threat=0` | Filter by threat type |
| `GET /alerts?since=2025-01-15T00:00:00Z` | Filter by time |
| `GET /alerts?limit=50&offset=100` | Pagination |
| `GET /alerts/latest` | Most recent alert |
| `GET /status` | WebSocket connection health |

### `lambda/handler.py`

Thin proxy — forwards requests from the public Function URL to the
EC2 private IP, injecting the shared auth token.

## Alert schema

Every alert object matches Tzofar's WebSocket payload exactly,
with one field added:

```json
{
  "type":        "ALERT",
  "time":        1773423099,
  "threat":      0,
  "isDrill":     false,
  "cities":      ["תל אביב - דרום העיר ויפו", "בת ים"],
  "received_at": "2025-01-15T14:31:39.123Z"
}
```

### Threat types

| `threat` | Type |
|---|---|
| 0 | Rockets / missiles (Red Alert) |
| 1 | Unconventional missile |
| 2 | Earthquake |
| 3 | Tsunami |
| 4 | Hostile aircraft / UAV |
| 5 | Terrorist infiltration |
| 6 | Hazardous materials |
| 7 | Radiological incident |
| 8 | Safe to leave shelter |

## Setup

### EC2

```bash
# 1. Install deps
sudo apt update && sudo apt install -y python3-pip python3-venv
sudo mkdir -p /opt/tzofar && sudo chown admin:admin /opt/tzofar
python3 -m venv /opt/tzofar/venv
/opt/tzofar/venv/bin/pip install websockets

# 2. Copy files
cp ec2/listener.py /opt/tzofar/listener.py
sudo cp ec2/tzofar.service /etc/systemd/system/tzofar.service

# 3. Set your token in the service file
sudo nano /etc/systemd/system/tzofar.service
# Change: Environment="API_TOKEN=CHANGE_ME"

# 4. Enable and start
sudo systemctl daemon-reload
sudo systemctl enable --now tzofar
sudo journalctl -u tzofar -f
```

### Lambda

1. Create function in `il-central-1`, Python 3.12, ARM64
2. Paste contents of `lambda/handler.py`
3. Set environment variables:
   - `VM_BASE_URL` = `http://YOUR_EC2_PRIVATE_IP:8080`
   - `API_TOKEN` = same value as in the service file
4. Attach to VPC, use the `tzofar-lambda-sg` security group
5. Enable Function URL, auth type: NONE
6. Set timeout to 15 seconds

## Usage

```bash
LAMBDA="https://YOUR_LAMBDA_FUNCTION_URL"

# Last 100 alerts
curl "$LAMBDA/alerts"

# Rocket alerts only
curl "$LAMBDA/alerts?threat=0"

# Paginate
curl "$LAMBDA/alerts?limit=50&offset=50"

# Since a specific time
curl "$LAMBDA/alerts?since=2025-01-15T00:00:00Z"

# Latest single alert
curl "$LAMBDA/alerts/latest"

# WebSocket connection health
curl "$LAMBDA/status"
```

## Cost

~$18/year (EC2 t4g.nano in il-central-1). Lambda and intra-VPC
data transfer are effectively free at this data volume.
