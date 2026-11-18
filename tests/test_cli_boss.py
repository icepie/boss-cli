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
    def boss_resume(self, security_id, **kw): return self.resp
    def boss_history(self, friend_id, **kw): return self.resp
    def boss_contacts_base(self, **kw): return self.resp
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


def _greet_env(sub):
    return _sub(BossClient.BOSS_ADD_FRIEND_PATH, sub)


def test_greet_confirm_sends_with_card_params(monkeypatch, capsys):
    fake = Fake(_greet_env({"code": 0, "zpData": {"relation": {"name": "张三"}}}))
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
    fake.boss_add_friend = lambda security_id, **kw: (seen.update(kw), _greet_env({"code": 0}))[1]
    _run(monkeypatch, fake, ["greet", "SEC", "--confirm"])
    assert seen["job_id"] == "42"


def test_greet_account_block_has_no_identity_hint(monkeypatch, capsys):
    # code 110 当前账号状态异常 on the RIGHT identity => account-level block, not an identity problem.
    fake = Fake()
    fake.boss_add_friend = lambda *a, **k: _greet_env(
        {"code": 110, "message": "当前账号状态异常，暂不支持此操作，如有疑问请联系客服"})
    with pytest.raises(SystemExit) as e:
        _run(monkeypatch, fake, ["greet", "SEC", "--job-id", "1", "--confirm"])
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "code=110" in err and "另一侧接口" not in err


def test_contacts_paged_renders_cards(monkeypatch, capsys):
    cards = [{"geekName": "周辰", "expectPositionName": "大模型算法", "salary": "5-8K",
              "geekDegree": "本科", "applyStatusDesc": "离职-随时到岗", "chatCardCount": 0,
              "actionDateDesc": "10月21日 19:21", "geekId": 576846713,
              "lid": "L.1", "securityId": "S" * 40}]
    fake = Fake(_sub(BossClient.BOSS_CONTACT_LIST_PATH,
                     {"code": 0, "zpData": {"total": 1000, "hasMore": True, "cardList": cards}}))
    _run(monkeypatch, fake, ["contacts"])
    out = capsys.readouterr().out
    assert "沟通列表 1 条" in out and "total=1000" in out
    assert "周辰" in out and "geekId=576846713" in out
    assert "securityId=" + "S" * 40 not in out      # long securityId is truncated in tables


def test_contacts_all_mode_uses_base_list(monkeypatch, capsys):
    cards = [{"name": "邵杭", "expectPositionName": "数据标注", "expectSalary": "10-12K",
              "chatStatus": 0, "jobName": "AI产品开发助理", "friendId": 635638207, "securityId": "S1"}]
    fake = Fake(_sub(BossClient.BOSS_CONTACTS_PATH,
                     {"code": 0, "zpData": {"identity": 1, "topCount": 1, "baseInfoList": cards}}))
    fake.boss_contacts_base = lambda: fake.resp
    _run(monkeypatch, fake, ["contacts", "--all"])
    out = capsys.readouterr().out
    assert "全部关系 1 条" in out and "邵杭" in out


def test_resume_renders_highlight_fields(monkeypatch, capsys):
    zd = {"geekBase4Vip": {"name": "周辰", "gender": 1, "ageDesc": "23岁", "workYearsDesc": "1年",
                           "degreeCategory": "本科", "applyStatusContent": "离职-随时到岗"},
          "geekExpPos4VipList": [{"positionName": "大模型算法", "salaryDesc": "5-8K",
                                  "locationName": "长沙"}],
          "geekWorkExp4VipList": [{"company": "玄甲公司", "positionName": "Java",
                                   "startDate": "20251001", "endDate": "20251201"}],
          "geekEduExp4VipList": [{"school": {"content": "********", "indexList": [{"start": 0, "end": 7}]},
                                  "startDate": "20220101", "endDate": "20260101"}]}
    fake = Fake(_sub(BossClient.BOSS_RESUME_PATH, {"code": 0, "zpData": zd}))
    _run(monkeypatch, fake, ["resume", "SEC"])
    out = capsys.readouterr().out
    assert "周辰" in out and "大模型算法 | 5-8K | 长沙" in out
    assert "玄甲公司 · Java · 20251001-20251201" in out
    assert "********" in out and "'content'" not in out      # highlight object unwrapped


def test_history_text_extraction(monkeypatch, capsys):
    import base64
    text = "您好 我是软件工程 2026 届本科生"
    blob = base64.b64encode(b"\x0a\x32" + text.encode()).decode()
    fake = Fake(_sub(BossClient.BOSS_HISTORY_PATH,
                     {"code": 0, "zpData": {"hasMore": False, "minMsgId": 1, "messages": [blob]}}))
    _run(monkeypatch, fake, ["history", "42", "--text"])
    out = capsys.readouterr().out
    assert "聊天记录 1 条" in out
    assert "您好 我是软件工程 2026 届本科生" in out


