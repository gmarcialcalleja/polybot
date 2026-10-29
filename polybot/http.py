"""Bounded public-data requests and a local, disposable response cache."""

import hashlib
import json
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class SourceError(RuntimeError):
    pass


class Http:
    def __init__(self, cache_dir=Path('.cache')):
        self.cache_dir = Path(cache_dir)
        self.session = requests.Session()
        retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504],
                      allowed_methods=['GET'], respect_retry_after_header=False)
        self.session.mount('https://', HTTPAdapter(max_retries=retry))

    def get(self, url, params=None, ttl=0):
        key = hashlib.sha256((url + json.dumps(params, sort_keys=True)).encode()).hexdigest()
        path = self.cache_dir / (key + '.txt')
        if ttl and path.exists() and time.time() - path.stat().st_mtime < ttl:
            return path.read_text(encoding='utf-8')
        try:
            response = self.session.get(url, params=params, timeout=(10, 35))
            response.raise_for_status()
        except requests.RequestException as exc:
            # Never print requests exceptions: URLs can contain credentials.
            status = getattr(getattr(exc, 'response', None), 'status_code', 'connection')
            raise SourceError(f'{urlparse(url).hostname}: request failed ({status})') from None
        if ttl:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(response.text, encoding='utf-8')
        return response.text

    def json(self, url, params=None, ttl=0):
        try:
            return json.loads(self.get(url, params, ttl))
        except (ValueError, TypeError):
            raise SourceError(f'{urlparse(url).hostname}: invalid JSON') from None
