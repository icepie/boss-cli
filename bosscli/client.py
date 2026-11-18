"""
BossClient — off-device BOSS直聘 API client (search + authenticated read endpoints).

A session (t2 auth token + this device's client_info/uniqid + cardlist template) is the
only account-bound input; extract it once from a logged-in device, or obtain t2 with the
login flow (bosscli.login). See session.example.json.

Signing has two branches, both reproduced in signer.py:
  * batch  (/api/batch/requests, subReqs) — used by search().
  * plain  (ordinary GET/POST)            — used by the login endpoints and the read endpoints.
Search + login sit on the app's no-secretKey whitelist, so their sign key is None; call_plain /
call_batch expose a `key` argument so endpoints that DO need the account secretKey can pass it.
"""
from __future__ import annotations
import json, os, time, uuid
import requests
from . import yzwg, signer

HOST = "https://api5.zhipin.com"
BATCH_PATH = "/api/batch/requests"
BATCH_V2_PATH = "/api/batch/batchRunV2"
CARDLIST_PATH = "/api/zpgeek/app/geek/search/cardlist"

# The app version this client's request shapes were RE'd against (batchRunV2 + recruiter endpoints
# come from 14.190 captures). A session that declares a DIFFERENT version in `v` / user_agent /
# client_info.version_flag is describing a device that never existed — see _check_version.
APP_VERSION = "14.190"
DEFAULT_USER_AGENT = f"NetType/wifi Screen/1080X2209 BossZhipin/{APP_VERSION} Android 36"


def _check_version(s: dict) -> None:
    """Refuse a session whose three version declarations disagree.

    `v` (query param), user_agent (`BossZhipin/<v>`) and client_info.version_flag (`<v without
    dots><build>`) all travel in the same request; a mismatch means the profile was stitched
    together from different captures, and the server's version-gated branches then answer
    unpredictably (empty 参数错误 instead of data). Only fields that are PRESENT are checked."""
    v = str(s.get("v") or APP_VERSION)
    ua = s.get("user_agent") or ""
    if "BossZhipin/" in ua:
        tail = ua.split("BossZhipin/", 1)[1].split()
        ua_v = tail[0] if tail else ""
        if ua_v and ua_v != v:
            raise ValueError(
                f"session 版本不一致: v={v!r} 但 user_agent 声明 BossZhipin/{ua_v!r}。"
                f"同一台设备必须只有一个版本；把 v / user_agent / client_info.version_flag "
                f"统一成同一次抓包的值（当前代码面向 v{APP_VERSION}）。")
    ci = s.get("client_info") or {}
    if isinstance(ci, dict):
        flag = str(ci.get("version_flag") or "")
        if flag and not flag.startswith(v.replace(".", "")):
            raise ValueError(
                f"session 版本不一致: v={v!r} 但 client_info.version_flag={flag!r}"
                f"（应形如 {v.replace('.', '')}<build>）。请统一这三个字段。")


class AuthExpired(RuntimeError):
    """The server rejected t2 (code==7, 当前登录状态已失效). Re-login to refresh it."""
    def __init__(self, message: str = "登录状态已失效 (t2 过期)", resp: dict | None = None):
        super().__init__(message)
        self.resp = resp


