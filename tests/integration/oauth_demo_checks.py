"""OAuth checks mixed into the existing owned demo HTTP harness.

No real account, external identity provider, pre-existing listener or third-party
OAuth SDK is involved. The optional compiled consumer uses the Terrane client.
"""
import base64
import hashlib
from html.parser import HTMLParser
import json
import subprocess
from urllib.parse import parse_qs, urlencode, urlsplit


class ConsentParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.transactions = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "input" and attributes.get("name") == "request_id":
            self.transactions.append(attributes.get("value"))


class OAuthDisabledChecks:
    def test_oauth_is_explicitly_disabled_by_default(self):
        self.assertEqual(self.request("GET", "/oauth")[0], 200)
        for method, path, body in [
            ("GET", "/oauth/authorize", None),
            ("POST", "/oauth/authorize", "request_id=missing&decision=allow"),
            ("POST", "/oauth/token", "grant_type=refresh_token&refresh_token=missing"),
            ("GET", "/oauth/resource", None),
            ("GET", "/oauth/callback", None),
        ]:
            with self.subTest(method=method, path=path):
                self.assertEqual(self.request(method, path, body,
                    content_type="application/x-www-form-urlencoded")[0], 404)


class OAuthBrowserHelpers:
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"

    def authorization_parameters(self, **overrides):
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).rstrip(b"=").decode()
        values = dict(response_type="code", client_id="terrane-demo",
            redirect_uri=self.origin + "/oauth/callback", scope="demo.read",
            state="state + & café", code_challenge=challenge, code_challenge_method="S256")
        values.update(overrides)
        return values

    def transaction_from_page(self, target):
        status, content_type, page = self.request("GET", target)
        self.assertEqual(status, 200)
        self.assertTrue(content_type.startswith("text/html"))
        parsed = ConsentParser()
        parsed.feed(page)
        self.assertEqual(len(parsed.transactions), 1)
        self.assertTrue(parsed.transactions[0])
        return parsed.transactions[0]

    def decide(self, transaction, decision="allow"):
        connection, response = self.response("POST", "/oauth/authorize",
            urlencode(dict(request_id=transaction, decision=decision)),
            content_type="application/x-www-form-urlencoded")
        try:
            response.read()
            self.assertEqual(response.status, 303)
            callback = response.getheader("Location")
            self.assertIsNotNone(callback)
            expected = urlsplit(self.origin + "/oauth/callback")
            actual = urlsplit(callback)
            self.assertEqual((actual.scheme, actual.netloc, actual.path),
                (expected.scheme, expected.netloc, expected.path))
            return callback
        finally:
            response.close()
            connection.close()

    def authorize(self):
        parameters = self.authorization_parameters()
        transaction = self.transaction_from_page("/oauth/authorize?" + urlencode(parameters))
        callback = self.decide(transaction)
        query = parse_qs(urlsplit(callback).query, keep_blank_values=True)
        self.assertEqual(query["state"], [parameters["state"]])
        self.assertEqual(len(query["code"]), 1)
        return query["code"][0], parameters

    def token_request(self, values, json_mode=False):
        body = json.dumps(values) if json_mode else urlencode(values)
        content_type = "application/json" if json_mode else "application/x-www-form-urlencoded"
        connection, response = self.response("POST", "/oauth/token", body, content_type)
        try:
            payload = json.loads(response.read())
            self.assertEqual(response.getheader("Content-Type").split(";")[0], "application/json")
            self.assertIn("no-store", response.getheader("Cache-Control", ""))
            self.assertEqual(response.getheader("Pragma"), "no-cache")
            return response.status, payload
        finally:
            response.close()
            connection.close()

    def exchange_values(self, code, parameters, **overrides):
        values = dict(grant_type="authorization_code", client_id="terrane-demo",
            redirect_uri=parameters["redirect_uri"], code=code, code_verifier=self.verifier)
        values.update(overrides)
        return values

    def grant(self):
        code, parameters = self.authorize()
        status, tokens = self.token_request(self.exchange_values(code, parameters))
        self.assertEqual(status, 200)
        self.assertEqual(tokens["token_type"], "Bearer")
        self.assertEqual(tokens["scope"], "demo.read")
        self.assertGreater(tokens["expires_in"], 0)
        return tokens

    def resource(self, token):
        return self.request("GET", "/oauth/resource", headers={"Authorization": "Bearer " + token})


