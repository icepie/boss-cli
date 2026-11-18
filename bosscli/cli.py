"""boss-cli entry point.  Usage: boss {search|filters|login|whoami|detail|cities|chat|me|jobs|rcmd|api} ... [opts]
(or `python -m bosscli.cli ...` without installing)."""
from __future__ import annotations
import argparse, json, os, sys, time
from .client import BossClient, AuthExpired
from . import output

DEFAULT_SESSION = os.environ.get("BOSS_SESSION") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "session.local.json")

# common city codes (BOSS uses China weather-station codes as cityCode)
CITY_ALIASES = {
    "全国": "100010000", "北京": "101010100", "上海": "101020100", "广州": "101280100",
    "深圳": "101280600", "杭州": "101210100", "成都": "101270100", "南京": "101190100",
    "武汉": "101200100", "西安": "101110100", "苏州": "101190400", "天津": "101030100",
    "长沙": "101250100", "重庆": "101040100", "郑州": "101180100", "厦门": "101230200",
}

# --sort names -> request value (server echoes internal codes 6/1/2; -1 = comprehensive request value)
SORT_ALIASES = {"综合": -1, "comprehensive": -1, "最新": 1, "latest": 1, "距离": 2, "distance": 2}

# convenience filter flags -> filterParams key. Values are BOSS codes (discover with `boss filters`);
# multi-value keys take a comma-separated string. --filter-param KEY=VAL is the raw escape hatch.
FILTER_FLAGS = {
    "salary": "salary", "experience": "experience", "degree": "degree",
    "scale": "scale", "stage": "stage", "jobtype": "jobType",
    "industry": "industry", "position": "position",
}

MAX_ALL_PAGES = 10  # safety cap for --all so we never hammer the API unbounded


def resolve_city(s: str) -> tuple[str, str]:
    """return (label, code). accepts an alias name or a raw code."""
    if s in CITY_ALIASES:
        return s, CITY_ALIASES[s]
    return s, s  # assume raw code


def _build_filter_params(args) -> dict:
    """Collect --filter-param KEY=VAL plus the convenience flags into one filterParams dict."""
    fp: dict = {}
    for item in (args.filter_param or []):
        if "=" not in item:
            sys.exit(f"--filter-param 需形如 KEY=VALUE，收到: {item!r}")
        k, v = item.split("=", 1)
        fp[k.strip()] = v.strip()
    for flag, key in FILTER_FLAGS.items():
        v = getattr(args, flag, None)
        if v is not None:
            fp[key] = v
    return fp


def _resolve_sort(v) -> int:
    if v is None:
        return -1
    if isinstance(v, str) and v in SORT_ALIASES:
        return SORT_ALIASES[v]
    try:
        return int(v)
    except (TypeError, ValueError):
        sys.exit(f"--sort 需为数字或 {list(SORT_ALIASES)}，收到 {v!r}")


def _client(args) -> BossClient:
    if not os.path.exists(args.session):
        sys.exit(f"session file not found: {args.session}\n"
                 f"Copy session.example.json -> session.local.json and fill in your t2 token "
                 f"and device profile (see README), or run `boss login --phone <号码> --solve`.")
    try:
        client = BossClient.from_file(args.session)
    except ValueError as e:      # stitched session profile — _check_version
        sys.exit(f"[session 版本不一致] {e}")
    if getattr(args, "token", None):
        client.s["t2"] = args.token
    return client


def _auth_hint():
    print("[!] 登录状态已失效 (t2 过期)。用以下命令刷新后重试：", file=sys.stderr)
    print("    boss login --phone <你的号码> --solve", file=sys.stderr)


def cmd_search(args):
    client = _client(args)
    sort = _resolve_sort(args.sort)
    filter_params = _build_filter_params(args)

    cities = [resolve_city(x.strip()) for x in args.city.split(",")] if args.city else [("默认", None)]
    # page selection: --all > --pages N > --page P (single, back-compat) > page 1
    if args.all:
        page_nums = list(range(1, MAX_ALL_PAGES + 1))
    elif args.pages > 1:
        page_nums = list(range(1, args.pages + 1))
    else:
        page_nums = [args.page]
    last_page = page_nums[-1]
    seen: set = set()
    all_jobs: list[dict] = []
    errors = 0

    try:
        for label, code in cities:
            for page in page_nums:
                resp = client.search(args.query, city=code, page=page, sort=sort,
                                     filter_params=filter_params or None)
                card = resp.get("zpData", {}).get("/api/zpgeek/app/geek/search/cardlist", {})
                if resp.get("code") != 0 or card.get("code") != 0:
                    print(f"[!] {label} p{page}: outer={resp.get('code')} {resp.get('message')} | "
                          f"cardlist={card.get('code')} {card.get('message')}", file=sys.stderr)
                    if resp.get("code") in (36, 37):   # 账户异常/频控 —— 降速或稍后再试
                        print("    [i] 触发 BOSS 风控/频控，请降低频率、隔一段时间再搜。", file=sys.stderr)
                    _identity_hint(client, card.get("code") or resp.get("code"),
                                   card.get("message") or resp.get("message"), "geek")
                    errors += 1
                    break
                jobs = BossClient.parse_jobs(resp)
                if not jobs:
                    break
                new = 0
                for j in jobs:
                    keyid = j.get("jobId") or j.get("securityId") or id(j)
                    if keyid in seen:
                        continue
                    seen.add(keyid)
                    j["_city_label"] = label
                    all_jobs.append(j)
                    new += 1
                if args.limit and len(all_jobs) >= args.limit:
                    break
                if new == 0 and page > 1:   # page returned only dups -> end of useful results
                    break
                if page < last_page:
                    time.sleep(0.5)          # be polite between pages
            if args.limit and len(all_jobs) >= args.limit:
                break
    except AuthExpired:
        _auth_hint(); sys.exit(2)

    if args.filter:
        kw = args.filter
        def hit(j):
            hay = (j.get("name") or "") + " " + output._labels(j.get("labels")) + " " + (j.get("company") or "")
            return kw in hay
        all_jobs = [j for j in all_jobs if hit(j)]

    if args.limit:
        all_jobs = all_jobs[:args.limit]

    fmt = "json" if args.json else args.format
    body = output.render(all_jobs, fmt)

    if args.output:
        with open(args.output, "w", encoding="utf-8", newline="") as f:
            f.write(body + ("\n" if not body.endswith("\n") else ""))
        print(f"[✓] {len(all_jobs)} jobs -> {args.output} ({fmt})")
        return

    if fmt == "text":
        flt = f" filter='{args.filter}'" if args.filter else ""
        pg = f"pages=1..{last_page}" if last_page > 1 else "page=1"
        print(f"# {len(all_jobs)} jobs for '{args.query}' (cities={[c[0] for c in cities]}, {pg}{flt})")
    print(body)
    if errors and not all_jobs:      # every city failed -> non-zero so scripts can branch
        sys.exit(2)


