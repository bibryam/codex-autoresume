import http.client, json, threading, unittest
from codex_autoresume.web import make_server


class FakeScheduler:
    def __init__(self):
        self.calls = []
        self.known = None
    def known_threads(self):
        return self.known
    def snapshot(self):
        return {"now": 1, "engine": "ok", "limits": None, "note": "", "entries": [], "running": None, "history": [], "log": [], "config": {}}
    def set_skip(self, tid, flag): self.calls.append(("skip", tid, flag))
    def schedule(self, tid): self.calls.append(("schedule", tid))
    def set_message(self, tid, message, model=None, effort=None): self.calls.append(("message", tid, message, model, effort))
    def resume_now(self, tid): self.calls.append(("now", tid))


class WebBase(unittest.TestCase):
    def setUp(self):
        self.sched = FakeScheduler()
        self.server = make_server(self.sched, port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close()

    def req(self, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers={"Content-Type": "application/json"})
        r = c.getresponse(); data = r.read(); c.close()
        return r.status, r.getheader("Content-Type", ""), data


class WebTest(WebBase):
    def test_page_and_state(self):
        st, ct, data = self.req("GET", "/")
        self.assertEqual(st, 200); self.assertIn("text/html", ct); self.assertIn(b"codex-autoresume", data)
        st, ct, data = self.req("GET", "/api/state")
        self.assertEqual(st, 200); self.assertEqual(json.loads(data)["engine"], "ok")

    def test_actions(self):
        self.assertEqual(self.req("POST", "/api/threads/t1/skip")[0], 200)
        self.assertEqual(self.req("POST", "/api/threads/t1/unskip")[0], 404)
        self.assertEqual(self.req("POST", "/api/threads/t1/resume-now")[0], 200)
        self.assertEqual(self.req("POST", "/api/threads/t1/schedule")[0], 200)
        self.assertEqual(self.sched.calls, [("skip", "t1", True), ("now", "t1"), ("schedule", "t1")])

    def test_post_message_roundtrip(self):
        msg = 'say "hi" — ünï'
        st, _, data = self.req("POST", "/api/threads/t1/message", {"message": msg, "model": "gpt-6-astra", "effort": "xhigh"})
        self.assertEqual(st, 200); self.assertTrue(json.loads(data)["ok"])
        self.assertEqual(self.sched.calls[-1], ("message", "t1", msg, "gpt-6-astra", "xhigh"))

    def test_unknown_routes(self):
        self.assertEqual(self.req("GET", "/nope")[0], 404)
        self.assertEqual(self.req("POST", "/api/threads/t1/explode")[0], 404)
        self.assertEqual(self.req("POST", "/api/threads/t1/message", None)[0], 400)


class HardeningTest(WebBase):
    def test_rejects_foreign_host_header(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/api/state", headers={"Host": "evil.example:80"})
        r = c.getresponse(); r.read(); c.close()
        self.assertEqual(r.status, 403)

    def test_post_requires_json_content_type(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("POST", "/api/threads/t1/skip", body="x", headers={"Content-Type": "text/plain"})
        r = c.getresponse(); r.read(); c.close()
        self.assertEqual(r.status, 415)
        self.assertEqual(self.sched.calls, [])

    def test_unknown_thread_id_is_rejected(self):
        self.sched.known = {"t1"}
        self.assertEqual(self.req("POST", "/api/threads/t1/skip")[0], 200)
        self.assertEqual(self.req("POST", "/api/threads/nope/skip")[0], 404)


class ThreadIdValidationTest(WebBase):
    def test_malformed_thread_ids_are_rejected_before_any_action(self):
        for bad in ("..%2F..%2Fetc", "a%20b", "%3Cscript%3E", "x" * 80):
            self.assertEqual(self.req("POST", f"/api/threads/{bad}/skip")[0], 400, bad)
        self.assertEqual(self.sched.calls, [])
        self.assertEqual(self.req("POST", "/api/threads/01a0fe2e-7ce2-7211-b63e-19e5d0419055/skip")[0], 200)


class MessageFieldTypesTest(WebBase):
    def test_model_and_effort_must_be_short_strings(self):
        st, _, _ = self.req("POST", "/api/threads/t1/message", {"message": "go", "model": 5, "effort": ["x"]})
        self.assertEqual(st, 400)
        st, _, _ = self.req("POST", "/api/threads/t1/message", {"message": "go", "model": "m" * 65})
        self.assertEqual(st, 400)
        self.assertEqual(self.sched.calls, [])
        st, _, _ = self.req("POST", "/api/threads/t1/message", {"message": "go", "model": "gpt-6-astra", "effort": None})
        self.assertEqual(st, 200)
        self.assertEqual(self.sched.calls[-1], ("message", "t1", "go", "gpt-6-astra", None))
