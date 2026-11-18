"""
CLI-level checks for the recruiter commands and the two request-shape rules they rely on —
no network, no session file: `cli._client` is replaced by a stub that returns canned envelopes.
Covers: jobs/rcmd rendering + exit codes, `api` param validation, the version-consistency guard,
and call_plain keeping empty-string params on the wire.
"""
import os, sys, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from bosscli import cli
from bosscli.client import CARDLIST_PATH, BossClient, _check_version


class Fake:
    """Stand-in for BossClient: records the call, returns one canned envelope."""

    def __init__(self, resp=None, curidentity="1"):
        self.resp = resp if resp is not None else {"code": 0, "zpData": {}}
        self.s = {"curidentity": curidentity, "t2": "T2"}
        self.kw = None
        self.subs = None

    def boss_jobs(self, **kw): return self.resp
    def boss_contacts(self, **kw): return self.resp
    def boss_account(self, full=False): return self.resp
    def boss_recommend_geeks(self, job_id, **kw): self.kw = dict(kw, job_id=job_id); return self.resp
    def search(self, *a, **kw): return self.resp
    def call_batch_v2(self, subs, key=None): self.subs = subs; return self.resp
    def call_plain(self, *a, **k): return self.resp
    def _secret(self): return None


def _sub(path, sub):
    return {"code": 0, "zpData": {BossClient._dotted(path): sub}}


def _run(monkeypatch, fake, argv):
    monkeypatch.setattr(cli, "_client", lambda args: fake)
    return cli.main(argv)


def test_jobs_renders_rows(monkeypatch, capsys):
    jobs = [{"jobId": 1, "positionName": "后端", "salaryDesc": "10-20K", "jobStatus": 1,
             "jobAuditStatus": 3, "encryptJobId": "E1", "experienceName": "1-3年",
             "degreeName": "本科"}]
    fake = Fake(_sub(BossClient.BOSS_JOB_LIST_PATH, {"code": 0, "zpData": {"jobList": jobs}}))
    _run(monkeypatch, fake, ["jobs"])
    out = capsys.readouterr().out
    assert "我的职位 1 个" in out
    assert "1 | 后端 | 10-20K | status=1/audit=3" in out


def test_jobs_inner_error_exits_2_with_identity_hint(monkeypatch, capsys):
    fake = Fake(_sub(BossClient.BOSS_JOB_LIST_PATH,
                     {"code": 24, "message": "请切换身份后再试"}))
    with pytest.raises(SystemExit) as e:
        _run(monkeypatch, fake, ["jobs"])
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "code=24" in err and "招聘端(boss)" in err


def test_rcmd_offline_exits_2(monkeypatch, capsys):
    fake = Fake(_sub(BossClient.BOSS_RCMD_PATH, {"code": 200001, "message": "当前职位不在线"}))
    with pytest.raises(SystemExit) as e:
        _run(monkeypatch, fake, ["rcmd", "573470974"])
    assert e.value.code == 2
    assert "不在线" in capsys.readouterr().err


def test_rcmd_passes_page_args(monkeypatch):
    fake = Fake(_sub(BossClient.BOSS_RCMD_PATH, {"code": 0, "zpData": {"geekList": []}}))
    _run(monkeypatch, fake, ["rcmd", "42", "--page", "3", "--page-size", "30", "--sort-type", "1"])
    assert fake.kw["job_id"] == "42"
    assert (fake.kw["page"], fake.kw["page_size"], fake.kw["sort_type"]) == (3, 30, 1)


def test_api_rejects_param_without_equals(monkeypatch):
    fake = Fake()
    with pytest.raises(SystemExit) as e:
        _run(monkeypatch, fake, ["api", "/api/x", "novalue"])
    assert e.value.code not in (0, None)


def test_api_batch_wraps_path(monkeypatch, capsys):
    fake = Fake({"code": 0, "zpData": {}})
    _run(monkeypatch, fake, ["api", "--batch", "/api/zpitem/boss/search", "page=2"])
    assert fake.subs == [{"path": "/api/zpitem/boss/search", "params": {"page": "2"}}]
    assert '"code": 0' in capsys.readouterr().out


def test_me_full_sends_all_sixteen_subs(monkeypatch, capsys):
    fake = Fake({"code": 0, "zpData": {}})
    _run(monkeypatch, fake, ["me", "--full"])
    assert fake.subs is None, "me --full must use boss_account(full=True), not call_batch_v2 directly"
    assert "账户整批 16 个子请求" in capsys.readouterr().out


def test_search_all_cities_failed_exits_2(monkeypatch, capsys):
    fake = Fake({"code": 0, "zpData": {CARDLIST_PATH:
                                       {"code": 24, "message": "请切换身份后再试"}}})
    with pytest.raises(SystemExit) as e:
        _run(monkeypatch, fake, ["search", "Python", "--city", "杭州"])
    assert e.value.code == 2