class BossClient:
    def __init__(self, session: dict):
        _check_version(session)
        self.s = session
        self.http = requests.Session()

    @classmethod
    def from_file(cls, path: str) -> "BossClient":
        with open(path, encoding='utf-8') as f:
            return cls(json.load(f))

    # ---- response decode: server picks encoding via zp-* headers ----
    @staticmethod
    def _decode(raw: bytes, key: str | None):
        # 0) plaintext JSON — the server returns some errors uncompressed/unencrypted, e.g.
        #    {"code":7,"message":"当前登录状态已失效"} (t2 expired) on the batch endpoint. Try
        #    this first so an auth error surfaces its message instead of crashing the RC4 path.
        if raw.lstrip()[:1] in (b'{', b'['):
            try:
                return json.loads(raw)
            except Exception:
                pass
        # A) base64url(RC4(json|frame))  B) raw RC4(frame(LZ4(json)))  C) raw RC4(json)
        try:
            dec = yzwg.rc4(yzwg._rc4_key(key), yzwg._unb64(raw.decode('ascii')))
            if dec[:8] == yzwg.MAGIC:
                dec = yzwg._deframe(dec)
            return json.loads(dec)
        except Exception:
            pass
        dec = yzwg.rc4(yzwg._rc4_key(key), raw)
        if dec[:8] == yzwg.MAGIC:
            return json.loads(yzwg._deframe(dec))
        return json.loads(dec)

    def _decode_resp(self, content: bytes, key: str | None):
        """Decode a response body. Non-whitelisted endpoints sign with the account secretKey; the
        server usually encrypts the reply with that same key, but occasionally replies null-keyed
        (or plaintext) — so try the request key first and fall back to None."""
        try:
            return self._decode(content, key)
        except Exception:
            if key is not None:
                return self._decode(content, None)
            raise

    @staticmethod
    def raise_for_auth(resp: dict) -> dict:
        """Turn a top-level code==7 into AuthExpired; pass anything else through."""
        if isinstance(resp, dict) and resp.get("code") == 7:
            raise AuthExpired(resp.get("message") or "登录状态已失效", resp)
        return resp

    def _headers(self) -> dict:
        return {
            "User-Agent": self.s.get("user_agent", DEFAULT_USER_AGENT),
            "zp-accept-encoding": "1", "zp-accept-compressing": "3", "zp-accept-encrypting": "1",
            "t2": self.s["t2"], "traceId": "A-" + str(uuid.uuid4()),
        }

    def _outer_params(self) -> dict:
        ci = self.s["client_info"]
        ci = ci if isinstance(ci, str) else json.dumps(ci, separators=(',', ':'), ensure_ascii=False)
        return {
            "client_info": ci,
            "curidentity": self.s.get("curidentity", "0"),
            "req_time": signer.now_ms(),
            "uniqid": self.s["uniqid"],
            "v": self.s.get("v", APP_VERSION),
        }

    def _wire(self, s: dict) -> str:
        """strD + signed sp/sig + app_id (appended AFTER signing), the on-the-wire param string."""
        return (s["strD"] + "&sp=" + requests.utils.quote(s["sp"], safe='')
                + "&sig=" + s["sig"] + "&app_id=" + self.s.get("app_id", "1003"))

    def _http(self, method: str, url: str, *, data=None, headers=None, retries: int = 2):
        """One HTTP call with a small retry/backoff on transport errors (not on 4xx/5xx bodies)."""
        last = None
        for attempt in range(retries + 1):
            try:
                r = self.http.request(method, url, data=data, headers=headers, timeout=20)
                r.raise_for_status()
                return r
            except (requests.ConnectionError, requests.Timeout) as e:
                last = e
                if attempt < retries:
                    time.sleep(0.6 * (attempt + 1))
                    continue
                raise
        raise last  # pragma: no cover

    # ---- generic signed calls (reused by search + the read endpoints) ----
    def call_plain(self, method: str, path: str, req_params: dict, key: str | None = None) -> dict:
        """Sign an ordinary GET/POST (net.bosszhipin.base.m.e branch) and return the decoded body.

        Only None is dropped: the app serializes every non-null @d8.a field, so an EMPTY string is
        a real value (`securityId=&encryptSubscribeId=`) and must stay on the wire — dropping it
        changes "present but empty" into "absent", which some endpoints treat differently."""
        params = {**self._outer_params(), **{k: v for k, v in req_params.items() if v is not None}}
        s = signer.sign_request(params, path, key)
        wire = self._wire(s)
        if method.upper() == "GET":
            r = self._http("GET", HOST + path + "?" + wire, headers=self._headers())
        else:
            h = self._headers(); h["Content-Type"] = "application/x-www-form-urlencoded"
            r = self._http("POST", HOST + path, data=wire, headers=h)
        return self.raise_for_auth(self._decode_resp(r.content, key))

    def call_batch(self, subreqs: list[dict], key: str | None = None) -> dict:
        """Sign a /api/batch/requests call (net.bosszhipin.base.m.f branch); subreqs is a list of
        {method, path, query} (and optional body). Returns the decoded envelope."""
        body_obj = {"subReqs": subreqs}
        body_json = json.dumps(body_obj, separators=(',', ':'), ensure_ascii=False)
        outer = self._outer_params()
        s = signer.sign_batch(outer, body_json, BATCH_PATH, key=key)
        q = self._wire(s)
        r = self._http("GET", HOST + BATCH_PATH + "?" + q, data=s["encBody"], headers=self._headers())
        return self.raise_for_auth(self._decode_resp(r.content, key))

    # ---- v14.190 batch envelope: GET /api/batch/batchRunV2?batch_method_feed=[...] ----
    # Reproduces BaseBatchApiRequest.getBatchParams + net.bosszhipin.base.k.a: each subrequest is
    # one string `"method=<dotted.path>&k=v&…"` (params Java-URLEncoder-encoded, i.e. '+' for space),
    # joined with ", " inside a JSON-ish array (ArrayList.toString of quoted entries). The whole
    # feed is a single outer query param; the outer path is on the no-secretKey whitelist
    # (config.p.f40637a), so key=None works even for subrequests whose standalone path is NOT
    # whitelisted. Response envelope zpData is keyed by the dotted method names (@d8.c mappings).

    @staticmethod
    def _dotted(path: str) -> str:
        """/api/zpjob/job/local/data -> zpjob.job.local.data (config.p.j)."""
        p = path[len("/api/"):] if path.startswith("/api/") else path.lstrip("/")
        return p.replace("/", ".")

    def call_batch_v2(self, subreqs: list[dict], key: str | None = None) -> dict:
        """Wrap subreqs ([{"path": ..., "params": {...}}, ...]) in batch_method_feed and call
        batchRunV2. None values are dropped (the app only serializes non-null fields)."""
        entries = []
        for sr in subreqs:
            e = "method=" + self._dotted(sr["path"])
            for k, v in (sr.get("params") or {}).items():
                if v is None:
                    continue
                e += "&" + k + "=" + signer.java_url_encode(str(v))
            entries.append('"' + e + '"')
        feed = "[" + ", ".join(entries) + "]"
        return self.call_plain("GET", BATCH_V2_PATH, {"batch_method_feed": feed}, key=key)

    @classmethod
    def batch_v2_sub(cls, resp: dict, path: str) -> dict:
        """Pull one subresponse {"code","message","zpData"} out of a batchRunV2 envelope."""
        return (resp.get("zpData") or {}).get(cls._dotted(path)) or {}

    # ---- search ----
    def search(self, query: str, city: str | None = None, page: int = 1, sort: int = -1,
               filter_params: dict | None = None) -> dict:
        """One page of geek job search. `filter_params` merges into the cardlist filterParams
        (e.g. {"salary": "407", "experience": "104,105"}); server honours these keys."""
        cq = dict(self.s.get("cardlist_defaults", {}))
        cq["query"] = query
        cq["page"] = str(page)
        cq["sort"] = str(sort)
        fp = json.loads(cq.get("filterParams", '{}'))
        if city:
            fp["cityCode"] = str(city)
        if filter_params:
            for k, v in filter_params.items():
                fp[k] = v
        cq["filterParams"] = json.dumps(fp, separators=(',', ':'), ensure_ascii=False)
        subreq_query = signer.build_query(cq)
        return self.call_batch([{"method": "GET", "path": CARDLIST_PATH, "query": subreq_query}], key=None)

    # ---- authenticated geek read endpoints (RE'd from v14.050) ----
    # Signing key: whitelisted paths (config.m.f43051a) use key=None like search; the rest use the
    # account secretKey (LBase.getSecretKey, stored in the session as `secretKey`).
    def _secret(self) -> str | None:
        return self.s.get("secretKey")

    def whoami(self, user_id: str = "0") -> dict:
        """GetUserAccountGeekDetailRequest — GET /api/zpgeek/cvapp/geek/baseinfo/query (key=secretKey).
        userId=0 means "me". Response geekDetail carries expectPositionList[].{expectId,encryptExpectId}."""
        return self.call_plain("GET", "/api/zpgeek/cvapp/geek/baseinfo/query",
                               {"userId": user_id}, key=self._secret())

    def job_detail(self, security_id: str, lid: str | None = None, expect_id: str | None = None,
                   query: str | None = None, extra: dict | None = None) -> dict:
        """GetJobDetailRequest — GET /api/zpgeek/jobapp/geek/job/querydetail (key=secretKey).
        Takes the securityId from a search result; response has jobBaseInfo/bossBaseInfo/brandComInfo."""
        params = {"securityId": security_id, "lid": lid, "expectId": expect_id, "query": query}
        if extra:
            params.update(extra)
        return self.call_plain("GET", "/api/zpgeek/jobapp/geek/job/querydetail", params, key=self._secret())

    # NOTE: geek F1 recommend (GET /api/zpgeek/app/geek/recommend/joblist) is intentionally NOT
    # exposed. Called standalone (even with the full z10.b.g param set expectId/encryptExpectId/
    # page/pageSize/sortType/expectPosition/filterParams) the server rejects it with
    # code 71 参数格式异常; the app only calls it wrapped in a batchRunV2 envelope
    # (call_batch_v2 above — that transport exists now, but this wrapping is unverified).

    def cities(self) -> dict:
        """Get1004CityRequest — GET /api/zpCommon/config/city (key=None, whitelisted). Full city
        code/name tree (city / citylv2 / hotCity)."""
        return self.call_plain("GET", "/api/zpCommon/config/city", {}, key=None)

    def add_friend(self, security_id: str, job_id: str | None = None, lid: str | None = None,
                   expect_id: str | None = None, friend_id: str | None = None,
                   apply_job_directly: int = 1, similar_position: str = "0",
                   start_chat_exp_group: int = 0, extra: dict | None = None) -> dict:
        """GeekCreateFriendRequest — GET /api/zpgeek/app/friend/add (key=secretKey). WRITE: starts a
        chat / 打招呼. Verified live (code 0). The params mirror the app's ht.b.k builder:
          * security_id / lid MUST come from the SEARCH-CARD (parse_jobs securityId/lid), NOT job_detail
          * friend_id = the boss's numeric userId (parse_jobs 'userId')
          * apply_job_directly defaults to 1 (the app always sends 1); entrance/jobAddressId are unused."""
        params = {"securityId": security_id, "jobId": job_id, "lid": lid,
                  "expectId": expect_id, "friendId": friend_id,
                  "applyJobDirectly": str(apply_job_directly),
                  "similarPosition": similar_position,
                  "startChatProcessExpGroup": str(start_chat_exp_group)}
        if extra:
            params.update(extra)
        return self.call_plain("GET", "/api/zpgeek/app/friend/add", params, key=self._secret())

    # ---- boss (recruiter) endpoints — all wrapped in batchRunV2 (RE'd from v14.190) ----
    # The App's recruiter home (BossF1) never calls these paths standalone: UserAccountBossBatchRequest
    # and BossF1GetListBatchRequest both aggregate them as batchRunV2 subrequests. Verified live:
    # the profile / job-list / job-sort subs all answer code 0 with key=None (outer path is whitelisted).
    BOSS_JOB_LIST_PATH = "/api/zpjob/job/local/data"        # GetBossJobListRequest — my posted jobs
    BOSS_JOB_SORT_PATH = "/api/zpjob/job/sort/list"         # GetJobSortListRequest — job switcher
    BOSS_DETAIL_PATH = "/api/zpboss/app/boss/get/detail"    # GetUserAccountBossDetailRequest — my profile
    BOSS_RCMD_PATH = "/api/zpjob/recommend/geek/list"       # BossF1GetGeekListRequest — 推荐牛人
    BOSS_SEARCH_PATH = "/api/zpitem/boss/search"            # GetAdvancedSearchGeekListRequest — 牛人搜索
    BOSS_ADD_FRIEND_PATH = "/api/zpjob/chat/add/friend"     # BossCreateFriendRequest — 给牛人打招呼
    BOSS_ADD_FRIEND_AI_PATH = "/api/zprelation/friend/boss/add"  # fromAiRecommend variant (a.R2)
    BOSS_CONTACTS_PATH = "/api/zprelation/friend/getBaseInfoList"  # 已沟通牛人（含 securityId/lid）
    BOSS_FRIEND_IDS_PATH = "/api/zprelation/friend/getFriendIdList"  # 会话对象 id 分桶

    # UserAccountBossBatchRequest, exactly as module/login/util/g.java:c builds it (16 subs).
    BOSS_ACCOUNT_SUBS = [
        {"path": "/api/zpboss/app/boss/get/detail"},                    # zpboss.app.boss.get.detail
        {"path": "/api/zpjob/job/local/data"},                          # zpjob.job.local.data
        {"path": "/api/zpuser/dynamicBar/get"},                         # zpuser.dynamicBar.get
        {"path": "/api/zpuser/user/getBottomBtns"},                     # zpuser.user.getBottomBtns
        {"path": "/api/zpjob/job/sort/list"},                           # zpjob.job.sort.list
        {"path": "/api/zpjob/guide/f1"},                                # zpjob.guide.f1
        {"path": "/api/certification/security/get"},                    # certification.security.get
        {"path": "/api/zpitem/boss/getItemMallF3"},                     # zpitem.boss.getItemMallF3
        {"path": "/api/zpp/app/user/unBzbOrderList"},                   # zpp.app.user.unBzbOrderList
        {"path": "/api/zpp/app/user/rechargeGuide"},                    # zpp.app.user.rechargeGuide
        {"path": "/api/zpitem/resumeDeliver/getDeliverSchedule"},       # zpitem.resumeDeliver.getDeliverSchedule
        {"path": "/api/zpblock/job/not/chatkey/list"},                  # zpblock.job.not.chatkey.list
        {"path": "/api/hunter/app/hunter/home/guidanceBar"},            # hunter.app.hunter.home.guidanceBar
        {"path": "/api/zpinterview/boss/interview/aiFocusPoints/experiment"},  # zpinterview.boss…experiment
        {"path": "/api/certification/security/sceneLimit/batchCheck",
         "params": {"sceneTypes": "18,21"}},                            # certification.security.sceneLimit.batchCheck
        {"path": "/api/zpjob/security/sceneLimit/check",
         "params": {"sceneType": "118"}},                               # zpjob.security.sceneLimit.check
    ]

    def boss_account(self, full: bool = False) -> dict:
        """UserAccountBossBatchRequest — one batchRunV2 carrying my profile + job list + job sort list.
        Read the subs with batch_v2_sub(resp, BossClient.BOSS_DETAIL_PATH) etc.
        `full=True` reproduces ALL 16 subs of module/login/util/g.java:c (recruiter home aggregate:
        dynamic bar, bottom buttons, item mall, deliver schedule, not-chat-key jobs, unpaid orders,
        security scene limits …); two of them carry params (sceneTypes=18,21 / sceneType=118)."""
        if full:
            return self.call_batch_v2(self.BOSS_ACCOUNT_SUBS)
        return self.call_batch_v2([
            {"path": self.BOSS_DETAIL_PATH},
            {"path": self.BOSS_JOB_LIST_PATH},
            {"path": self.BOSS_JOB_SORT_PATH},
        ])

    def boss_jobs(self) -> dict:
        """Just the posted-jobs sub (zpjob.job.local.data), for when the feed template isn't loaded."""
        return self.call_batch_v2([{"path": self.BOSS_JOB_LIST_PATH}])

    def boss_recommend_geeks(self, job_id, page: int = 1, page_size: int = 15, sort_type: int = 0,
                             filter_params=None, source: int = 0) -> dict:
        """BossF1GetGeekListRequest — 推荐牛人 for one of MY jobs (recruiter F1 home feed).
        Params mirror BossF1DataViewModel.E0: jobId/page/pageSize/sortType/filterParams/source/uuid/
        coverScreenMemory. `filter_params` is the raw filterParams JSON string (F1 entity's k()).
        The server answers inner code 200001 当前职位不在线 when job_id is not an online job of the
        account, so a closed test posting is not a transport failure."""
        params = {"jobId": str(job_id), "page": str(page), "pageSize": str(page_size),
                  "sortType": str(sort_type), "source": str(source),
                  "uuid": str(uuid.uuid4()), "coverScreenMemory": "0"}
        if filter_params is not None:
            params["filterParams"] = (filter_params if isinstance(filter_params, str)
                                      else json.dumps(filter_params, ensure_ascii=False,
                                                      separators=(",", ":")))
        return self.call_batch_v2([{"path": self.BOSS_RCMD_PATH, "params": params}])

    def boss_add_friend(self, security_id: str, job_id: str | None = None,
                        friend_id: str | None = None, expect_id: str | None = None,
                        lid: str | None = None, from_: str | None = None,
                        entrance: int = 77, greet: str | None = None,
                        language_id: str | None = None, extra: dict | None = None) -> dict:
        """BossCreateFriendRequest — GET /api/zpjob/chat/add/friend (key=secretKey). WRITE: 给牛人打招呼
        / 发起沟通. Params mirror CreateFriendManager.g() (the boss branch):
          * securityId + lid come from the GEEK CARD (推荐牛人 card), not from my job
          * friendId / expectId describe the GEEK (card userId / expectId)
          * jobId is one of MY jobs (the card is served in that job's recommend feed)
          * entrance is the UI entry: 77 = F1 推荐卡 (the app's primitive default is 0)
          * greet = 打招呼语, languageId = 语音/外语场次 (omit unless that scene applies)
        None params are dropped, mirroring the app's null String fields; `entrance` is a Java int
        so it is always serialized (default 77 here)."""
        params = {"securityId": security_id, "jobId": job_id, "friendId": friend_id,
                  "expectId": expect_id, "lid": lid, "from": from_,
                  "entrance": str(entrance), "greet": greet, "languageId": language_id}
        if extra:
            params.update(extra)
        return self.call_plain("GET", self.BOSS_ADD_FRIEND_PATH, params, key=self._secret())

    def boss_contacts(self) -> dict:
        """zprelation.friend.getBaseInfoList — every geek this recruiter has a relation with.
        Each card carries friendId / expectId / jobId / securityId / lid-less chatStatus, i.e. the
        exact inputs `greet` and the IM session need. Verified live (88 contacts, identity=1)."""
        return self.call_batch_v2([{"path": self.BOSS_CONTACTS_PATH}])

    @staticmethod
    def parse_boss_contacts(resp: dict) -> list[dict]:
        sub = BossClient.batch_v2_sub(resp, BossClient.BOSS_CONTACTS_PATH)
        return ((sub.get("zpData") or {}).get("baseInfoList")) or []

    @staticmethod
    def parse_boss_jobs(resp: dict) -> list[dict]:
        """jobList of a batchRunV2 envelope (or of the boss_jobs() response)."""
        sub = BossClient.batch_v2_sub(resp, BossClient.BOSS_JOB_LIST_PATH)
        return ((sub.get("zpData") or {}).get("jobList")) or []

    @staticmethod
    def parse_expects(whoami_resp: dict) -> list[dict]:
        """Pull [{expectId, encryptExpectId, positionName, salary, location}] out of a whoami response."""
        data = whoami_resp.get("zpData", whoami_resp) or {}
        detail = data.get("geekDetail") or data.get("geekDetailResponse") or data
        out = []
        for key in ("expectPositionList", "expectInternList", "expectList"):
            for e in (detail.get(key) or []):
                out.append({
                    "expectId": e.get("expectId"),
                    "encryptExpectId": e.get("encryptExpectId"),
                    "positionName": e.get("positionName") or e.get("positionType"),
                    "salary": e.get("salaryDesc") or e.get("salary"),
                    "location": e.get("locationName") or e.get("location"),
                    "kind": key,
                })
        return out

    @staticmethod
    def cardlist_data(resp: dict) -> dict:
        """The inner cardlist zpData (holds cardList, nlpFilters, labelFilter, sorts, searchLid …)."""
        card = resp.get("zpData", {}).get(CARDLIST_PATH, {})
        return card.get("zpData", {}) or {}

    @staticmethod
    def parse_jobs(resp: dict) -> list[dict]:
        zp = BossClient.cardlist_data(resp)
        jobs = []
        for cl in zp.get("cardList", []) or []:
            jobs += cl.get("positionSearchCardList", []) or []
        if not jobs and isinstance(zp.get("jobList"), list):
            jobs = zp["jobList"]
        out = []
        for j in jobs:
            pn = j.get("positionName")
            out.append({
                "jobId": j.get("jobId") or j.get("encryptJobId"),
                "encryptJobId": j.get("encryptJobId"),
                "name": pn.get("name") if isinstance(pn, dict) else (pn or j.get("jobName")),
                "salary": j.get("salaryDesc"),
                "company": j.get("brandName") or j.get("company"),
                "city": j.get("cityName") or j.get("city"),
                "area": j.get("areaDistrict") or j.get("businessDistrict"),
                "experience": j.get("jobExperience"),
                "degree": j.get("jobDegree"),
                "labels": j.get("jobLabels") or j.get("skills"),
                "hr": (j.get("bossName") or j.get("name")),
                "hrTitle": j.get("bossTitle"),
                "userId": j.get("userId"),          # boss's numeric id — friendId for `boss chat`
                "securityId": j.get("securityId"),
                "lid": j.get("lid"),
            })
        return out