def cmd_filters(args):
    """Probe the live search response and print the filter/sort options it advertises, so the
    user can discover valid codes for --industry / --position / --sort (and see the NLP tags)."""
    client = _client(args)
    _, code = resolve_city(args.city) if args.city else ("", None)
    try:
        resp = client.search(args.query, city=code, page=1)
    except AuthExpired:
        _auth_hint(); sys.exit(2)
    zp = BossClient.cardlist_data(resp)

    print("# sorts (--sort 值)")
    for s in zp.get("sorts") or []:
        print(f"    {s.get('code'):>3}  {s.get('name')}")

    def walk(node, depth, prefix_seen):
        subs = node.get("subLevelModelList")
        name = node.get("name"); val = node.get("value")
        if val:
            code = val.split(":", 1)[1] if ":" in val else val
            print(f"    {'  '*depth}{code:<10} {name}   ({val})")
        elif name:
            print(f"    {'  '*depth}{name}")
        for s in (subs or []):
            walk(s, depth + 1, prefix_seen)

    for cat in zp.get("nlpFilters") or []:
        print(f"\n# {cat.get('name')}  (--industry / --position 用左侧数字码)")
        for s in cat.get("subLevelModelList") or []:
            walk(s, 0, set())

    lf = zp.get("labelFilter") or {}
    if lf.get("tags"):
        print("\n# labelFilter tags")
        for t in lf["tags"]:
            print(f"    {t.get('code'):>8}  {t.get('tag')}  [{t.get('source','')}]")

    print("\n提示：薪资/经验/学历/规模/融资的码表不在此响应里；用 `--filter-param 键=值` 原样透传，"
          "或在 App 里对照。已知服务器认这些 filterParams 键：salary / experience / degree / "
          "scale / stage / jobType / industry / position。")


def _run(fn):
    """Call an endpoint, translating AuthExpired into the standard hint/exit."""
    try:
        return fn()
    except AuthExpired:
        _auth_hint(); sys.exit(2)


SIDE_CURIDENTITY = {"geek": "0", "boss": "1"}


def _identity_hint(client, code, message: str = "", side: str | None = None):
    """BOSS answers code 24 请切换身份后再试 (and 71 参数格式异常 on some paths) when the session's
    curidentity doesn't match the API side being called: 0 = 求职端 (geek), 1 = 招聘端 (boss).
    `side` is the command's own side, so a plain 71 on the CORRECT side (bad params) stays quiet."""
    cur = str(getattr(client, "s", {}).get("curidentity", "0"))
    mismatched = side is not None and cur != SIDE_CURIDENTITY.get(side, cur)
    if code != 24 and "切换身份" not in (message or "") and not (code == 71 and mismatched):
        return False
    side = {"0": "求职端(geek)", "1": "招聘端(boss)"}.get(cur, f"curidentity={cur}")
    print(f"    [i] 该 session 是 {side} 身份，不能调用另一侧接口。用同身份的 session，"
          f"或 `boss login --identity geek|boss` 重新登录。", file=sys.stderr)
    return True


def _check_code(resp: dict, what: str, client=None, side: str | None = None):
    if isinstance(resp, dict) and resp.get("code") not in (0, None):
        msg = resp.get("message")
        print(f"[!] {what}: code={resp.get('code')} {msg}", file=sys.stderr)
        if resp.get("code") in (36, 37):
            print("    [i] 触发 BOSS 风控/频控，请降低频率、稍后再试。", file=sys.stderr)
        if client is not None:
            _identity_hint(client, resp.get("code"), msg, side)
        sys.exit(2)


def cmd_whoami(args):
    client = _client(args)
    resp = _run(lambda: client.whoami())
    _check_code(resp, "whoami", client, side="geek")
    if args.json:
        print(json.dumps(resp, ensure_ascii=False, indent=2)); return
    expects = BossClient.parse_expects(resp)
    print(f"# uid={client.s.get('uid')}  求职期望 {len(expects)} 个")
    for e in expects:
        print(f"- [{e['kind']}] {e['positionName']} | {e['salary']} | {e['location']}  "
              f"expectId={e['expectId']} encryptExpectId={e['encryptExpectId']}")
    if args.set_expect and expects:
        e = expects[0]
        with open(args.session, encoding="utf-8") as f:
            sess = json.load(f)
        cd = sess.setdefault("cardlist_defaults", {})
        cd["expectId"] = str(e["expectId"]) if e["expectId"] is not None else cd.get("expectId")
        if e["encryptExpectId"]:
            cd["encryptExpectId"] = e["encryptExpectId"]
        with open(args.session, "w", encoding="utf-8") as f:
            json.dump(sess, f, ensure_ascii=False, indent=2)
        print(f"[✓] 已把 expectId={cd['expectId']} 写回 {args.session} 的 cardlist_defaults")


def cmd_detail(args):
    client = _client(args)
    resp = _run(lambda: client.job_detail(args.security_id, lid=args.lid, expect_id=args.expect_id))
    _check_code(resp, "detail", client, side="geek")
    if args.json:
        print(json.dumps(resp, ensure_ascii=False, indent=2)); return
    zp = resp.get("zpData", resp) or {}
    jb = zp.get("jobBaseInfo") or {}
    boss = zp.get("bossBaseInfo") or {}
    brand = zp.get("brandComInfo") or {}
    print(f"# {jb.get('jobName') or jb.get('positionName') or '(职位)'} | {jb.get('salaryDesc','')}")
    print(f"  公司: {brand.get('brandName','')}  {brand.get('scaleName','')} {brand.get('stageName','')}")
    print(f"  boss: {boss.get('name','')} {boss.get('title','')}")
    loc = jb.get("locationName") or jb.get("address") or ""
    if loc:
        print(f"  地点: {loc}")
    jd = jb.get("postDescription") or jb.get("jobDesc") or ""
    if jd:
        print("  --- JD ---")
        print("  " + jd.replace("\n", "\n  "))


