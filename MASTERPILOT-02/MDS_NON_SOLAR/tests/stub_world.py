"""Recording stubs for running the REAL provisioning CLI offline (provisioner boundary tests).

- StubApi: a local HTTP server answering the data-server API calls the provisioning phases make, recording every request
  (method, path, query, body). The CLI reaches it only because the profile's api.url points there.
- write_sitecustomize(): a sitecustomize.py for the child process that (1) blocks every socket except 127.0.0.1:<stub>,
  (2) installs a recording fake `boto3` (Session/STS/S3/SQS) that records profile, region, bucket, key and queue of every call.
Nothing here can reach a real system.
"""
from __future__ import annotations

import json
import threading
import uuid as uuid_lib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


def kvs(d: dict) -> str:
    return json.dumps({"kvs": [{"key": k, "val": v if isinstance(v, str) else json.dumps(v), "configSource": "PILOT"} for k, v in d.items()]})


class StubApi:
    def __init__(self, pilot: int, flat_plan: str, tou_plan: str | None, bucket: str, gas: bool = True, extra_configs: dict | None = None):
        self.pilot, self.records, self.users = pilot, [], {}
        ss = {"supportedStreams": [{"measurementType": "ELECTRIC", "streams": [{"streamType": "DELIVERED"}, {"streamType": "RECEIVED"}]}]
              + ([{"measurementType": "GAS", "streams": [{"streamType": "DELIVERED"}]}] if gas else [])}
        self.configs = {
            "launchpad_ingestion_configs": {"create_streams_based_on_supported_streams_config": "true", "handle_meter_enrollment": "true",
                                            "isEndDateInclusive": "false", "supported_streams": ss, "raw_data_date_type": "DATE",
                                            "raw_data_date_format": "yyyyMMddHHmmss", "parser_time_zone": "UTC",
                                            "raw_data_consumption_unit_map": {"ELECTRIC": "Wh", "GAS": "ccf"}},
            "measurement_type": {"allowed_measurement_type": "Electricity,Gas"},
            "data_ingestion": {"s3BucketName": bucket},
            "meta_data": {"supported_solar_communication_events": "MONTHLY_SUMMARY,USER_WELCOME"},
            "disagg_preference": {"enable_solar": "true"}, "hybrid_moderation": {"electric_hybrid_disagg_enabled": "true"},
        }
        self.configs.update(extra_configs or {})
        rows = [{"chargeType": "CONSUMPTION_BASED", "rate": r, "attributes": {"data_stream": s}, "monthLow": 1, "monthHigh": 12,
                 "dayLow": 1, "dayHigh": 31, "timeOfDayLow": 0, "timeOfDayHigh": 23, "weekLow": 1, "weekHigh": 7, "validLow": 0,
                 "validHigh": 2147483647, "consumptionLow": 0, "consumptionHigh": 0, "touName": "ALL", "isHoliday": False}
                for s, r in (("DELIVERED", 0.15), ("RECEIVED", 0.05))]
        self.plans = {flat_plan: (9101, rows)} | ({tou_plan: (9102, rows)} if tou_plan else {})
        self.uploads, self.bc_rows, self.meters, self.home_plan, self.invoices = [], [], {}, None, {}
        self.user_configs: dict = {}
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code: int, obj) -> None:
                raw = json.dumps({"requestId": "stub", "payload": obj}).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _handle(self, method: str) -> None:
                u = urlparse(self.path)
                body = None
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    try:
                        body = json.loads(self.rfile.read(n))
                    except ValueError:
                        body = "<unparsed>"
                path = u.path.strip("/")
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                if path == "__stub/upload":                       # the fake boto3 forwards S3 puts (ingestion emulation)
                    stub.ingest(body["bucket"], body["key"], body["text"])
                    return self._send(200, {"ok": True})
                stub.records.append({"method": method, "path": path, "query": q, "body": body, "host": self.headers.get("Host")})
                code, obj = stub.route(method, path, q, body)
                if code == "bare":                                 # endpoints the product answers without a payload wrapper
                    raw = json.dumps(obj).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                self._send(code, obj)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def do_PUT(self):
                self._handle("PUT")

            def do_DELETE(self):
                self._handle("DELETE")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self.httpd.shutdown()

    # ---- ingestion emulation (what launchpad would make visible through the API) --------------------------
    @staticmethod
    def _epoch(d: str) -> int:
        from datetime import datetime, timezone
        fmt = "%Y%m%d%H%M%S" if len(d) == 14 else "%Y-%m-%d"
        return int(datetime.strptime(d, fmt).replace(tzinfo=timezone.utc).timestamp())

    def ingest(self, bucket: str, key: str, text: str) -> None:
        self.uploads.append({"bucket": bucket, "key": key})
        rows = [ln.split("|") for ln in text.splitlines() if ln.strip()]
        if key.startswith("BC_SCHEDULE"):
            self.bc_rows += [{"billCycleCode": r[0], "validFrom": self._epoch(r[1])} for r in rows]
        elif key.startswith("METERENROLL"):
            for r in rows:
                c, a, p, meter, fuel = r[0], r[1], r[2], r[3], r[4]
                self.home_plan = r[7] or self.home_plan
                base = f"{self.pilot}:{c}_{a}_{p}_{meter}"
                self.meters[f"{base}_AMI"] = {"token": f"{base}_AMI", "streamType": "DELIVERED", "fuel": fuel}
                if fuel == "ELECTRIC":
                    self.meters[f"{base}-R_AMI"] = {"token": f"{base}-R_AMI", "streamType": "RECEIVED", "fuel": fuel}
        elif key.startswith("RAW_"):
            if not rows:
                return
            r0 = rows[0]
            tok = f"{self.pilot}:{r0[0]}_{r0[1]}_{r0[2]}_{r0[3]}_AMI"
            ts = [self._epoch(r[4]) for r in rows]
            if tok in self.meters:
                self.meters[tok].update(firstDataTimestamp=str(min(ts)), lastDataTimestamp=str(max(ts)))
        elif key.startswith("INVOICE"):
            for r in rows:
                if r[9] == "TOTAL":
                    k = "cost" if r[4] == "ELECTRIC" else "gasCost"
                    self.invoices.setdefault(r[4], {}).setdefault(str(self._epoch(r[6])), {})[k] = float(r[12])

    # ---- routes -----------------------------------------------------------------------------------------
    def route(self, method: str, path: str, q: dict, body):
        p = path.split("/")
        if method == "GET" and len(p) == 5 and p[0] == "entities" and p[3] == "configs":
            ctype = p[4]
            if p[1] == "pilot" and str(p[2]) == str(self.pilot) and ctype in self.configs:
                return 200, {ctype: kvs(self.configs[ctype])}
            if p[1] == "user" and (p[2], ctype) in self.user_configs:
                return 200, {ctype: json.dumps({"kvs": [{"key": k, "val": v, "configSource": "USER"}
                                                        for k, v in self.user_configs[(p[2], ctype)].items()]})}
            return 200, {}
        if method == "POST" and len(p) == 3 and p[0] == "entities" and p[2] == "configs" and isinstance(body, dict):
            d = self.user_configs.setdefault((p[1], body.get("configType")), {})
            d.update({kv["configKey"]: kv["configVal"] for kv in body.get("configKVs") or []})
            return 200, {"status": True}
        if method == "GET" and path == f"v3.0/rates/configuration/utilityId/{self.pilot}":
            return 200, [{"planName": k, "planNumber": v[0]} for k, v in self.plans.items()]
        if method == "GET" and path.startswith(f"v3.0/rates/utilities/{self.pilot}/plans/") and path.endswith("/structure"):
            num = int(p[5])
            rows = next((v[1] for v in self.plans.values() if v[0] == num), None)
            return (200, rows) if rows else (404, None)
        if method == "GET" and path == "v2.0/pii/users":
            ext = q.get("externalUserId")
            if str(q.get("pilotId")) != str(self.pilot):
                return 200, {"data": []}
            u = self.users.setdefault(ext, str(uuid_lib.uuid5(uuid_lib.NAMESPACE_URL, f"{self.pilot}/{ext}")))
            return 200, {"data": [{"uuid": u, "externalUserId": ext, "pilotId": self.pilot}]}
        if method == "GET" and path.startswith(f"2.1/utilityBillingCycles/utility/{self.pilot}"):
            return 200, list(self.bc_rows)
        if method == "GET" and len(p) == 6 and p[0] == "meta" and p[5] == "gws":
            return ("bare", {"gws/1": {"endpointId": 1}}) if self.meters else (404, None)
        if method == "GET" and len(p) == 8 and p[0] == "meta" and p[5] == "gws" and p[7] == "meters":
            return "bare", {f"meters/{i}": dict(m) for i, m in enumerate(self.meters.values(), 1)}
        if method == "GET" and len(p) == 5 and p[0] == "meta" and p[3] == "homes":
            return 200, {"ratePlanId": self.home_plan, "solarUser": "true"}
        if method == "GET" and path.startswith("billingdata/users/") and path.endswith("/utilitydata"):
            return "bare", dict(self.invoices.get(q.get("measurementType"), {}))
        if method == "GET":
            return 200, {}
        return 200, {"status": True}


