import os

from . import config

_gcs_client = None


def _bucket():
    global _gcs_client
    if _gcs_client is None:
        from google.cloud import storage
        _gcs_client = storage.Client()
    return _gcs_client.bucket(config.GCS_BUCKET_NAME)


def read_text(relative_path):
    """Returns the text content at relative_path, or None if it doesn't
    exist. Reads from the GCS bucket when config.GCS_BUCKET_NAME is set,
    otherwise from local disk under the trading/ directory."""
    if config.GCS_BUCKET_NAME:
        blob = _bucket().blob(relative_path)
        if not blob.exists():
            return None
        return blob.download_as_text()

    path = os.path.join(config.TRADING_DIR, relative_path)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return f.read()


def write_text(relative_path, text):
    if config.GCS_BUCKET_NAME:
        blob = _bucket().blob(relative_path)
        blob.upload_from_string(text, content_type="text/plain; charset=utf-8")
        return

    path = os.path.join(config.TRADING_DIR, relative_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def read_bytes(relative_path):
    """Binary counterpart to read_text, for images and other non-text
    blobs (e.g. generated book illustrations)."""
    if config.GCS_BUCKET_NAME:
        blob = _bucket().blob(relative_path)
        if not blob.exists():
            return None
        return blob.download_as_bytes()

    path = os.path.join(config.TRADING_DIR, relative_path)
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return f.read()


def write_bytes(relative_path, data, content_type="application/octet-stream"):
    if config.GCS_BUCKET_NAME:
        blob = _bucket().blob(relative_path)
        blob.upload_from_string(data, content_type=content_type)
        return

    path = os.path.join(config.TRADING_DIR, relative_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def list_prefix(prefix):
    """Returns relative paths of every blob/file directly under `prefix`
    (a directory-style prefix, e.g. "shyfly/books/"). Non-recursive is not
    guaranteed for the local-disk fallback (os.walk descends), but callers
    in this codebase only ever use flat, one-level directories."""
    if config.GCS_BUCKET_NAME:
        return [blob.name for blob in _bucket().list_blobs(prefix=prefix)]

    dir_path = os.path.join(config.TRADING_DIR, prefix)
    if not os.path.isdir(dir_path):
        return []
    root = config.TRADING_DIR
    paths = []
    for dirpath, _dirnames, filenames in os.walk(dir_path):
        for name in filenames:
            full = os.path.join(dirpath, name)
            paths.append(os.path.relpath(full, root).replace(os.sep, "/"))
    return paths