def cmd_cities(args):
    client = _client(args)
    resp = _run(lambda: client.cities())
    _check_code(resp, "cities", client, side="geek")
    zp = resp.get("zpData", resp) or {}
    flat = []
    def walk(nodes):
        for nd in nodes or []:
            code, name = nd.get("code"), nd.get("name")
            if code and name:
                flat.append((str(code), name))
            walk(nd.get("subLevelModelList") or nd.get("subList") or nd.get("children"))
    walk(zp.get("city") or zp.get("cityList") or [])
    if args.json:
        print(json.dumps(dict(flat), ensure_ascii=False, indent=2)); return
    kw = args.grep
    shown = 0
    for code, name in flat:
        if kw and kw not in name:
            continue
        print(f"{code}\t{name}")
        shown += 1
    print(f"# {shown}/{len(flat)} cities" + (f" matching '{kw}'" if kw else ""), file=sys.stderr)


def cmd_chat(args):
    """WRITE action — 打招呼/发起沟通. Dry-run by default; --confirm actually sends it.
    securityId / --lid / --friend-id(=userId) come from a SEARCH result (boss search --json),
    NOT from job detail — friend/add only accepts the search-card securityId."""
    client = _client(args)
    if not args.expect_id:   # default to the session's expectId (see `boss whoami --set-expect`)
        args.expect_id = client.s.get("cardlist_defaults", {}).get("expectId")
    payload = {"securityId": args.security_id, "jobId": args.job_id, "lid": args.lid,
               "expectId": args.expect_id, "friendId": args.friend_id, "applyJobDirectly": 1}
    if not args.confirm:
        print("[dry-run] 将向 GET /api/zpgeek/app/friend/add 发起打招呼（写操作，会以你的账号发出）：")
        print("  " + json.dumps({k: v for k, v in payload.items() if v is not None}, ensure_ascii=False))
        print("  确认无误后加 --confirm 真正发送。")
        if not args.friend_id:
            print("  [i] 建议带上 --friend-id <userId>（来自 boss search --json 的 userId），否则可能 code=17。")
        return
    resp = _run(lambda: client.add_friend(
        args.security_id, job_id=args.job_id, lid=args.lid, expect_id=args.expect_id,
        friend_id=args.friend_id))
    _check_code(resp, "chat", client, side="geek")
    rel = (resp.get("zpData") or {}).get("relation") or {}
    print(f"[✓] 打招呼成功 · {resp.get('message') or 'ok'}"
          + (f" · 已与 {rel.get('name')}（{rel.get('jobName')}）建立会话" if rel else ""))
    if args.json:
        print(json.dumps(resp, ensure_ascii=False, indent=2))


def _sub_check(sub: dict, what: str, client=None, side: str = "boss"):
    """Validate a batchRunV2 subresponse's inner code (outer envelope is already code 0 here)."""
    if (sub.get("code") or 0) not in (0, None):
        print(f"[!] {what}: code={sub.get('code')} {sub.get('message')}", file=sys.stderr)
        if client is not None:
            _identity_hint(client, sub.get("code"), sub.get("message"), side)
        sys.exit(2)


def _boss_subs_text(resp: dict, paths: list[tuple[str, str]]):
    """Print (label, path) subs as `label -> code message` lines, for quick health checks."""
    for label, path in paths:
        sub = BossClient.batch_v2_sub(resp, path)
        print(f"  {label}: code={sub.get('code')} {sub.get('message')}")


def _digest(zp: dict, limit: int = 3) -> str:
    """One-line summary of a subresponse's zpData: list sizes + scalar values (never the whole blob)."""
    if not isinstance(zp, dict) or not zp:
        return ""
    parts = []
    for k, v in zp.items():
        if isinstance(v, list):
            parts.append(f"{k}[{len(v)}]")
        elif isinstance(v, (str, int, float, bool)):
            s = str(v)
            parts.append(f"{k}={s[:24] + ('…' if len(s) > 24 else '')}")
        elif isinstance(v, dict) and v:
            parts.append(f"{k}{{{len(v)}}}")
        if len(parts) >= limit:
            break
    return "  " + " ".join(parts)


def cmd_bossme(args):
    """Recruiter identity: company + boss profile (UserAccountBossBatchRequest detail sub).
    --full sends all 16 subs of the app's account aggregate and digests each reply."""
    client = _client(args)
    resp = _run(lambda: client.boss_account(full=args.full))
    if resp.get("code") not in (0, None):
        _check_code(resp, "batchRunV2", client, side="boss")
    if args.full:
        if args.json:
            print(json.dumps(resp, ensure_ascii=False, indent=2)); return
        subs = BossClient.BOSS_ACCOUNT_SUBS
        print(f"# 账户整批 {len(subs)} 个子请求 (UserAccountBossBatchRequest)")
        bad = 0
        for sr in subs:
            sub = BossClient.batch_v2_sub(resp, sr["path"])
            code = sub.get("code")
            if code not in (0, None):
                bad += 1
            print(f"  {BossClient._dotted(sr['path']):<52} code={code} "
                  f"{_digest(sub.get('zpData') or {})}")
        if bad:
            print(f"[!] {bad} 个子请求未 code=0；--json 看原文", file=sys.stderr)
        return
    sub = BossClient.batch_v2_sub(resp, BossClient.BOSS_DETAIL_PATH)
    _sub_check(sub, "boss detail", client)
    if args.json:
        print(json.dumps(sub, ensure_ascii=False, indent=2)); return
    ud = sub.get("zpData") or {}
    det = ud.get("userDetail") or {}
    extra = det.get("userExtra") or {}
    info = det.get("userInfo") or {}
    scale = extra.get("scaleCategory") or extra.get("scale")
    if isinstance(scale, dict):
        scale = scale.get("name") or scale.get("scaleName") or ""
    print(f"# 招聘端身份 · {extra.get('comName') or extra.get('companyName') or '(公司)'}"
          f"  name={info.get('name','')}  userId={info.get('userId')}")
    print(f"  identityType={extra.get('identityType')}  active={det.get('activeTimeDesc') or ''}")
    print(f"  行业: {extra.get('industryCategory','')}  规模: {scale or ''}"
          f"  公司全称: {extra.get('companyFullName','')}")
    print(f"  今日面试={ud.get('interviewTodayTime')}  待评价={ud.get('interviewNotCommentCount')}"
          f"  可聊牛人={ud.get('canShareGeek')}")