def test_check_version_accepts_consistent_session():
    _check_version({"v": "14.190",
                    "user_agent": "NetType/wifi Screen/1080X2209 BossZhipin/14.190 Android 29",
                    "client_info": {"version_flag": "1419010"}})


@pytest.mark.parametrize("session", [
    {"v": "14.190", "user_agent": "NetType/wifi BossZhipin/14.050 Android 29"},
    {"v": "14.190", "client_info": {"version_flag": "1405010"}},
    {"v": "14.050", "client_info": {"version_flag": "1419010"}},
])
def test_check_version_rejects_stitched_profile(session):
    with pytest.raises(ValueError):
        _check_version(session)
    with pytest.raises(ValueError):
        BossClient(dict(session, t2="T2", uniqid="u", client_info=session.get("client_info", {"uniqid": "u"})))


def test_call_plain_keeps_empty_string_param():
    c = BossClient({"t2": "T2", "uniqid": "u", "client_info": {"uniqid": "u"}, "v": "14.190"})
    sent = []

    def fake_http(method, url, *, data=None, headers=None, retries=2):
        sent.append(url)
        return types.SimpleNamespace(content=b'{"code":0,"zpData":{}}')

    c._http = fake_http
    c.call_plain("GET", "/api/zpitem/boss/search", {"securityId": "", "page": 1})
    url = sent[-1]
    assert "securityId=&" in url or url.endswith("securityId=")
    assert "page=1" in url


def test_greet_dry_run_never_sends(monkeypatch, capsys):
    fake = Fake()

    def boom(*a, **k):
        raise AssertionError("must not send without --confirm")

    fake.boss_add_friend = boom
    _run(monkeypatch, fake, ["greet", "SEC", "--job-id", "1"])
    out = capsys.readouterr().out
    assert "dry-run" in out and "SEC" in out and "entrance" in out


def test_greet_confirm_sends_with_card_params(monkeypatch, capsys):
    fake = Fake({"code": 0, "zpData": {"relation": {"name": "张三"}}})
    seen = {}

    def add(security_id, **kw):
        seen.update(kw, security_id=security_id)
        return fake.resp

    fake.boss_add_friend = add
    _run(monkeypatch, fake, ["greet", "SEC", "--job-id", "7", "--friend-id", "88", "--confirm"])
    assert (seen["security_id"], seen["job_id"], seen["friend_id"]) == ("SEC", "7", "88")
    assert seen["entrance"] == 77
    assert "招呼已发出" in capsys.readouterr().out


def test_greet_falls_back_to_first_job(monkeypatch):
    env = _sub(BossClient.BOSS_JOB_LIST_PATH,
               {"code": 0, "zpData": {"jobList": [{"jobId": 42, "positionName": "后端"}]}})
    fake = Fake(env)
    seen = {}
    fake.boss_add_friend = lambda security_id, **kw: (seen.update(kw), {"code": 0})[1]
    _run(monkeypatch, fake, ["greet", "SEC", "--confirm"])
    assert seen["job_id"] == "42"


def test_greet_param_error_on_correct_side_has_no_identity_hint(monkeypatch, capsys):
    # code 71 on the RIGHT identity means bad/missing params — blaming the identity would mislead.
    fake = Fake()
    fake.boss_add_friend = lambda *a, **k: {"code": 71, "message": "参数格式异常，请您退出登录后重试."}
    with pytest.raises(SystemExit) as e:
        _run(monkeypatch, fake, ["greet", "SEC", "--job-id", "1", "--confirm"])
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "code=71" in err and "另一侧接口" not in err


def test_contacts_renders_cards_with_limit(monkeypatch, capsys):
    cards = [
        {"name": "邵杭", "expectPositionName": "数据标注", "expectSalary": "10-12K", "degree": "本科",
         "chatStatus": 0, "jobName": "AI产品开发助理", "friendId": 635638207, "securityId": "S1"},
        {"name": "周辰", "expectPositionName": "大模型算法", "expectSalary": "5-8K", "degree": "本科",
         "chatStatus": 0, "jobName": "AI产品开发助理", "friendId": 576846713, "securityId": "S2"},
    ]
    fake = Fake(_sub(BossClient.BOSS_CONTACTS_PATH,
                     {"code": 0, "zpData": {"identity": 1, "topCount": 2, "baseInfoList": cards}}))
    _run(monkeypatch, fake, ["contacts", "--limit", "1"])
    out = capsys.readouterr().out
    assert "已沟通牛人 2 个" in out
    assert "邵杭" in out and "securityId=S1" in out
    assert "周辰" not in out          # --limit applied
