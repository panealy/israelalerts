import json
import os
import urllib.request
import urllib.parse

VM_BASE_URL  = os.environ["VM_BASE_URL"]
API_TOKEN    = os.environ["API_TOKEN"]
ROUTE_PREFIX = "/default/tzofar-proxy"

def lambda_handler(event, context):
    qs_params = event.get("queryStringParameters") or {}
    raw_path  = event.get("rawPath", "/alerts")

    path = raw_path
    if path.startswith(ROUTE_PREFIX):
        path = path[len(ROUTE_PREFIX):]

    if not path or path == "/":
        path = "/alerts"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    if qs_params:
        path = path + "?" + urllib.parse.urlencode(qs_params)

    url = VM_BASE_URL.rstrip("/") + path

    try:
        req = urllib.request.Request(url, headers={"X-API-Token": API_TOKEN})
        with urllib.request.urlopen(req, timeout=10) as resp:
            body   = resp.read().decode("utf-8")
            status = resp.status
    except urllib.error.HTTPError as e:
        body   = e.read().decode("utf-8")
        status = e.code
    except Exception as e:
        body   = json.dumps({"error": str(e)})
        status = 502

    return {
        "statusCode": status,
        "headers": {
            "Content-Type":                "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": body,
    }