def cmd_bossjobs(args):
    """My posted jobs (zpjob.job.local.data). jobId here is what boss rcmd/search take."""
    client = _client(args)
    resp = _run(client.boss_jobs)
    if resp.get("code") not in (0, None):
        _check_code(resp, "batchRunV2", client, side="boss")
    sub = BossClient.batch_v2_sub(resp, BossClient.BOSS_JOB_LIST_PATH)
    _sub_check(sub, "job list", client)
    jobs = BossClient.parse_boss_jobs(resp)
    if args.json:
        print(json.dumps(jobs, ensure_ascii=False, indent=2)); return
    print(f"# 我的职位 {len(jobs)} 个 (jobStatus/jobAuditStatus 为服务端原始码)")
    for j in jobs:
        print(f"- {j.get('jobId')} | {j.get('positionName')} | {j.get('salaryDesc')} | "
              f"status={j.get('jobStatus')}/audit={j.get('jobAuditStatus')} | "
              f"{j.get('experienceName','')} {j.get('degreeName','')} | "
              f"encryptJobId={j.get('encryptJobId')}")
    if jobs:
        print("\n下一步: boss rcmd <jobId>   # 该职位的推荐牛人（职位需在线，否则 code 200001）")


def cmd_bossrcmd(args):
    """推荐牛人 for one of my jobs (BossF1GetGeekListRequest via batchRunV2)."""
    client = _client(args)
    fp = None
    if args.filter_params:
        fp = args.filter_params
    resp = _run(lambda: client.boss_recommend_geeks(
        args.job_id, page=args.page, page_size=args.page_size,
        sort_type=args.sort_type, filter_params=fp))
    if resp.get("code") not in (0, None):
        _check_code(resp, "batchRunV2", client, side="boss")
    sub = BossClient.batch_v2_sub(resp, BossClient.BOSS_RCMD_PATH)
    if args.json:
        print(json.dumps(sub, ensure_ascii=False, indent=2)); return
    if sub.get("code") not in (0, None):
        print(f"[!] 推荐牛人: code={sub.get('code')} {sub.get('message')}", file=sys.stderr)
        if str(sub.get("code")) == "200001":
            print("    [i] 该职位当前不在线；用 `boss jobs` 换一个在线职位。", file=sys.stderr)
        _identity_hint(client, sub.get("code"), sub.get("message"), "boss")
        sys.exit(2)
    zd = sub.get("zpData") or {}
    shown = 0
    for name in ("geekList", "geekCardList", "cardList", "list"):
        cards = zd.get(name)
        if isinstance(cards, list) and cards:
            print(f"# 推荐牛人 {len(cards)} 条 (page={args.page})")
            for g in cards:
                print(f"- {g.get('name') or g.get('geekName','')} | {g.get('positionName','')} | "
                      f"{g.get('salaryDesc','')} | {g.get('geekWorkYear','')} {g.get('geekDegree','')} | "
                      f"securityId={g.get('securityId')}")
            shown = len(cards)
            break
    if not shown:
        print(f"# 推荐牛人: 无列表字段；zpData keys={list(zd)[:12]}")
        print(json.dumps(sub, ensure_ascii=False, indent=2))


APPLY_STATUS_CODES = {"随时到岗": "701", "暂不考虑": "702", "考虑机会": "703", "月内到岗": "704"}


def _short(v, n: int = 14) -> str:
    """securityId is ~600 chars of opaque ciphertext — show only its head in tables (--json has all)."""
    s = "" if v is None else str(v)
    return s if len(s) <= n else s[:n] + "…"
WITHIN_CODES = {"7": "3", "30": "1", "90": "2"}       # chat-status: 开始沟通时间


def cmd_boss_contacts(args):
    """沟通列表（zprelation.bossTag.contactList：分页 + 服务端筛选）或 --all 的全部关系（getBaseInfoList）。
    卡片自带 securityId/lid/geekId/expectId，可直接喂 `boss greet`。"""
    client = _client(args)
    if args.all:
        resp = _run(client.boss_contacts_base)
        sub = BossClient.batch_v2_sub(resp, BossClient.BOSS_CONTACTS_PATH)
        _sub_check(sub, "contacts", client)
        cards = BossClient.parse_boss_contacts(resp)
        if args.json:
            print(json.dumps(cards, ensure_ascii=False, indent=2)); return
        zd = sub.get("zpData") or {}
        print(f"# 全部关系 {len(cards)} 条（identity={zd.get('identity')} 置顶={zd.get('topCount')}）")
        for ct in cards[:args.limit or len(cards)]:
            print(f"- {ct.get('name','')} | {ct.get('expectPositionName','')} | {ct.get('expectSalary','')} | "
                  f"chatStatus={ct.get('chatStatus')} | job={ct.get('jobName','')} | "
                  f"friendId={ct.get('friendId')} securityId={_short(ct.get('securityId'))}")
        return

    encrypt_job_id = args.job
    if args.source == 3 and not encrypt_job_id:
        jobs = BossClient.parse_boss_jobs(_run(client.boss_jobs))
        if not jobs:
            sys.exit("[✗] source=3 需要 --job <encryptJobId>，而当前账号没有职位。")
        encrypt_job_id = jobs[0]["encryptJobId"]
        print(f"[i] 未给 --job，用第一个职位的 encryptJobId", file=sys.stderr)

    filter_params = {}
    if args.apply_status:
        code = APPLY_STATUS_CODES.get(args.apply_status, args.apply_status)
        filter_params["geek-apply-status"] = code
    if args.within:
        filter_params["chat-status"] = WITHIN_CODES.get(str(args.within), str(args.within))

    pages = range(1, args.pages + 1)
    seen, cards = set(), []
    meta = {}
    for page in pages:
        resp = _run(lambda p=page: client.boss_contacts(
            page=p, source=args.source, encrypt_job_id=encrypt_job_id,
            filter_params=filter_params or None, sort_type=args.sort))
        sub = BossClient.batch_v2_sub(resp, BossClient.BOSS_CONTACT_LIST_PATH)
        _sub_check(sub, "contact list", client)
        batch = BossClient.parse_contact_cards(resp)
        meta = BossClient.contact_page_meta(resp)
        new = 0
        for ct in batch:
            key = ct.get("geekId") or ct.get("securityId")
            if key in seen:
                continue
            seen.add(key); cards.append(ct); new += 1
        if not meta.get("hasMore") or not new:
            break
        if len(pages) > 1:
            time.sleep(0.4)

    if args.json:
        print(json.dumps(cards, ensure_ascii=False, indent=2)); return
    flt = ""
    if filter_params:
        flt = " filter=" + json.dumps(filter_params, ensure_ascii=False)
    print(f"# 沟通列表 {len(cards)} 条（source={args.source}{flt} total={meta.get('total')}）")
    for ct in cards:
        print(f"- {ct.get('geekName','')} | {ct.get('expectPositionName','')} | {ct.get('salary','')} | "
              f"{ct.get('geekDegree','')} | {ct.get('applyStatusDesc','')} | "
              f"聊天卡={ct.get('chatCardCount')} | {ct.get('actionDateDesc','')} | "
              f"geekId={ct.get('geekId')} lid={ct.get('lid')} securityId={_short(ct.get('securityId'))}")


