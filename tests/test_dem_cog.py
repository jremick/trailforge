"""Offline transport and GDAL checks for bounded DEA elevation reads."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from trailforge.ingestion.connectors import _cog_ranges, dem
from trailforge.ingestion.connectors.base import BBox


def _range_provider(data: bytes, reads: list[tuple[int, int]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.headers["Accept-Encoding"] == "identity"
        match = re.fullmatch(r"bytes=(\d+)-(\d+)", request.headers["Range"])
        assert match
        start, end = map(int, match.groups())
        end = min(end, len(data) - 1)
        reads.append((start, end))
        return httpx.Response(
            206, headers={"Content-Range": f"bytes {start}-{end}/{len(data)}"},
            content=data[start : end + 1],
        )

    return httpx.MockTransport(handler)


@pytest.mark.parametrize("limit", ["bytes", "requests"])
def test_cog_reader_shares_budget_across_ranges_and_metadata(
    monkeypatch: pytest.MonkeyPatch, limit: str
) -> None:
    if limit == "bytes":
        monkeypatch.setattr(_cog_ranges, "_MAX_READ_BYTES", 6)
    else:
        monkeypatch.setattr(_cog_ranges, "_MAX_READ_REQUESTS", 2)
    reads: list[tuple[int, int]] = []
    data = b"a mock COG byte stream"
    with (
        httpx.Client(transport=_range_provider(data, reads)) as provider,
        _cog_ranges.cog_range_path("https://fixture.test/elevation.tif", provider, timeout_s=3)
        as operation,
        httpx.Client(trust_env=False) as local,
    ):
        url = operation.path.removeprefix("/vsicurl/")
        assert httpx.URL(url).host == "127.0.0.1"
        assert local.get(f"{url}.aux.xml").status_code == 404
        head_resp = local.head(url)
        assert head_resp.status_code == 200
        assert head_resp.headers["Content-Length"] == str(len(data))
        assert "Content-Range" not in head_resp.headers
        assert local.head(url).status_code == 200
        first = local.get(url, headers={"Range": "bytes=2-5"})
        assert first.status_code == 206 and first.content == data[2:6]
        assert first.headers["Content-Range"] == f"bytes 2-5/{len(data)}"
        assert local.get(url, headers={"Range": "bytes=6-9"}).status_code == 502

    assert reads == [(0, 0), (2, 5)]
    assert operation.failed.is_set()


@pytest.mark.parametrize("bad_url", ["file:///etc/passwd", "ftp://fixture.test/dem.tif", "https://"])
def test_cog_reader_rejects_non_http_or_hostless_source_urls(bad_url: str) -> None:
    with (
        httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(200))) as provider,
        pytest.raises(ValueError, match="direct HTTP\\(S\\) URL"),
        _cog_ranges.cog_range_path(bad_url, provider, timeout_s=3),
    ):
        pass


@pytest.mark.parametrize("provider_status", [200, 302])
def test_cog_reader_declines_full_responses_and_redirects_without_reading_them(
    provider_status: int,
) -> None:
    class UnreadBody(httpx.SyncByteStream):
        def __iter__(self):
            pytest.fail("Provider response body was read after invalid range headers")

    requests: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url)
        return httpx.Response(
            provider_status, headers={"Location": "https://other.test/elsewhere.tif"},
            stream=UnreadBody(),
        )

    source = "https://fixture.test/elevation.tif"
    with (
        httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as provider,
        _cog_ranges.cog_range_path(source, provider, timeout_s=3) as operation,
        httpx.Client(trust_env=False) as local,
    ):
        url = operation.path.removeprefix("/vsicurl/")
        assert local.head(url).status_code == 502
    assert requests == [httpx.URL(source)]


def test_srtm_uses_configured_cog_and_removes_failed_output_before_wcs(
    monkeypatch: pytest.MonkeyPatch, fixtures_dir: Path, tmp_path: Path,
) -> None:
    source = "https://fixture.test/custom-hydro.tif?version=12"
    monkeypatch.setenv("TRAILFORGE_DEA_SRTM_HYDRO_URL", source)
    monkeypatch.setattr(_cog_ranges, "_MAX_READ_BYTES", 3)
    monkeypatch.setattr(dem.shutil, "which", lambda _: "gdal_translate")
    upstream_urls: list[httpx.URL] = []

    def provider(request: httpx.Request) -> httpx.Response:
        upstream_urls.append(request.url)
        return httpx.Response(206, headers={"Content-Range": "bytes 0-0/10"}, content=b"x")

    def translate(args, *, env, **_kwargs):
        assert env["no_proxy"] == "127.0.0.1"
        assert env["GDAL_HTTP_MULTIRANGE"] == "SERIAL"
        url = args[-2].removeprefix("/vsicurl/")
        assert args[-2].startswith("/vsicurl/http://127.0.0.1:")
        with httpx.Client(trust_env=False) as local:
            assert local.head(url).status_code == 200
            assert local.get(url, headers={"Range": "bytes=0-3"}).status_code == 502
        Path(args[-1]).write_bytes(b"incomplete raster")
        return subprocess.CompletedProcess(args, 0)

    def backup(_bbox, path: Path) -> bool:
        assert not path.exists()
        shutil.copyfile(fixtures_dir / "dem_alps_corridor.tif", path)
        return True

    monkeypatch.setattr(dem.subprocess, "run", translate)
    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        connector = dem.AUElvisDEMConnector(client=client, prefer_5m=False, srtm_product="hydro")
        monkeypatch.setattr(connector, "_fetch_srtm_1s_wcs_backup", backup)
        bbox = BBox(min_x=148.12, min_y=-36.61, max_x=148.14, max_y=-36.59)
        out, tier = connector.fetch_dem(bbox, tmp_path)

    assert out.is_file() and tier == "srtm-1s"
    assert upstream_urls == [httpx.URL(source)]


def test_real_gdal_reads_a_small_window_without_downloading_the_full_cog(tmp_path: Path) -> None:
    rasterio = pytest.importorskip("rasterio")
    import numpy as np
    from rasterio.shutil import copy
    from rasterio.transform import from_origin
    from rasterio.windows import Window

    raster_path = tmp_path / "source.tif"
    cog_path = tmp_path / "source-cog.tif"
    data = np.arange(1024 * 1024, dtype=np.uint32).reshape((1024, 1024))
    with rasterio.open(
        raster_path, "w", driver="GTiff", width=1024, height=1024, count=1,
        dtype=data.dtype, crs="EPSG:4326", transform=from_origin(148, -36, 1 / 3600, 1 / 3600),
    ) as dataset:
        dataset.write(data, 1)
    copy(raster_path, cog_path, driver="COG", BLOCKSIZE=128, COMPRESS="NONE")
    cog_bytes = cog_path.read_bytes()
    reads: list[tuple[int, int]] = []

    with (
        httpx.Client(transport=_range_provider(cog_bytes, reads)) as provider,
        _cog_ranges.cog_range_path("https://fixture.test/elevation.tif", provider, timeout_s=5)
        as operation,
        rasterio.Env(
            GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", GDAL_HTTP_MULTIRANGE="SERIAL",
            CPL_VSIL_CURL_USE_HEAD="YES", NO_PROXY="127.0.0.1", no_proxy="127.0.0.1",
        ),
        rasterio.open(operation.path) as dataset,
    ):
        window = dataset.read(1, window=Window(35, 40, 24, 24))

    np.testing.assert_array_equal(window, data[40:64, 35:59])
    assert 1 < sum(end - start + 1 for start, end in reads) < len(cog_bytes) // 2
    assert not operation.failed.is_set()
