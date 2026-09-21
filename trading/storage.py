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