def _im_text(blob: str) -> str:
    """Best-effort text out of an IM message: the wire format is a base64 protobuf with
    length-prefixed UTF-8 strings. Pulls the longest CJK/ASCII runs so a message is readable
    without a schema; URLs and stray flags are filtered out."""
    import base64, re
    try:
        raw = base64.b64decode(blob + "=" * (-len(blob) % 4))
    except Exception:
        return ""
    out = []
    for m in re.finditer(rb"(?:[\x20-\x7e]|[\xe4-\xe9][\x80-\xbf]{2}){2,}", raw):
        try:
            t = m.group().decode("utf-8")
        except Exception:
            continue
        if t.startswith(("http://", "https://")) and len(t) > 60:
            continue
        out.append(t.strip())
    return " ".join(dict.fromkeys(x for x in out if len(x) >= 2))


def _hl(v) -> str:
    """Resume fields may be highlight objects {"content": ..., "indexList": [...]} — unwrap to text."""
    if isinstance(v, dict):
        return str(v.get("content") or "")
    return "" if v is None else str(v)


def cmd_boss_resume(args):
    """牛人简历（GetGeekResumeVipRequest → batchRunV2），securityId 来自 contacts/rcmd 卡片。"""
    client = _client(args)
    resp = _run(lambda: client.boss_resume(args.security_id))
    _check_code(resp, "batchRunV2", client, side="boss")
    sub = BossClient.batch_v2_sub(resp, BossClient.BOSS_RESUME_PATH)
    _sub_check(sub, "resume", client)
    zd = BossClient.parse_resume(resp)
    if args.json:
        print(json.dumps(zd, ensure_ascii=False, indent=2)); return
    base = zd.get("geekBase4Vip") or {}
    print(f"# {base.get('name','')} · {base.get('gender')=='1' and '男' or '女'} · "
          f"{base.get('ageDesc','')} · {base.get('workYearsDesc','')} · {base.get('degreeCategory','')}")
    if base.get("applyStatusContent") or base.get("applyStatus"):
        print(f"  状态: {base.get('applyStatusContent') or base.get('applyStatus')}")
    for e in zd.get("geekExpPos4VipList") or []:
        bits = [_hl(e.get(k)) for k in ("positionName", "salaryDesc", "locationName")]
        print("  期望: " + " | ".join(b for b in bits if b))
    for e in zd.get("geekWorkExp4VipList") or []:
        span = f"{e.get('startDate','')}-{e.get('endDate','') or '至今'}"
        print(f"  经历: {_hl(e.get('company'))} · {_hl(e.get('positionName'))} · {span}")
        if e.get("responsibility") and not args.brief:
            print(f"        {_hl(e.get('responsibility')).replace(chr(10), ' ')[:200]}")
    for e in zd.get("geekEduExp4VipList") or []:
        print(f"  教育: {_hl(e.get('school'))} · {e.get('startDate','')}-{e.get('endDate','') or '至今'}")
    for e in zd.get("geekProjExp4VipList") or []:
        print(f"  项目: {_hl(e.get('name'))} · {_hl(e.get('roleName'))} "
              f"· {e.get('startDate','')}-{e.get('endDate','') or '至今'}")


def cmd_boss_history(args):
    """聊天历史（HistoryChatRequest → batchRunV2）。消息是 IM protobuf，--text 尽力抽取可见文本。"""
    client = _client(args)
    resp = _run(lambda: client.boss_history(args.friend_id, security_id=args.security_id,
                                            friend_source=args.friend_source, count=args.count))
    _check_code(resp, "batchRunV2", client, side="boss")
    sub = BossClient.batch_v2_sub(resp, BossClient.BOSS_HISTORY_PATH)
    _sub_check(sub, "history", client)
    hist = BossClient.parse_history(resp)
    if args.json:
        print(json.dumps(hist, ensure_ascii=False, indent=2)); return
    print(f"# 聊天记录 {len(hist['messages'])} 条（hasMore={hist['hasMore']} minMsgId={hist['minMsgId']}）")
    for i, blob in enumerate(hist["messages"], 1):
        text = _im_text(blob) if (args.text or not args.raw) else ""
        print(f"- #{i} {('' if not text else text[:160])}")


def cmd_boss_greet(args):
    """WRITE — 招聘端给牛人打招呼 (BossCreateFriendRequest). securityId/lid/friend-id/expect-id 都来自
    `boss rcmd <jobId> --json` 的推荐卡；dry-run by default, --confirm actually sends."""
    client = _client(args)
    job_id = args.job_id
    if not job_id:
        jobs = BossClient.parse_boss_jobs(_run(client.boss_jobs))
        if not jobs:
            sys.exit("[✗] 没有职位可挂靠；用 --job-id 指定，或先发布在线职位。")
        job_id = str(jobs[0].get("jobId"))
        print(f"[i] 未给 --job-id，用第一个职位 {job_id}（{jobs[0].get('positionName','')}）", file=sys.stderr)
    payload = {"securityId": args.security_id, "jobId": job_id, "friendId": args.friend_id,
               "expectId": args.expect_id, "lid": args.lid, "from": args.from_,
               "entrance": args.entrance, "greet": args.greet, "languageId": args.language_id}
    if not args.confirm:
        print("[dry-run] 将向 GET /api/zpjob/chat/add/friend 发起打招呼（写操作，会以你的招聘身份发出）：")
        print("  " + json.dumps({k: v for k, v in payload.items() if v is not None}, ensure_ascii=False))
        print("  确认无误后加 --confirm 真正发送。")
        if not args.friend_id or not args.lid:
            print("  [i] 建议带上 --friend-id / --lid（来自 `boss rcmd <jobId> --json` 推荐卡），"
                  "否则服务端可能 code 17 缺少必要参数。")
        return
    resp = _run(lambda: client.boss_add_friend(
        args.security_id, job_id=job_id, friend_id=args.friend_id, expect_id=args.expect_id,
        lid=args.lid, from_=args.from_, entrance=args.entrance, greet=args.greet,
        language_id=args.language_id))
    _check_code(resp, "boss greet", client, side="boss")
    sub = BossClient.batch_v2_sub(resp, BossClient.BOSS_ADD_FRIEND_PATH)
    _sub_check(sub, "boss greet", client)
    rel = (sub.get("zpData") or {}).get("relation") or {}
    print(f"[✓] 招呼已发出 · {sub.get('message') or 'ok'}"
          + (f" · 已与 {rel.get('name')} 建立会话" if rel else ""))
    if args.json:
        print(json.dumps(sub, ensure_ascii=False, indent=2))