SITE = r'''
import json, os, socket, sys, types
ALLOW = os.environ["STUB_ALLOW"]            # "127.0.0.1:<port>"
REC = os.environ["STUB_RECORD"]
_real_connect = socket.socket.connect
def _guard(self, addr):
    if isinstance(addr, tuple) and f"{addr[0]}:{addr[1]}" != ALLOW:
        _rec({"blocked_connect": f"{addr[0]}:{addr[1]}"})
        raise OSError(f"network blocked in the provisioner boundary test: {addr}")
    return _real_connect(self, addr)
socket.socket.connect = _guard
def _rec(o):
    with open(REC, "a") as f:
        f.write(json.dumps(o, default=str) + "\n")
ACCOUNTS = json.loads(os.environ.get("STUB_ACCOUNTS", "{}"))
STORE = {}
class _Err(Exception):
    def __init__(self, code):
        super().__init__(code); self.response = {"Error": {"Code": code}}
class _Client:
    def __init__(self, svc, prof, region):
        self.svc, self.prof, self.region = svc, prof, region
    def _r(self, op, **k):
        _rec(dict(svc=self.svc, op=op, profile=self.prof, region=self.region, **{x: v for x, v in k.items() if x != "Body"}))
    def get_caller_identity(self):
        self._r("get_caller_identity")
        return {"Account": ACCOUNTS.get(self.prof, "000000000000"), "Arn": f"arn:aws:iam::x:user/{self.prof}"}
    def head_bucket(self, Bucket):
        self._r("head_bucket", Bucket=Bucket)
        return {}
    def head_object(self, Bucket, Key):
        self._r("head_object", Bucket=Bucket, Key=Key)
        if Key.endswith(".processed"):
            return {"ContentLength": 0, "Metadata": {}}
        if (Bucket, Key) in STORE:
            return {"ContentLength": len(STORE[(Bucket, Key)][0]), "Metadata": STORE[(Bucket, Key)][1]}
        if Key.endswith(".xlsx"):
            return {"ContentLength": 1, "Metadata": {}}
        raise _Err("404")
    def put_object(self, Bucket, Key, Body=b"", Metadata=None, **k):
        import hashlib
        raw = Body if isinstance(Body, bytes) else str(Body).encode()
        # the exact bytes this fake S3 received (manifest integrity is checked against them)
        self._r("put_object", Bucket=Bucket, Key=Key, Metadata=Metadata, sha256=hashlib.sha256(raw).hexdigest(), size=len(raw))
        STORE[(Bucket, Key)] = (Body, Metadata or {})
        import urllib.request
        data = json.dumps({"bucket": Bucket, "key": Key, "text": (Body.decode() if isinstance(Body, bytes) else str(Body))}).encode()
        urllib.request.urlopen(urllib.request.Request(f"http://{ALLOW}/__stub/upload", data=data, method="POST",
                                                      headers={"Content-Type": "application/json"}), timeout=30).read()
        return {}
    def get_object(self, Bucket, Key):
        self._r("get_object", Bucket=Bucket, Key=Key)
        import io
        return {"Body": io.BytesIO(STORE.get((Bucket, Key), (b"", {}))[0])}
    def list_objects_v2(self, **k):
        self._r("list_objects_v2", **k)
        return {}
    def get_queue_url(self, QueueName):
        self._r("get_queue_url", QueueName=QueueName)
        return {"QueueUrl": f"https://sqs.{self.region}.stub/{QueueName}"}
    def send_message(self, QueueUrl, MessageBody):
        self._r("send_message", QueueUrl=QueueUrl, size=len(MessageBody))
        return {"MessageId": "stub-msg"}
class Session:
    def __init__(self, profile_name=None, region_name=None):
        self.prof, self.region = profile_name, region_name
        _rec({"svc": "session", "profile": profile_name, "region": region_name})
    def client(self, svc, *a, **k):
        return _Client(svc, self.prof, self.region)
boto3 = types.ModuleType("boto3"); boto3.Session = Session
sys.modules["boto3"] = boto3
'''


def write_sitecustomize(d: Path) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / "sitecustomize.py").write_text(SITE)
    return d