class OAuthProviderChecks(OAuthBrowserHelpers):
    def test_pkce_exchange_protects_resource_and_code_replay(self):
        connection, response = self.response("GET", "/oauth/resource")
        try:
            response.read()
            self.assertEqual(response.status, 401)
            self.assertTrue(response.getheader("WWW-Authenticate", "").startswith("Bearer"))
        finally:
            response.close()
            connection.close()
        code, parameters = self.authorize()
        request = self.exchange_values(code, parameters)
        status, tokens = self.token_request(request)
        self.assertEqual(status, 200)
        status, _, body = self.resource(tokens["access_token"])
        self.assertEqual((status, body["subject"], body["scope"]), (200, "demo-user", "demo.read"))
        status, failure = self.token_request(request)
        self.assertEqual((status, failure["error"]), (400, "invalid_grant"))
        self.assertEqual(self.resource("not-a-token")[0], 401)

    def test_wrong_pkce_redirect_and_client_do_not_consume_code(self):
        code, parameters = self.authorize()
        for overrides in [dict(code_verifier="x" * 43),
            dict(redirect_uri="http://127.0.0.1:1/oauth/callback"), dict(client_id="other-client")]:
            with self.subTest(fields=list(overrides)):
                status, failure = self.token_request(self.exchange_values(code, parameters, **overrides))
                self.assertEqual(status, 400)
                self.assertIn(failure["error"], ("invalid_grant", "invalid_client"))
        self.assertEqual(self.token_request(self.exchange_values(code, parameters))[0], 200)

    def test_form_and_json_refresh_rotate_and_reject_replay(self):
        first = self.grant()
        values = dict(grant_type="refresh_token", client_id="terrane-demo", refresh_token=first["refresh_token"])
        status, second = self.token_request(values)
        self.assertEqual(status, 200)
        self.assertNotEqual(second["refresh_token"], first["refresh_token"])
        self.assertNotEqual(second["access_token"], first["access_token"])
        self.assertEqual(self.resource(first["access_token"])[0], 401)
        self.assertEqual(self.resource(second["access_token"])[0], 200)
        status, failure = self.token_request(values)
        self.assertEqual((status, failure["error"]), (400, "invalid_grant"))
        values["refresh_token"] = second["refresh_token"]
        status, third = self.token_request(values, json_mode=True)
        self.assertEqual(status, 200)
        self.assertNotEqual(third["refresh_token"], second["refresh_token"])
        self.assertEqual(self.resource(second["access_token"])[0], 401)
        self.assertEqual(self.resource(third["access_token"])[0], 200)

    def test_denial_returns_state_and_consumes_consent(self):
        parameters = self.authorization_parameters()
        transaction = self.transaction_from_page("/oauth/authorize?" + urlencode(parameters))
        callback = self.decide(transaction, "deny")
        values = parse_qs(urlsplit(callback).query)
        self.assertEqual(values["error"], ["access_denied"])
        self.assertEqual(values["state"], [parameters["state"]])
        self.assertNotIn("code", values)
        self.assertEqual(self.request("POST", "/oauth/authorize",
            urlencode(dict(request_id=transaction, decision="allow")),
            content_type="application/x-www-form-urlencoded")[0], 400)

    def test_untrusted_redirects_and_ambiguous_authorization_are_not_redirected(self):
        valid = list(self.authorization_parameters().items())
        cases = [self.authorization_parameters(redirect_uri="https://outside.example/oauth/callback"),
            self.authorization_parameters(code_challenge_method="plain"),
            self.authorization_parameters(state=""),
            valid + [("state", "duplicate")], valid + [("client_id", "terrane-demo")]]
        for values in cases:
            with self.subTest(parameters=values):
                connection, response = self.response("GET", "/oauth/authorize?" + urlencode(values))
                try:
                    response.read()
                    self.assertEqual(response.status, 400)
                    self.assertIsNone(response.getheader("Location"))
                finally:
                    response.close()
                    connection.close()

    def test_token_errors_and_form_validation(self):
        status, failure = self.token_request([("grant_type", "refresh_token"),
            ("grant_type", "authorization_code"), ("client_id", "terrane-demo")])
        self.assertEqual((status, failure["error"]), (400, "invalid_request"))
        status, failure = self.token_request(dict(grant_type="unsupported", client_id="terrane-demo"))
        self.assertEqual((status, failure["error"]), (400, "unsupported_grant_type"))
        self.assertEqual(self.request("POST", "/oauth/token", "bad=%ZZ",
            content_type="application/x-www-form-urlencoded")[0], 422)
        self.assertEqual(self.request("POST", "/oauth/token", "grant_type=refresh_token",
            content_type="text/plain")[0], 415)


class OAuthClientChecks(OAuthBrowserHelpers):
    def consumer(self, action, value=None, success=True):
        completed = subprocess.run([str(self.client_binary), action, self.origin],
            input="" if value is None else json.dumps(value), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        if success:
            self.assertEqual(completed.returncode, 0, completed.stderr)
            return json.loads(completed.stdout)
        self.assertNotEqual(completed.returncode, 0)
        return completed.stderr

    def callback_for(self, authorization, decision="allow"):
        target = urlsplit(authorization["url"])
        transaction = self.transaction_from_page(target.path + "?" + target.query)
        return self.decide(transaction, decision)

    def test_actual_terrane_client_completes_and_refreshes_both_encodings(self):
        begun = self.consumer("begin")
        callback = self.callback_for(begun)
        handoff = dict(begun, callback_url=callback)
        first = self.consumer("complete", handoff)
        resource = self.consumer("resource", first)
        self.assertEqual((resource["status"], resource["body"]["subject"]), (200, "demo-user"))
        replay = self.consumer("complete", handoff, success=False)
        self.assertIn("[token-endpoint]", replay)
        second = self.consumer("refresh", first)
        self.assertEqual(self.consumer("resource", first)["status"], 401)
        self.assertEqual(self.consumer("resource", second)["status"], 200)
        third = self.consumer("refresh", dict(second, json=True))
        self.assertNotEqual(third["refresh_token"], second["refresh_token"])
        self.assertEqual(self.consumer("resource", second)["status"], 401)
        self.assertEqual(self.consumer("resource", third)["status"], 200)
        failure = self.consumer("refresh", second, success=False)
        self.assertIn("[token-endpoint]", failure)

    def test_actual_client_rejects_denial_and_wrong_callback_state(self):
        begun = self.consumer("begin")
        callback = self.callback_for(begun, "deny")
        failure = self.consumer("complete", dict(begun, callback_url=callback), success=False)
        self.assertIn("[provider]", failure)
        begun = self.consumer("begin")
        callback = self.callback_for(begun)
        handoff = dict(begun, callback_url=callback)
        wrong = dict(handoff, state="different-state")
        failure = self.consumer("complete", wrong, success=False)
        self.assertIn("[callback]", failure)
        tokens = self.consumer("complete", handoff)
        self.assertEqual(self.consumer("resource", tokens)["status"], 200)