def cmd_api(args):
    """Generic escape hatch: call any endpoint. Most recruiter paths must be wrapped in
    batchRunV2 (--batch); whitelisted paths (cities/search/…) work plain and need no --secret."""
    client = _client(args)
    params = {}
    for item in args.param or []:
        if "=" not in item:
            sys.exit(f"参数需形如 KEY=VALUE，收到: {item!r}")
        k, v = item.split("=", 1)
        params[k] = v
    key = client._secret() if args.secret else None
    if args.batch:
        resp = _run(lambda: client.call_batch_v2([{"path": args.path, "params": params}], key=key))
    else:
        resp = _run(lambda: client.call_plain("GET", args.path, params, key=key))
    print(json.dumps(resp, ensure_ascii=False, indent=2))


def _auto_solve_captcha(lc, args):
    """--solve: solve whatever captcha man/machine demands and populate args.validate (+
    challenge/seccode for Geetest). Routes by captchaType:
      4   网易易盾 -> gt3solver.netease_harvest (browser slide solve)
      0/1 Geetest -> CapSolver token mode (handles slide AND 九宫格点选, no browser) if CAPSOLVER_KEY
          is set, else fall back to the browser harvester (opencv slide / 超级鹰 click).
    All solvers are imported lazily so the manual --validate path works without their deps."""
    import json as _json
    from .login import CAPTCHA_GEETEST, CAPTCHA_NETEASE
    mm = lc.man_machine(args.phone)
    if not mm["isMachine"]:
        print("[+] man/machine: isMachine=false，无需验证码"); return
    ct = mm.get("captchaType")
    headless = not args.headful
    if ct == CAPTCHA_NETEASE:
        from gt3solver.netease_harvest import harvest
        print("[*] 网易易盾：启动浏览器求解…（低频使用，勿密集重试）")
        got = harvest(args.session, phone=args.phone, attempts=args.attempts, headless=headless)
        if not (got and got.get("validate")):
            sys.exit("[✗] 网易易盾未解出；重试，或加 --headful 用有头浏览器")
        args.validate = got["validate"]
        print("[✓] 网易易盾已解出 validate")
    elif ct in CAPTCHA_GEETEST:
        sc = _json.loads(mm["startCaptcha"]) if mm.get("startCaptcha") else {}
        gt, chal = sc.get("gt"), sc.get("challenge")
        # 1) prefer CapSolver token mode — one API round-trip, covers slide + 九宫格点选, no browser
        try:
            from gt3solver.capsolver import CapSolver, CapSolverError
            if not (gt and chal):
                raise CapSolverError("man/machine 未返回 gt/challenge")
            print("[*] Geetest：CapSolver token 求解…（slide/九宫格通用）")
            got = CapSolver().solve_geetest_v3(gt, chal)
            if not got.get("validate"):
                raise CapSolverError("CapSolver 未返回 validate")
            args.challenge = got["challenge"] or chal
            args.validate = got["validate"]
            args.seccode = got["seccode"]
            print("[✓] CapSolver 解出 Geetest challenge/validate/seccode")
            return
        except Exception as e:
            print(f"[i] CapSolver 不可用/失败（{e}）；回退浏览器 harvester")
        # 2) fallback: browser harvester (opencv slide, or 九宫格点选 via 超级鹰)
        from gt3solver.browser_harvest import harvest
        print("[*] Geetest GT3：启动浏览器求解…")
        got = harvest(args.session, phone=args.phone, attempts=args.attempts, headless=headless)
        if not (got and got.get("geetest_validate")):
            sys.exit("[✗] Geetest 未解出；建议配置 CAPSOLVER_KEY，或重试/加 --headful")
        args.challenge = got["geetest_challenge"]
        args.validate = got["geetest_validate"]
        args.seccode = got["geetest_seccode"]
        print("[✓] Geetest 已解出（browser）")
    else:
        sys.exit(f"[✗] 未知 captchaType={ct}，无法自动求解")


def cmd_login(args):
    if not os.path.exists(args.session):
        sys.exit(f"session file not found: {args.session}\n"
                 f"login reuses the device profile (client_info/uniqid) from your session file; "
                 f"copy session.example.json -> session.local.json and fill the device profile first.")
    from .login import (LoginClient, LoginError, CaptchaRequired, update_session_token,
                        CAPTCHA_GEETEST, CAPTCHA_NETEASE)
    lc = LoginClient.from_file(args.session)
    identity = {"geek": "0", "boss": "1"}.get(args.identity, args.identity)

    # --solve: auto-solve the behavior captcha in a browser and fill args.validate before sending
    if args.solve and not args.code and not (args.validate or args.seccode):
        _auto_solve_captcha(lc, args)

    try:
        # --code X: code already sent, just log in.  --send: only send.  neither: full interactive.
        if not args.code:
            resp = lc.send_sms_code(
                args.phone, region=args.region, voice=("1" if args.voice else "0"),
                challenge=args.challenge, validate=args.validate, seccode=args.seccode)
            print(f"[+] 验证码已发送到 {args.region} {args.phone}")
            if args.send:
                print(f"    收到后用: boss login --phone {args.phone} --code <验证码>"
                      + (f" --region {args.region}" if args.region != '+86' else ""))
                return
            from .smsbackend import resolve_backend
            args.code = resolve_backend(args.sms_backend)(args.phone)
            if not args.code:
                sys.exit("[✗] 未取得短信验证码（backend 返回空）")

        tokens = lc.code_login(args.phone, args.code, region=args.region, identity_type=identity)
    except CaptchaRequired as e:
        # man/machine 对离设备请求几乎恒返回 isMachine=true —— 这是 BOSS 登录的必经步骤
        # (真机也弹验证码，人手解掉才发码)，与出口 IP 无关。captchaType 决定该解哪种：
        #   0/1 = Geetest GT3 (startCaptcha 带 gt/challenge) —— 解出后传 --challenge/--validate/--seccode
        #   4   = 网易易盾   —— App 的 onValidate 只回填 validate 一个字段，故解出后只需 --validate
        print(f"[风控拦截] {e}")
        if e.captcha_type in CAPTCHA_GEETEST:
            print(f"    Geetest GT3: {e.start_captcha}")
            print("    解出后：boss login --phone <号码> --challenge <c> --validate <v> --seccode <s>")
        elif e.captcha_type == CAPTCHA_NETEASE:
            print(f"    网易易盾: captchaId={e.wy_captcha_id} type={e.wy_captcha_type} (0=滑块/1=无感)")
            print("    解出后：boss login --phone <号码> --validate <网易validate>  (网易只需 validate)")
        else:
            print(f"    未知 captchaType={e.captcha_type}")
        print("    注：isMachine=true 是登录常态，非 IP 封锁；需要一次真正的验证码 token。可加 --solve 自动求解。")
        sys.exit(2)
    except LoginError as e:
        sys.exit(f"[登录失败] {e}")

    update_session_token(args.session, tokens)
    who = "新用户(注册)" if tokens.get("register") else "老用户"
    print(f"[✓] 登录成功 · uid={tokens.get('uid')} · {who} · t2 已写入 {args.session}")
    if not tokens.get("t2"):
        print("[!] 响应里没有 t2，请检查 --json 原始响应:")
        print(json.dumps(tokens.get("raw"), ensure_ascii=False, indent=2))


