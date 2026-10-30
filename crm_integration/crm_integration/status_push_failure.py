"""Bounded, credential-redacted evidence carried across a financial-job rollback."""

import re
from itertools import chain, islice
from urllib.parse import parse_qsl, quote, quote_plus, unquote, urlsplit, urlunsplit


REDACTED = "[REDACTED]"
SENSITIVE_KEY = re.compile(r"authorization|api.?key|token|secret|password|passwd|cookie|credential", re.I)
REQUEST_FIELDS = (
	"sourceSystem", "externalStatus", "externalOrderId", "erpOrderId", "crmApprovalRound", "traceId",
)
SUMMARY_FIELDS = ("errorCode", "code", "traceId", "trace_id", "success", "status", "message", "errors")
CREDENTIAL_VALUE = re.compile(
	r"(?i)([\w-]*(?:api[_-]?key|token|secret|password|passwd|cookie|authorization)[\w-]*[\"']?\s*[:=]\s*)"
	r'''(?:"(?:\\.|[^"\\])*(?:"|\\?\Z)|'(?:\\.|[^'\\])*(?:'|\\?\Z)|(?!["'])[^,\r\n;<>]+)'''
)


def safe_url(value):
	try:
		parts = urlsplit(value)
		host = parts.hostname or ""
		if ":" in host:
			host = f"[{host}]"
		if parts.port:
			host += f":{parts.port}"
		return urlunsplit((parts.scheme, host, parts.path, "", ""))
	except ValueError:
		return "[invalid URL]"


def redact_evidence(value, secrets=()):
	"""Keep business errors useful without retaining auth fields or unbounded bodies."""
	secrets = {variant for secret in secrets if isinstance(secret, str) and secret
		for variant in (secret, quote(secret, safe=""), quote_plus(secret))}
	secrets = sorted(secrets, key=len, reverse=True)
	remaining_nodes, remaining_chars = 128, 16000

	def clean_text(text):
		for secret in secrets:
			text = text.replace(secret, REDACTED)
		text = re.sub(r"https?://[^\s\"<>]+", lambda match: safe_url(match[0]), text)
		text = CREDENTIAL_VALUE.sub(lambda match: match[1] + REDACTED, text)
		return re.sub(r"(?i)\b(?:Bearer|Basic|token)\s+[^\s,;\"<>]+", REDACTED, text)

	def clean(item, depth=0):
		nonlocal remaining_nodes, remaining_chars
		remaining_nodes -= 1
		if remaining_nodes < 0 or depth > 6:
			return "[truncated]"
		if isinstance(item, dict):
			result = {}
			# Preserve diagnosis before spending the shared budget on data or long lists.
			items = chain(((key, item[key]) for key in SUMMARY_FIELDS if key in item),
				((key, child) for key, child in item.items() if key not in SUMMARY_FIELDS))
			for index, (key, child) in enumerate(islice(items, 65)):
				if index == 64 or remaining_nodes <= 0 or remaining_chars <= 0:
					result["_truncated"] = True
					break
				clean_key = clean_text(str(key))
				if len(clean_key) > 128:
					clean_key = clean_key[:128] + "[truncated]"
				if len(clean_key) > remaining_chars:
					result["_truncated"] = True
					break
				remaining_nodes -= 1
				remaining_chars -= len(clean_key)
				# Compute the key before its child can exhaust either budget.
				result[clean_key] = REDACTED if SENSITIVE_KEY.search(str(key)) else clean(child, depth + 1)
			return result
		if isinstance(item, (list, tuple)):
			result = []
			for child in item[:64]:
				if remaining_nodes <= 0 or remaining_chars <= 0:
					result.append("[truncated]")
					break
				result.append(clean(child, depth + 1))
			return result
		if item is None or isinstance(item, (bool, int, float)):
			return item
		text = clean_text(str(item))
		limit = max(0, min(4000, remaining_chars))
		remaining_chars -= min(len(text), limit)
		return text[:limit] + ("[truncated]" if len(text) > limit else "")

	return clean(value)


def status_request_evidence(url, payload, api_key):
	# Never copy request headers, free-form remarks or attachments into log evidence.
	return {
		"request_url": safe_url(url),
		"request_payload": redact_evidence({key: payload.get(key) for key in REQUEST_FIELDS}, known_secrets(url, api_key)),
	}


def known_secrets(url, api_key):
	try:
		parts = urlsplit(url)
		return (api_key, parts.username, parts.password,
			unquote(parts.username or ""), unquote(parts.password or ""),
			*(value for key, value in parse_qsl(parts.query) if SENSITIVE_KEY.search(key)))
	except ValueError:
		return (api_key,)


def status_failure_evidence(*, order_name, event, url, payload, response_payload, response, error, api_key):
	secrets = known_secrets(url, api_key)
	return {
		"direction": "Outbound", "event": event, "status": "Failed", "source": "ERPNext",
		"reference_doctype": "Sales Order", "reference_name": order_name,
		**status_request_evidence(url, payload, api_key),
		"response_payload": redact_evidence(response_payload, secrets),
		"http_status_code": getattr(response, "status_code", None) or 0,
		"trace_id": payload["traceId"], "external_status": payload["externalStatus"],
		"error_message": redact_evidence(f"{type(error).__name__}: {error}", secrets),
	}
