"""Remove credential fields and explicitly supplied secret values before persistence."""

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def sensitive_key(key: str) -> bool:
    compact = re.sub(r"[^a-z0-9]", "", key.lower())
    return compact in {
        "authorization",
        "proxyauthorization",
        "password",
        "secret",
        "accesstoken",
        "refreshtoken",
        "apikey",
        "token",
        "clientsecret",
    } or compact.endswith("apikey")


class Redactor:
    def __init__(self, secrets: tuple[str, ...] = ()) -> None:
        self.secrets = sorted((s for s in secrets if s), key=len, reverse=True)

    def clean(self, value: object) -> tuple[object, tuple[str, ...]]:
        redacted: list[str] = []

        def walk(item: object, path: str) -> object:
            if isinstance(item, dict):
                result = {}
                for key, child in item.items():
                    child_path = f"{path}.{key}" if path else str(key)
                    if sensitive_key(str(key)):
                        result[key] = "[REDACTED]"
                        redacted.append(child_path)
                    else:
                        result[key] = walk(child, child_path)
                return result
            if isinstance(item, (list, tuple)):
                return [walk(child, f"{path}[{i}]") for i, child in enumerate(item)]
            if isinstance(item, str):
                cleaned = item
                for secret in self.secrets:
                    cleaned = cleaned.replace(secret, "[REDACTED]")
                cleaned = re.sub(r"(?i)\bBearer\s+[^\s\"']+", "Bearer [REDACTED]", cleaned)
                if cleaned.startswith(("http://", "https://", "ws://", "wss://")):
                    url = urlsplit(cleaned)
                    query = parse_qsl(url.query, keep_blank_values=True)
                    if any(sensitive_key(k) for k, _ in query) or url.username:
                        host = url.netloc.rsplit("@", 1)[-1]
                        cleaned = urlunsplit(
                            (
                                url.scheme,
                                host,
                                url.path,
                                urlencode(
                                    [(k, "[REDACTED]" if sensitive_key(k) else v) for k, v in query]
                                ),
                                url.fragment,
                            )
                        )
                if cleaned != item:
                    redacted.append(path)
                return cleaned
            return item

        return walk(value, ""), tuple(redacted)