def _add_search_filters(sp):
    sp.add_argument("--city", help="city name(北京/上海/杭州...) or code; comma-separated for several")
    sp.add_argument("--filter", help="post-filter: keep only jobs whose title/labels/company contain this text")
    sp.add_argument("--page", type=int, default=1, help="(deprecated alias) start page; prefer --pages")
    sp.add_argument("--pages", type=int, default=1, help="fetch pages 1..N (default 1)")
    sp.add_argument("--all", action="store_true", help=f"fetch until exhausted (cap {MAX_ALL_PAGES} pages)")
    sp.add_argument("--sort", default=None, help="综合/最新/距离 or -1/1/2 (default 综合)")
    sp.add_argument("--limit", type=int, help="cap the number of jobs returned")
    sp.add_argument("--filter-param", action="append", metavar="KEY=VAL",
                    help="raw filterParams passthrough (repeatable), e.g. --filter-param salary=407")
    for flag in FILTER_FLAGS:
        sp.add_argument(f"--{flag}", help=f"filterParams.{FILTER_FLAGS[flag]} code (see `boss filters`)")
    sp.add_argument("--format", choices=["text", "json", "csv", "md"], default="text", help="output format")
    sp.add_argument("--json", action="store_true", help="shorthand for --format json")
    sp.add_argument("-o", "--output", help="write to this file instead of stdout")
    sp.add_argument("--token", help="override t2 auth token")


