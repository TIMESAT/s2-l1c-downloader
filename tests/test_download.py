from __future__ import annotations

from s2vomb.catalogue import CatalogueStore
from s2vomb.download import (
    download_products,
    product_safe_directory,
    product_target,
    verify_local_file,
    verify_safe_directory,
)


class NoAuth:
    def get_token(self):
        raise AssertionError("authentication should not be used for an already complete file")


class StaticToken:
    def get_token(self):
        return "test-token"

    def invalidate(self):
        raise AssertionError("token should not be invalidated")


class StreamResponse:
    def __init__(self, body, start, total):
        self.body = body
        self.status_code = 206
        self.headers = {
            "Content-Range": f"bytes {start}-{total - 1}/{total}",
            "Content-Length": str(len(body)),
        }

    def iter_content(self, chunk_size):
        yield self.body

    def close(self):
        pass


class ResumeSession:
    def __init__(self, body, start, total):
        self.body = body
        self.start = start
        self.total = total
        self.headers = {}
        self.calls = []

    def get(self, url, headers, stream, timeout, allow_redirects):
        self.calls.append((url, headers))
        assert headers["Range"] == f"bytes={self.start}-"
        assert headers["Authorization"] == "Bearer test-token"
        return StreamResponse(self.body, self.start, self.total)

    def close(self):
        pass


def test_already_downloaded_detection_skips_authentication(app_config, zipped_record):
    record, payload = zipped_record
    store = CatalogueStore(app_config)
    store.write_csv([record])
    target = product_target(app_config, record)
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)

    result = download_products(app_config, [record], store, token_manager=NoAuth())

    assert result.already_present == 1
    assert result.downloaded == 0
    saved = store.read()[0]
    assert saved.download_status == "completed"
    assert saved.checksum_verified is True
    assert verify_local_file(target, saved).valid


def test_extracted_safe_detection_skips_authentication(app_config, zipped_record):
    record, _ = zipped_record
    store = CatalogueStore(app_config)
    store.write_csv([record])
    safe = product_safe_directory(app_config, record)
    image_data = safe / "GRANULE" / "L1C_TEST" / "IMG_DATA"
    image_data.mkdir(parents=True)
    (safe / "manifest.safe").write_text("manifest", encoding="utf-8")
    (safe / "MTD_MSIL1C.xml").write_text("<xml/>", encoding="utf-8")
    (image_data / "B01.jp2").write_bytes(b"jp2-data")

    result = download_products(app_config, [record], store, token_manager=NoAuth())

    assert result.already_present == 1
    assert result.downloaded == 0
    saved = store.read()[0]
    assert saved.download_status == "existing-safe"
    assert saved.local_path == str(safe)
    assert saved.checksum_verified is False
    assert verify_safe_directory(safe).valid
    assert not product_target(app_config, record).exists()


def test_resumes_partial_download_and_checkpoints_catalogue(app_config, zipped_record):
    record, payload = zipped_record
    store = CatalogueStore(app_config)
    store.write_csv([record])
    target = product_target(app_config, record)
    target.parent.mkdir(parents=True)
    split = len(payload) // 2
    target.with_name(f"{target.name}.part").write_bytes(payload[:split])
    session = ResumeSession(payload[split:], split, len(payload))

    result = download_products(
        app_config,
        [record],
        store,
        token_manager=StaticToken(),
        session_factory=lambda: session,
    )

    assert result.downloaded == 1
    assert target.read_bytes() == payload
    assert not target.with_name(f"{target.name}.part").exists()
    saved = store.read()[0]
    assert saved.download_status == "completed"
    assert saved.checksum_verified is True
    assert saved.downloaded_bytes == len(payload)


def test_dry_run_does_not_create_archive(app_config, product_record):
    store = CatalogueStore(app_config)
    store.write_csv([product_record])
    result = download_products(app_config, [product_record], store, dry_run=True)
    assert result.outcomes[0].status == "planned"
    assert not product_target(app_config, product_record).exists()
