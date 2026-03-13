import json
import os
import urllib.request
import urllib.parse

VM_BASE_URL = os.environ["VM_BASE_URL"]   # e.g. http://10.0.1.42:8080
API_TOKEN   = os.environ["API_TOKEN"]     # shared secret with EC2 service

def lambda_handler(event, context):
    qs_params = event.get("queryStringParameters") or {}
    path      = event.get("rawPath", "/alerts")

    # Build forwarded URL preserving query string
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