def test_history_raw_skips_extraction(monkeypatch, capsys):
    fake = Fake(_sub(BossClient.BOSS_HISTORY_PATH,
                     {"code": 0, "zpData": {"hasMore": True, "minMsgId": 7, "messages": ["QUJD"]}}))
    _run(monkeypatch, fake, ["history", "42", "--raw"])
    out = capsys.readouterr().out
    assert "hasMore=True minMsgId=7" in out and "QUJD" not in out


def test_im_urls_strips_protobuf_quotes():
    import base64
    from bosscli.cli import _im_urls
    blob = base64.b64encode(b'\x0a\x22https://img.bosszhipin.com/a/b.jpeg"').decode()
    assert _im_urls(blob) == ["https://img.bosszhipin.com/a/b.jpeg"]
    assert _im_urls("not-base64!!") == []


def test_ext_for_content_type_and_fallback():
    from bosscli.cli import _ext_for
    assert _ext_for("image/jpeg", "https://x/y") == ".jpg"
    assert _ext_for("application/pdf; charset=binary", "https://x/y") == ".pdf"
    assert _ext_for("", "https://x/y/简历.pdf?sign=1") == ".pdf"
    assert _ext_for("application/octet-stream", "https://x/y") == ".bin"


def test_history_download_lists_message_files(monkeypatch, capsys, tmp_path):
    import base64
    blob = base64.b64encode(b'\x0a\x1chttps://img.example.com/x.png').decode()
    fake = Fake(_sub(BossClient.BOSS_HISTORY_PATH,
                     {"code": 0, "zpData": {"hasMore": False, "minMsgId": 0, "messages": [blob]}}))

    class Resp:
        headers = {"content-type": "image/png"}
        content = b"PNGDATA"
        def raise_for_status(self): pass

    class Http:
        def get(self, url, timeout=20):
            assert url == "https://img.example.com/x.png"
            return Resp()

    fake.http = Http()
    _run(monkeypatch, fake, ["history", "42", "--download", str(tmp_path)])
    out = capsys.readouterr().out
    assert "消息内文件 1 个" in out and "已保存 1 个文件" in out
    files = list(tmp_path.iterdir())
    assert len(files) == 1 and files[0].suffix == ".png" and files[0].read_bytes() == b"PNGDATA"


def _history_fake(pages):
    calls = []
    fake = Fake()

    def hist(friend_id, **kw):
        calls.append(kw.get("max_msg_id"))
        idx = min(len(calls) - 1, len(pages) - 1)
        return _sub(BossClient.BOSS_HISTORY_PATH, pages[idx])

    fake.boss_history = hist
    return fake, calls


def test_history_all_pages_backwards_and_dedupes(monkeypatch, capsys):
    fake, calls = _history_fake([
        {"code": 0, "zpData": {"hasMore": True, "minMsgId": 100, "messages": ["A", "B"]}},
        {"code": 0, "zpData": {"hasMore": False, "minMsgId": 50, "messages": ["B", "C"]}},
    ])
    _run(monkeypatch, fake, ["history", "42", "--all", "--count", "2", "--raw"])
    out = capsys.readouterr().out
    assert "聊天记录 3 条" in out          # A,B + C (B deduped)
    assert "page=2" in out and "hasMore=False" in out
    assert calls == [0, 100]              # second page asked for messages before minMsgId


def test_history_all_respects_max_pages(monkeypatch, capsys):
    fake, calls = _history_fake([
        {"code": 0, "zpData": {"hasMore": True, "minMsgId": 90, "messages": ["A"]}},
        {"code": 0, "zpData": {"hasMore": True, "minMsgId": 80, "messages": ["B"]}},
        {"code": 0, "zpData": {"hasMore": True, "minMsgId": 70, "messages": ["C"]}},
    ])
    _run(monkeypatch, fake, ["history", "42", "--all", "--max-pages", "2", "--raw"])
    out, err = capsys.readouterr()
    assert len(calls) == 2 and "page=2" in out
    assert "max-pages=2" in err           # warns that older messages were left


def test_history_single_page_ignores_paging(monkeypatch, capsys):
    fake, calls = _history_fake([
        {"code": 0, "zpData": {"hasMore": True, "minMsgId": 100, "messages": ["A"]}},
        {"code": 0, "zpData": {"hasMore": False, "minMsgId": 1, "messages": ["B"]}},
    ])
    _run(monkeypatch, fake, ["history", "42", "--raw"])
    assert len(calls) == 1
    assert "page=1" in capsys.readouterr().out