def main(argv=None):
    from .localenv import load_local_env
    load_local_env()   # pull CAPSOLVER_KEY / CHAOJIYING_* / BOSS_SMS_* from a gitignored .env
    ap = argparse.ArgumentParser(prog="boss", description="Off-device BOSS直聘 search + login")
    ap.add_argument("--session", default=DEFAULT_SESSION,
                    help="session json (default: session.local.json / $BOSS_SESSION)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("search", help="search jobs by keyword")
    sp.add_argument("query")
    _add_search_filters(sp)
    sp.set_defaults(func=cmd_search)

    fp = sub.add_parser("filters", help="show available filter/sort codes (probes a live search)")
    fp.add_argument("query", nargs="?", default="Python", help="probe keyword (default Python)")
    fp.add_argument("--city", help="probe city (name or code)")
    fp.add_argument("--token", help="override t2 auth token")
    fp.set_defaults(func=cmd_filters)

    wp = sub.add_parser("whoami", help="show the logged-in account's job expectations (expectId …)")
    wp.add_argument("--json", action="store_true")
    wp.add_argument("--set-expect", action="store_true",
                    help="write the first expect's expectId/encryptExpectId back into the session")
    wp.add_argument("--token", help="override t2 auth token")
    wp.set_defaults(func=cmd_whoami)

    dp = sub.add_parser("detail", help="show a job's detail (JD, boss, company) by securityId")
    dp.add_argument("security_id", help="securityId from a search result")
    dp.add_argument("--lid")
    dp.add_argument("--expect-id")
    dp.add_argument("--json", action="store_true")
    dp.add_argument("--token", help="override t2 auth token")
    dp.set_defaults(func=cmd_detail)

    cp = sub.add_parser("cities", help="dump BOSS city code/name table (from /api/zpCommon/config/city)")
    cp.add_argument("--grep", help="only show cities whose name contains this text")
    cp.add_argument("--json", action="store_true")
    cp.add_argument("--token", help="override t2 auth token")
    cp.set_defaults(func=cmd_cities)

    hp = sub.add_parser("chat", help="打招呼/start a chat with a boss (WRITE; dry-run unless --confirm)")
    hp.add_argument("security_id", help="securityId from a SEARCH result (boss search --json), not detail")
    hp.add_argument("--friend-id", help="the boss's numeric userId (search result's 'userId')")
    hp.add_argument("--job-id")
    hp.add_argument("--lid", help="lid from the same search result")
    hp.add_argument("--expect-id", help="your expectId (see boss whoami); default from the session")
    hp.add_argument("--confirm", action="store_true", help="actually send it (default is dry-run)")
    hp.add_argument("--json", action="store_true")
    hp.add_argument("--token", help="override t2 auth token")
    hp.set_defaults(func=cmd_chat)

    mep = sub.add_parser("me", help="(招聘端) 我的 boss 身份/公司 (batchRunV2 zpboss.app.boss.get.detail)")
    mep.add_argument("--full", action="store_true",
                     help="拉取整批 16 个子请求（动态栏/底部按钮/交付进度/未读职位/安全场景…）")
    mep.add_argument("--json", action="store_true")
    mep.add_argument("--token", help="override t2 auth token")
    mep.set_defaults(func=cmd_bossme)

    jp = sub.add_parser("jobs", help="(招聘端) 我发布的职位 (jobId 供 rcmd 使用)")
    jp.add_argument("--json", action="store_true")
    jp.add_argument("--token", help="override t2 auth token")
    jp.set_defaults(func=cmd_bossjobs)

    rp = sub.add_parser("rcmd", help="(招聘端) 某职位的推荐牛人 (需在线职位)")
    rp.add_argument("job_id", help="jobId from `boss jobs`")
    rp.add_argument("--page", type=int, default=1)
    rp.add_argument("--page-size", type=int, default=15)
    rp.add_argument("--sort-type", type=int, default=0)
    rp.add_argument("--filter-params", help="raw filterParams (JSON string) passthrough")
    rp.add_argument("--json", action="store_true")
    rp.add_argument("--token", help="override t2 auth token")
    rp.set_defaults(func=cmd_bossrcmd)

    ctp = sub.add_parser("contacts", help="(招聘端) 沟通列表（分页+服务端筛选；卡片含 greet 所需 id）")
    ctp.add_argument("--page", type=int, default=1, help="起始页（每页 10 条）")
    ctp.add_argument("--pages", type=int, default=1, help="连续抓几页（去重）")
    ctp.add_argument("--source", type=int, default=2, help="2=全部（默认），3=按职位（需 --job）")
    ctp.add_argument("--job", help="encryptJobId（source=3 用；默认取第一个职位）")
    ctp.add_argument("--sort", type=int, default=0, help="0=按开始聊天时间倒序（默认），1=正序")
    ctp.add_argument("--apply-status", help="求职状态：701/随时到岗 · 702/暂不考虑 · 703/考虑机会 · 704/月内到岗")
    ctp.add_argument("--within", help="开始沟通时间：7 / 30 / 90（天）")
    ctp.add_argument("--all", action="store_true", help="改用 getBaseInfoList：全部关系（88 条，含 chatStatus）")
    ctp.add_argument("--limit", type=int, help="--all 模式下只显示前 N 条")
    ctp.add_argument("--json", action="store_true")
    ctp.add_argument("--token", help="override t2 auth token")
    ctp.set_defaults(func=cmd_boss_contacts)

    rsp = sub.add_parser("resume", help="(招聘端) 看牛人简历（securityId 来自 contacts/rcmd）")
    rsp.add_argument("security_id")
    rsp.add_argument("--brief", action="store_true", help="不打印工作经历的责任描述")
    rsp.add_argument("--json", action="store_true")
    rsp.add_argument("--token", help="override t2 auth token")
    rsp.set_defaults(func=cmd_boss_resume)

    hsp = sub.add_parser("history", help="(招聘端) 看与某牛人的聊天记录（--text 抽取可见文本）")
    hsp.add_argument("friend_id", help="牛人 geekId/friendId（contacts 卡片）")
    hsp.add_argument("--security-id", help="同一张卡片的 securityId（服务端用于鉴权）")
    hsp.add_argument("--friend-source", type=int, default=0)
    hsp.add_argument("--count", type=int, default=20, help="拉取条数（默认 20）")
    hsp.add_argument("--text", action="store_true", help="尽力抽取消息可见文本（protobuf 无 schema）")
    hsp.add_argument("--raw", action="store_true", help="只列条数，不抽取文本")
    hsp.add_argument("--json", action="store_true")
    hsp.add_argument("--token", help="override t2 auth token")
    hsp.set_defaults(func=cmd_boss_history)

    gp = sub.add_parser("greet", help="(招聘端) 给牛人打招呼（写操作，dry-run 除非 --confirm）")
    gp.add_argument("security_id", help="牛人卡片的 securityId（来自 `boss rcmd --json`）")
    gp.add_argument("--job-id", help="挂靠职位 jobId；默认取第一个职位")
    gp.add_argument("--friend-id", help="牛人的数字 userId（推荐卡里的 userId）")
    gp.add_argument("--expect-id", help="牛人的 expectId（推荐卡里）")
    gp.add_argument("--lid", help="推荐卡的 lid")
    gp.add_argument("--from", dest="from_", help="来源标识（可选）")
    gp.add_argument("--entrance", type=int, default=77, help="入口码，默认 77 = F1 推荐卡")
    gp.add_argument("--greet", help="打招呼语（可选）")
    gp.add_argument("--language-id", help="外语/语音场次 id（可选）")
    gp.add_argument("--confirm", action="store_true", help="真正发送（默认 dry-run）")
    gp.add_argument("--json", action="store_true")
    gp.add_argument("--token", help="override t2 auth token")
    gp.set_defaults(func=cmd_boss_greet)

    rp2 = sub.add_parser("api", help="原始调用任意接口（招聘端多数要用 --batch 包 batchRunV2）")
    rp2.add_argument("path", help="e.g. /api/zpitem/boss/search")
    rp2.add_argument("param", nargs="*", metavar="KEY=VAL", help="query params")
    rp2.add_argument("--batch", action="store_true", help="wrap in /api/batch/batchRunV2")
    rp2.add_argument("--secret", action="store_true", help="sign with secretKey (non-whitelisted plain paths)")
    rp2.add_argument("--token", help="override t2 auth token")
    rp2.set_defaults(func=cmd_api)

    lp = sub.add_parser("login", help="SMS-code login -> obtain t2 into the session file")
    lp.add_argument("--phone", required=True, help="mobile number (without region code)")
    lp.add_argument("--region", default="+86", help="region code, e.g. +86 / +63 (default +86)")
    lp.add_argument("--code", help="the SMS code (skip sending; just exchange code->t2)")
    lp.add_argument("--send", action="store_true", help="only send the SMS code, then exit (two-step flow)")
    lp.add_argument("--identity", default="geek", help="geek|boss (or 0|1); default geek")
    lp.add_argument("--voice", action="store_true", help="request a voice call code instead of SMS")
    lp.add_argument("--challenge", help="solved Geetest geetest_challenge (to pass man/machine)")
    lp.add_argument("--validate", help="solved captcha validate (Geetest geetest_validate, or 网易易盾 validate)")
    lp.add_argument("--seccode", help="solved Geetest geetest_seccode")
    lp.add_argument("--solve", action="store_true",
                    help="auto-solve the man/machine captcha (网易易盾/Geetest); needs the captcha extras")
    lp.add_argument("--headful", action="store_true", help="show the browser while --solve (default headless)")
    lp.add_argument("--attempts", type=int, default=3, help="--solve captcha attempts (default 3)")
    lp.add_argument("--sms-backend", default=None,
                    help="where the SMS code comes from: manual(default)|env|http|module:func "
                         "(see bosscli/smsbackend.py; also $BOSS_SMS_BACKEND)")
    lp.set_defaults(func=cmd_login)

    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
