"""
Offline checks for the v14.190 batchRunV2 transport (no network): intercept the HTTP layer and
assert the request shape the app produces — GET /api/batch/batchRunV2 with ONE outer param
`batch_method_feed=["method=<dotted>&k=v&…", …]`, signed with key=None (the outer path is on the
no-secretKey whitelist even when a subrequest's standalone path is not).
"""
import os, sys, types
from urllib.parse import unquote
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bosscli.client import BossClient
from bosscli import yzwg

SECRET = "ab8d5d0665ef5d5bbba3699b14d1a825"
SESSION = {
    "t2": "T2TOKEN", "v": "14.190", "app_id": "1003", "uniqid": "uniq-123",
    "client_info": {"os": "Android", "uniqid": "uniq-123"}, "curidentity": "1",
    "secretKey": SECRET, "uid": 42,
}


def _capture():
    c = BossClient(dict(SESSION))
    calls = []

    def fake_http(method, url, *, data=None, headers=None, retries=2):
        calls.append({"method": method, "url": url, "data": data, "headers": headers})
        return types.SimpleNamespace(content=b'{"code":0,"zpData":{}}')

    c._http = fake_http
    return c, calls


def _strd(url: str) -> str:
    """The wire is strD + '&sp=' + quote(sp) + '&sig=' + sig + '&app_id=' + id; strD comes first."""
    return url.split("?", 1)[1].split("&sp=", 1)[0]


def _assert_sig(url: str, path: str, key):
    strD = _strd(url)
    sig = url.split("&sig=", 1)[1].split("&app_id=", 1)[0]
    expect = yzwg.native_signature((path + strD).encode(), key)
    assert sig == expect, f"{path}: sig mismatch (key={'secretKey' if key else 'None'})"
    return strD


def test_dotted_path():
    assert BossClient._dotted("/api/zpjob/job/local/data") == "zpjob.job.local.data"
    assert BossClient._dotted("zpitem/boss/search") == "zpitem.boss.search"


def test_boss_jobs_uses_batchrunv2_with_keynull():
    c, calls = _capture()
    c.boss_jobs()
    assert calls[-1]["method"] == "GET"
    assert "/api/batch/batchRunV2?" in calls[-1]["url"]
    strD = unquote(_assert_sig(calls[-1]["url"], "/api/batch/batchRunV2", None))
    # feed is one Java-URLEncoder'd array of quoted entries: ["method=zpjob.job.local.data"]
    assert 'batch_method_feed=["method=zpjob.job.local.data"]' in strD


def test_boss_account_batches_three_methods():
    c, calls = _capture()
    c.boss_account()
    strD = unquote(_assert_sig(calls[-1]["url"], "/api/batch/batchRunV2", None))
    for method in ("zpboss.app.boss.get.detail", "zpjob.job.local.data", "zpjob.job.sort.list"):
        assert "method=" + method in strD
    # entries are joined with ", " inside the array (ArrayList.toString)
    assert '",+"method=' in strD          # ", " joined; the space is Java-encoded to '+' 


def test_recommend_sends_app_param_names():
    c, calls = _capture()
    c.boss_recommend_geeks(777, page=2, page_size=15, sort_type=1)
    strD = unquote(_assert_sig(calls[-1]["url"], "/api/batch/batchRunV2", None))
    assert "method=zpjob.recommend.geek.list" in strD
    for kv in ("jobId=777", "page=2", "pageSize=15", "sortType=1", "coverScreenMemory=0"):
        assert kv in strD, f"missing {kv}"
    assert "filterParams" not in strD   # not sent unless requested


def test_recommend_filter_params_serialized_compact():
    c, calls = _capture()
    c.boss_recommend_geeks(1, filter_params={"salary": "407"})
    strD = unquote(_assert_sig(calls[-1]["url"], "/api/batch/batchRunV2", None))
    # inner values are Java-URLEncoder'd once more (as the app does for each feed param)
    assert 'filterParams=%7B%22salary%22%3A%22407%22%7D' in strD


def test_batch_v2_sub_extracts_by_dotted_name():
    envelope = {"code": 0, "zpData": {"zpjob.job.local.data": {"code": 0, "zpData": {"jobList": [1]}}}}
    sub = BossClient.batch_v2_sub(envelope, "/api/zpjob/job/local/data")
    assert sub["zpData"]["jobList"] == [1]
    assert BossClient.batch_v2_sub(envelope, "/api/nope") == {}
    assert BossClient.parse_boss_jobs(envelope) == [1]
