"""大模型协议和错误处理验证；使用假响应，不消耗实际 API。"""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from wxbot_framework.llm import LLMClient, LLMError
from plugins.idiom_chain.judge import IdiomJudge


class JSONClient:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def complete_json(self, system, user):
        self.calls.append((system, user))
        return self.result


class LLMTests(unittest.TestCase):
    def setUp(self):
        self.options = {"api_key": "dummy-test-key", "model": "deepseek-flash"}
        self.client = LLMClient(self.options)

    def response(self, content, **extra):
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": content}, **extra}]}).encode())

    def test_official_json_request_and_utf8(self):
        with patch("wxbot_framework.llm.urlopen", return_value=self.response('{"idiom":"一心一意"}')) as call:
            self.assertEqual(self.client.complete_json("只返回 JSON", "中文"), {"idiom": "一心一意"})
        request = call.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.deepseek.com/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer dummy-test-key")
        payload = json.loads(request.data)
        self.assertEqual(payload["model"], "deepseek-flash")
        self.assertEqual(payload["messages"][1]["content"], "中文")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(call.call_args.kwargs["timeout"], 20)

    def test_compatible_gateway_preserves_version_path_without_vendor_parameters(self):
        self.client = LLMClient({**self.options, "base_url": "https://gateway.example.test/v1/", "model": "custom-model"})
        with patch("wxbot_framework.llm.urlopen", return_value=self.response('{"reply":"你好"}')) as call:
            self.assertEqual(self.client.complete_json("返回 JSON", "你好"), {"reply": "你好"})
        request = call.call_args.args[0]
        self.assertEqual(request.full_url, "https://gateway.example.test/v1/chat/completions")
        self.assertEqual(set(json.loads(request.data)), {"model", "messages", "response_format", "max_tokens", "stream"})

    def test_full_endpoint_is_not_appended_twice(self):
        client = LLMClient({**self.options, "base_url": "https://gateway.example.test/v1/chat/completions/"})
        with patch("wxbot_framework.llm.urlopen", return_value=self.response('{}')) as call:
            client.complete_json("JSON", "测试")
        self.assertEqual(call.call_args.args[0].full_url, "https://gateway.example.test/v1/chat/completions")

    def test_completion_token_parameter_sends_only_selected_limit(self):
        client = LLMClient({**self.options, "token_limit_parameter": "max_completion_tokens", "max_tokens": 2048})
        with patch("wxbot_framework.llm.urlopen", return_value=self.response('{}')) as call:
            client.complete_json("JSON", "测试")
        body = json.loads(call.call_args.args[0].data)
        self.assertEqual(body["max_completion_tokens"], 2048)
        self.assertNotIn("max_tokens", body)

    def test_prompt_json_mode_omits_response_format_and_accepts_fenced_json(self):
        client = LLMClient({**self.options, "json_mode": False})
        for content in ('{"reply":"你好"}', '```json\n{"reply":"你好"}\n```', '```\n{"reply":"你好"}\n```'):
            with self.subTest(content=content), patch("wxbot_framework.llm.urlopen", return_value=self.response(content)) as call:
                self.assertEqual(client.complete_json("原功能规则", "测试"), {"reply": "你好"})
                body = json.loads(call.call_args.args[0].data)
                self.assertNotIn("response_format", body)
                self.assertIn("原功能规则", body["messages"][0]["content"])
                self.assertIn("JSON 对象", body["messages"][0]["content"])

    def test_prompt_mode_still_requires_a_single_json_object(self):
        client = LLMClient({**self.options, "json_mode": False})
        for content in ('这是普通回答', '[]', '前言\n```json\n{}\n```', '```json\n{}\n```\n后记', '{} {}'):
            with self.subTest(content=content), patch("wxbot_framework.llm.urlopen", return_value=self.response(content)):
                with self.assertRaises(LLMError):
                    client.complete_json("JSON", "测试")

    def test_vendor_parameters_are_opt_in_and_copied(self):
        extra = {"thinking": {"type": "disabled"}, "temperature": 0.5}
        client = LLMClient({**self.options, "extra_body": extra})
        extra["thinking"]["type"] = "enabled"
        with patch("wxbot_framework.llm.urlopen", return_value=self.response('{}')) as call:
            client.complete_json("JSON", "测试")
        body = json.loads(call.call_args.args[0].data)
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual(body["temperature"], 0.5)

    def test_explicit_reasoning_options_override_official_defaults(self):
        for extra in ({"thinking":{"type":"enabled"}}, {"reasoning_effort":"low"}):
            with self.subTest(extra=extra):
                client = LLMClient({**self.options, "extra_body":extra})
                with patch("wxbot_framework.llm.urlopen", return_value=self.response('{}')) as call:
                    client.complete_json("JSON", "测试")
                body = json.loads(call.call_args.args[0].data)
                for key, value in extra.items():
                    self.assertEqual(body[key], value)
                if "thinking" not in extra:
                    self.assertNotIn("thinking", body)

    def test_legacy_reasoner_and_similar_host_do_not_receive_non_thinking_default(self):
        for change in ({"model":"deepseek-reasoner"}, {"base_url":"https://api.deepseek.com.example.test/v1"}):
            with self.subTest(change=change):
                client = LLMClient({**self.options, **change})
                with patch("wxbot_framework.llm.urlopen", return_value=self.response('{}')) as call:
                    client.complete_json("JSON", "测试")
                self.assertNotIn("thinking", json.loads(call.call_args.args[0].data))

    def test_official_empty_json_response_retries_once_in_prompt_mode(self):
        with patch("wxbot_framework.llm.urlopen", side_effect=[self.response(''),self.response('```json\n{"reply":"成功"}\n```')]) as call:
            self.assertEqual(self.client.complete_json("原功能规则 JSON", "原输入"), {"reply":"成功"})
        self.assertEqual(call.call_count, 2)
        first, second = [json.loads(args.args[0].data) for args in call.call_args_list]
        self.assertIn("response_format", first)
        self.assertNotIn("response_format", second)
        self.assertIn("原功能规则", second["messages"][0]["content"])
        self.assertEqual(first["messages"][1], second["messages"][1])
        self.assertEqual(first["thinking"], second["thinking"])
        self.assertTrue(self.client.json_mode)

    def test_gateway_empty_response_retry_keeps_configured_protocol(self):
        client = LLMClient({**self.options,"base_url":"https://gateway.example.test/v1"})
        with patch("wxbot_framework.llm.urlopen", side_effect=[self.response(None),self.response('{"reply":"成功"}')]) as call:
            self.assertEqual(client.complete_json("JSON", "测试"), {"reply":"成功"})
        requests = [json.loads(args.args[0].data) for args in call.call_args_list]
        self.assertEqual(requests[0], requests[1])
        self.assertNotIn("thinking", requests[0])

    def test_reasoning_only_responses_never_become_the_reply_and_retry_is_bounded(self):
        raw = {"choices":[{"finish_reason":"stop","message":{"content":"", "reasoning_content":"private-reasoning-text"}}]}
        with patch("wxbot_framework.llm.urlopen", side_effect=[io.BytesIO(json.dumps(raw).encode()),io.BytesIO(json.dumps(raw).encode())]) as call:
            with self.assertRaisesRegex(LLMError,"未取得最终答案") as caught:
                self.client.complete_json("JSON", "测试")
        self.assertEqual(call.call_count, 2)
        self.assertNotIn("private-reasoning-text", str(caught.exception))

    def test_refusal_filter_tool_call_and_truncation_do_not_retry(self):
        cases = [("content_filter",{}), ("stop",{"refusal":"private-provider-detail"}),
                 ("tool_calls",{}), ("stop",{"tool_calls":[{"id":"test"}]}), ("length",{})]
        for finish, fields in cases:
            raw = {"choices":[{"finish_reason":finish,"message":{"content":"",**fields}}]}
            with self.subTest(finish=finish,fields=fields), patch("wxbot_framework.llm.urlopen",return_value=io.BytesIO(json.dumps(raw).encode())) as call:
                with self.assertRaises(LLMError) as caught:
                    self.client.complete_json("JSON", "测试")
                self.assertEqual(call.call_count, 1)
                self.assertNotIn("private-provider-detail", str(caught.exception))

    def test_extension_cannot_override_protocol_or_authentication_fields(self):
        for key in ("model", "messages", "stream", "response_format", "max_tokens", "max_completion_tokens", "api_key", "Authorization"):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "不能覆盖"):
                LLMClient({**self.options, "extra_body": {key: "forbidden"}})

    def test_invalid_extensions_and_compatibility_options(self):
        for change in ({"json_mode": "false"}, {"token_limit_parameter": "max_output_tokens"},
                       {"extra_body": []}, {"extra_body": None}, {"extra_body": {1: True}},
                       {"extra_body": {"temperature": float("nan")}},
                       {"extra_body": {"value": object()}}, {"extra_body": {"value": "a" * 16384}}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                LLMClient({**self.options, **change})

    def test_local_http_is_allowed_only_for_loopback_hosts(self):
        for url in ("http://localhost:11434/v1", "http://127.0.0.1:1234/v1", "http://[::1]:8080/v1"):
            with self.subTest(url=url):
                self.assertEqual(LLMClient({**self.options, "base_url": url}).endpoint, url + "/chat/completions")
        for url in ("http://192.168.1.2:8000/v1", "http://localhost.example.test/v1", "https://host:bad/v1",
                    "https://host:0/v1", "https://host/v1?token=private", "https://host/v1#fragment", "https://host/\n/v1"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                LLMClient({**self.options, "base_url": url})

    def test_http_errors_are_not_retried_or_returned_with_private_payloads(self):
        error = HTTPError("https://example.test", 400, "private prompt or credential", {}, None)
        with patch("wxbot_framework.llm.urlopen", side_effect=error) as call:
            with self.assertRaises(LLMError) as caught:
                self.client.complete_json("private prompt", "private content")
        self.assertEqual(call.call_count, 1)
        self.assertNotIn("private", str(caught.exception))

    def test_config_from_utf8_bom_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "llm.json"
            path.write_text(json.dumps(self.options), encoding="utf-8-sig")
            self.assertEqual(LLMClient.from_file(path).model, "deepseek-flash")

    def test_invalid_configuration(self):
        for change in ({"api_key": ""}, {"model": 1}, {"base_url": None},
                       {"base_url": "http://example.com"}, {"base_url": "https://user:pass@example.com"},
                       {"timeout_seconds": True}, {"timeout_seconds": float("nan")},
                       {"max_tokens": 1}, {"max_tokens": True}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                LLMClient({**self.options, **change})

    def test_server_and_connection_errors_hide_sensitive_details(self):
        for error in (HTTPError("https://example.com", 401, "dummy-test-key", {}, None),
                      URLError("dummy-test-key"), TimeoutError("dummy-test-key")):
            with self.subTest(error=type(error).__name__), patch("wxbot_framework.llm.urlopen", side_effect=error):
                with self.assertRaises(LLMError) as caught:
                    self.client.complete_json("JSON", "测试")
                self.assertNotIn("dummy-test-key", str(caught.exception))

    def test_malformed_or_truncated_model_results(self):
        for content in ("", "not json", "[]", None):
            with self.subTest(content=content), patch("wxbot_framework.llm.urlopen", return_value=self.response(content)):
                with self.assertRaises(LLMError):
                    self.client.complete_json("JSON", "测试")
        with patch("wxbot_framework.llm.urlopen", return_value=self.response("{}", finish_reason="length")):
            with self.assertRaisesRegex(LLMError, "截断"):
                self.client.complete_json("JSON", "测试")
        with patch("wxbot_framework.llm.urlopen", return_value=io.BytesIO(b'{"choices":[null]}')):
            with self.assertRaises(LLMError):
                self.client.complete_json("JSON", "测试")


class JudgeTests(unittest.TestCase):
    def setUp(self):
        self.client = JSONClient({"is_idiom": True, "reason": "常见成语", "next_idiom": "发扬光大"})
        self.judge = IdiomJudge(self.client)

    def test_local_rejections_do_not_call_model(self):
        for previous, word, used in (("一心一意", "不是四字成语", ()),
                                     ("一心一意", "异想天开", ()),
                                     ("一心一意", "意气风发", ("意气风发",))):
            self.assertFalse(self.judge.evaluate(previous, word, used).accepted)
        self.assertEqual(self.client.calls, [])

    def test_success_sends_only_game_words(self):
        result = self.judge.evaluate("一心一意", "意气风发", ("一心一意",))
        self.assertTrue(result.accepted)
        self.assertEqual(result.next_idiom, "发扬光大")
        self.assertEqual(json.loads(self.client.calls[0][1]),
                         {"previous": "一心一意", "candidate": "意气风发", "used": ["一心一意", "意气风发"]})

    def test_model_can_reject_fake_four_character_word(self):
        self.client.result = {"is_idiom": False, "reason": "不是成语", "next_idiom": ""}
        result = self.judge.evaluate("一心一意", "意意意意", ("一心一意",))
        self.assertFalse(result.accepted)

    def test_invalid_model_next_word_never_advances(self):
        for word in ("假的", "一心一意", "发扬光大", "意气风发"):
            self.client.result["next_idiom"] = word
            with self.subTest(word=word), self.assertRaises(LLMError):
                self.judge.evaluate("一心一意", "意气风发", ("一心一意", "发扬光大"))

    def test_model_boolean_is_strict(self):
        self.client.result["is_idiom"] = "true"
        with self.assertRaises(LLMError):
            self.judge.evaluate("一心一意", "意气风发", ())

    def test_opening_shape(self):
        self.client.result = {"idiom": "一心一意"}
        self.assertEqual(self.judge.opening(), "一心一意")
        self.client.result = {"idiom": "假词"}
        with self.assertRaises(LLMError):
            self.judge.opening()
