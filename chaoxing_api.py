import asyncio
import re
import time
from typing import Optional, Literal
from nekro_agent.api.plugin import dynamic_import_pkg

# 动态导入依赖
httpx = dynamic_import_pkg("httpx>=0.28.1")

from .cipher import AESCipher
from .decoder import (
    decode_course_list,
    decode_course_point,
    decode_course_folder,
    decode_course_card,
    decode_questions_info,
)

def _safe_json(resp) -> dict:
    """安全解析 JSON 响应，空响应或解析失败返回空字典"""
    try:
        return resp.json()
    except Exception:
        return {}

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Connection": "keep-alive",
    "DNT": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "sec-ch-ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

VIDEO_HEADERS = {
    "Referer": "https://mooc1.chaoxing.com/ananas/modules/video/index.html?v=2025-0725-1842",
}
AUDIO_HEADERS = {
    "Referer": "https://mooc1.chaoxing.com/ananas/modules/audio/index_new.html?v=2025-0725-1842",
}

def get_timestamp():
    return str(int(time.time() * 1000))

class AsyncRateLimiter:
    """异步速率限制器"""
    def __init__(self, call_interval: float):
        self.last_call = time.time()
        self.lock = asyncio.Lock()
        self.call_interval = call_interval

    async def limit_rate(self, random_time: bool = False, random_min: float = 0.0, random_max: float = 1.0):
        async with self.lock:
            import random
            if random_time:
                wait_time = random.uniform(random_min, random_max)
                await asyncio.sleep(wait_time)
            now = time.time()
            time_elapsed = now - self.last_call
            if time_elapsed <= self.call_interval:
                await asyncio.sleep(self.call_interval - time_elapsed)
                self.last_call = time.time()
                return

            self.last_call = now
            return

class AsyncChaoxing:
    """超星学习通异步 API 封装"""

    def __init__(self, cookies: dict = None):
        import httpx
        self.client = httpx.AsyncClient(
            headers=HEADERS,
            cookies=cookies,
            timeout=30.0,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=10),
            transport=httpx.AsyncHTTPTransport(retries=5)
        )
        # 增加网络请求重试机制的简单包装
        _orig_get = self.client.get
        _orig_post = self.client.post
        
        async def _safe_get(*args, **kwargs):
            import asyncio
            from nekro_agent.api.core import logger
            for attempt in range(5):
                try:
                    return await _orig_get(*args, **kwargs)
                except httpx.RequestError as e:
                    if attempt < 4:
                        logger.warning(f"[AsyncChaoxing] GET 请求超时/失败，正在重试 ({attempt+1}/5): {e}")
                        await asyncio.sleep(2 * (attempt + 1))
                    else:
                        logger.error(f"[AsyncChaoxing] GET 请求彻底失败(已重试5次): {e}")
                        raise

        async def _safe_post(*args, **kwargs):
            import asyncio
            from nekro_agent.api.core import logger
            for attempt in range(5):
                try:
                    return await _orig_post(*args, **kwargs)
                except httpx.RequestError as e:
                    if attempt < 4:
                        logger.warning(f"[AsyncChaoxing] POST 请求超时/失败，正在重试 ({attempt+1}/5): {e}")
                        await asyncio.sleep(2 * (attempt + 1))
                    else:
                        logger.error(f"[AsyncChaoxing] POST 请求彻底失败(已重试5次): {e}")
                        raise
                        
        self.client.get = _safe_get
        self.client.post = _safe_post
        
        self.cipher = AESCipher()
        self.uid = None
        self.tiku = None
        self.config = {}
        
        self.rate_limiter = AsyncRateLimiter(0.5)
        self.video_log_limiter = AsyncRateLimiter(2.0)

    async def close(self):
        await self.client.aclose()
        
    async def limit_rate(self):
        await self.rate_limiter.limit_rate()

    async def login(self, username: str, password: str) -> dict:
        self.account = {"username": username, "password": password}
        _url = "https://passport2.chaoxing.com/fanyalogin"
        _data = {
            "fid": "-1",
            "uname": self.cipher.encrypt(username),
            "password": self.cipher.encrypt(password),
            "refer": "https%3A%2F%2Fi.chaoxing.com",
            "t": True,
            "forbidotherlogin": 0,
            "validate": "",
            "doubleFactorLogin": 0,
            "independentId": 0,
        }
        
        resp = await self.client.post(_url, data=_data)
        res_json = _safe_json(resp)
        if res_json.get("status") == True:
            return {"status": True, "msg": "登录成功"}
        else:
            return {"status": False, "msg": str(res_json.get("msg2"))}

    async def _validate_cookie_session(self) -> bool:
        _url = "https://sso.chaoxing.com/apis/login/userLogin4Uname.do"
        resp = await self.client.get(_url)
        return _safe_json(resp).get("result") == 1

    async def get_uid(self) -> str:
        if self.uid:
            return self.uid
        _url = "https://sso.chaoxing.com/apis/login/userLogin4Uname.do"
        resp = await self.client.get(_url)
        if _safe_json(resp).get("result") == 1:
            self.uid = str(_safe_json(resp)["msg"]["puid"])
            return self.uid
        return None

    async def get_course_list(self) -> list[dict]:
        from nekro_agent.api.core import logger
        _url = "https://mooc2-ans.chaoxing.com/mooc2-ans/visit/courselistdata"
        _data = {"courseType": 1, "courseFolderId": 0, "query": "", "superstarClass": 0}
        _headers = {
            "Referer": "https://mooc2-ans.chaoxing.com/mooc2-ans/visit/interaction?moocDomain=https://mooc1-1.chaoxing.com/mooc-ans",
        }
        logger.info("[get_course_list] 正在请求主课程列表...")
        _resp = await self.client.post(_url, headers=_headers, data=_data)
        logger.info(f"[get_course_list] 主课程列表响应: status={_resp.status_code}, len={len(_resp.text)}")
        course_list = await asyncio.to_thread(decode_course_list, _resp.text)
        logger.info(f"[get_course_list] 主目录解析到 {len(course_list)} 门课程")

        _interaction_url = "https://mooc2-ans.chaoxing.com/mooc2-ans/visit/interaction"
        logger.info("[get_course_list] 正在请求课程文件夹列表...")
        _interaction_resp = await self.client.get(_interaction_url)
        logger.info(f"[get_course_list] 文件夹列表响应: status={_interaction_resp.status_code}, len={len(_interaction_resp.text)}")
        course_folder = await asyncio.to_thread(decode_course_folder, _interaction_resp.text)
        logger.info(f"[get_course_list] 发现 {len(course_folder)} 个课程文件夹")
        
        for fi, folder in enumerate(course_folder):
            logger.info(f"[get_course_list] 正在请求文件夹 {fi+1}/{len(course_folder)}: {folder.get('rename', folder['id'])}")
            _data = {
                "courseType": 1,
                "courseFolderId": folder["id"],
                "query": "",
                "superstarClass": 0,
            }
            _resp = await self.client.post(_url, data=_data)
            folder_courses = await asyncio.to_thread(decode_course_list, _resp.text)
            logger.info(f"[get_course_list] 文件夹 {fi+1} 解析到 {len(folder_courses)} 门课程")
            course_list += folder_courses
            
        logger.info(f"[get_course_list] 总计获取 {len(course_list)} 门课程")
        return course_list

    async def get_course_point(self, _courseid, _clazzid, _cpi):
        _url = f"https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/studentcourse?courseid={_courseid}&clazzid={_clazzid}&cpi={_cpi}&ut=s"
        _resp = await self.client.get(_url)
        return await asyncio.to_thread(decode_course_point, _resp.text)

    async def get_job_list(self, course: dict, point: dict) -> tuple[list[dict], dict]:
        await self.limit_rate()
        job_list = []
        job_info = {}
        cards_params = {
            "clazzid": course["clazzId"],
            "courseid": course["courseId"],
            "knowledgeid": point["id"],
            "ut": "s",
            "cpi": course["cpi"],
            "v": "2025-0424-1038-3",
            "mooc2": 1
        }

        seen_jobids = set()
        for _possible_num in "0123456":
            cards_params.update({"num": _possible_num})
            _resp = await self.client.get("https://mooc1.chaoxing.com/mooc-ans/knowledge/cards", params=cards_params)
            if _resp.status_code != 200:
                continue

            _job_list, _job_info = await asyncio.to_thread(decode_course_card, _resp.text)
            if _job_info.get("notOpen", False):
                return [], _job_info

            for _j in _job_list:
                jid = _j.get("jobid")
                if jid and jid not in seen_jobids:
                    seen_jobids.add(jid)
                    job_list.append(_j)
                    
            job_info.update(_job_info)

        if not job_list:
            await self.study_emptypage(course, point)

        return job_list, job_info

    async def study_emptypage(self, _course, point):
        _url = f"https://mooc1.chaoxing.com/mooc-ans/knowledge/cards?clazzid={_course['clazzId']}&courseid={_course['courseId']}&knowledgeid={point['id']}&num=0&v=20160407-3&cpi={_course['cpi']}&isdaliangb=0"
        await self.client.get(_url)
        _url = f"https://mooc1.chaoxing.com/mooc-ans/mycourse/studentstudyAjax?courseId={_course['courseId']}&clazzid={_course['clazzId']}&chapterId={point['id']}&cpi={_course['cpi']}&verificationcode="
        await self.client.get(_url)

    async def process_course(self, course: dict, handle, report_func):
        """处理整个课程的大循环"""
        from nekro_agent.api.core import logger
        logger.info(f"[process_course] 开始处理课程: {course['title']}")
        point_list_data = await self.get_course_point(course["courseId"], course["clazzId"], course["cpi"])
        points = point_list_data.get("points", [])
        logger.info(f"[process_course] 获取到 {len(points)} 个章节")
        
        # 针对并发，此框架简单先按顺序进行，若需并发可重构这里用 asyncio.gather
        for i, point in enumerate(points):
            if handle.is_cancelled:
                break
                
            percent = int((i / len(points)) * 100)
            await report_func(f"[{course['title']}] 正在检查: {point['title']}", percent, current_chapter=point['title'])

            if point.get("has_finished"):
                continue

            jobs, job_info = await self.get_job_list(course, point)
            logger.info(f"[process_course] 章节 '{point['title']}' 找到 {len(jobs)} 个任务点")
            
            if job_info.get("notOpen", False):
                logger.info(f"[process_course] 章节 '{point['title']}' 未开放，跳过")
                continue
                
            for job in jobs:
                if handle.is_cancelled:
                    break
                
                try:
                    job_type = job.get("type")
                    logger.info(f"[process_course] 处理任务点: type={job_type} jobid={job.get('jobid', 'N/A')}")
                    if job_type == "video":
                        await report_func(f"[{course['title']}] 开始视频: {point['title']} (ID: {job['jobid']})", percent, current_chapter=point['title'])
                        result = await self.study_video(course, job, job_info, speed=self.config.get("speed", 1.0), _type="Video", report_func=report_func, course_percent=percent)
                        if not result:
                            # 参考原版：视频失败则尝试音频模式
                            await report_func(f"[{course['title']}] 视频失败，尝试音频模式: {point['title']}", percent, current_chapter=point['title'])
                            await self.study_video(course, job, job_info, speed=self.config.get("speed", 1.0), _type="Audio", report_func=report_func, course_percent=percent)
                    elif job_type == "document":
                        await report_func(f"[{course['title']}] 开始文档: {point['title']}", percent, current_chapter=point['title'])
                        await self.study_document(course, job)
                    elif job_type == "workid":
                        await report_func(f"[{course['title']}] 开始测验: {point['title']}", percent, current_chapter=point['title'])
                        await self.study_work(course, job, job_info, report_func=report_func)
                    elif job_type == "read":
                        await report_func(f"[{course['title']}] 开始阅读任务: {point['title']}", percent, current_chapter=point['title'])
                        await self.study_read(course, job, job_info)
                except Exception as e:
                    logger.error(f"[process_course] 任务 {job.get('jobid')} 发生异常，已跳过: {e}")
                    import traceback
                    logger.debug(traceback.format_exc())
                    continue
                
            # 章节完成通知 AI (当 notify_level 为 Chapter 时才通知)
            if self.config.get("notify_level", "Chapter") == "Chapter":
                await handle.notify_agent(
                    f"✅ 章节完成: {course['title']} - {point['title']}", 
                    trigger=True
                )
            await asyncio.sleep(1.0)
            
    def get_enc(self, clazzId, jobid, objectId, playingTime, duration, userid):
        import hashlib
        return hashlib.md5(
            f"[{clazzId}][{userid}][{jobid}][{objectId}][{playingTime * 1000}][d_yHJ!$pdA~5][{duration * 1000}][0_{duration}]".encode()
        ).hexdigest()

    def get_fid(self):
        # 简单从 cookie 中取 fid，或者使用默认的
        # 超星大部分视频接口需要 fid
        fid = self.client.cookies.get("fid", domain=".chaoxing.com")
        return fid if fid else "-1"

    async def video_progress_log(
        self,
        _course,
        _job,
        _job_info,
        _dtoken,
        _duration,
        _playingTime,
        _type: str = "Video",
        headers: dict = None,
    ) -> tuple[bool, int]:
        
        await self.video_log_limiter.limit_rate(random_time=True, random_max=2.0)
        
        if headers is None:
            headers = VIDEO_HEADERS
        
        uid = await self.get_uid()
        enc = self.get_enc(_course["clazzId"], _job["jobid"], _job["objectid"], _playingTime, _duration, uid)
        
        params = {
            "clazzId": _course["clazzId"],
            "playingTime": _playingTime,
            "duration": _duration,
            "clipTime": f"0_{_duration}",
            "objectId": _job["objectid"],
            "otherInfo": _job["otherinfo"],
            "courseId": _course["courseId"],
            "jobid": _job["jobid"],
            "userid": uid,
            "isdrag": "3",
            "view": "pc",
            "enc": enc,
            "dtype": _type
        }

        _url = (
            f"https://mooc1.chaoxing.com/mooc-ans/multimedia/log/a/"
            f"{_course['cpi']}/"
            f"{_dtoken}"
        )

        face_capture_enc = _job.get("videoFaceCaptureEnc")
        att_duration = _job.get("attDuration")
        att_duration_enc = _job.get("attDurationEnc")

        if face_capture_enc:
            params["videoFaceCaptureEnc"] = face_capture_enc
        if att_duration:
            params["attDuration"] = att_duration
        if att_duration_enc:
            params["attDurationEnc"] = att_duration_enc

        rt = _job.get('rt')
        if not rt:
            rt_search = re.search(r"-rt_([1d])", _job.get('otherinfo', ''))
            if rt_search:
                rt_char = rt_search.group(1)
                rt = "0.9" if rt_char == "d" else "1"

        if rt:
            params.update({"rt": rt, "_t": get_timestamp()})
            resp = await self.client.get(_url, params=params, headers=headers)
        else:
            for rt in [0.9, 1]:
                params.update({"rt": rt, "_t": get_timestamp()})
                resp = await self.client.get(_url, params=params, headers=headers)
                if resp.status_code == 200:
                    return _safe_json(resp).get("isPassed", False), 200
                elif resp.status_code == 403:
                    pass
                else:
                    break

        if resp.status_code == 200:
            return _safe_json(resp).get("isPassed", False), 200
        return False, resp.status_code

    async def _refresh_video_status(self, job: dict, _type: Literal["Video", "Audio"]) -> Optional[dict]:
        await self.limit_rate()
        info_url = (
            f"https://mooc1.chaoxing.com/ananas/status/{job['objectid']}?"
            f"k={self.get_fid()}&flag=normal"
        )
        try:
            resp = await self.client.get(info_url)
            if resp.status_code != 200:
                return None
            data = _safe_json(resp)
            if data.get("status") == "success":
                return data
        except Exception:
            return None
        return None

    async def study_video(self, _course, _job, _job_info, speed: float = 1.0, _type: Literal["Video", "Audio"] = "Video", report_func=None, course_percent: int = 0):
        headers = VIDEO_HEADERS if _type == "Video" else AUDIO_HEADERS
        _info_url = f"https://mooc1.chaoxing.com/ananas/status/{_job['objectid']}?k={self.get_fid()}&flag=normal"
        resp = await self.client.get(_info_url, headers=headers)
        _video_info = _safe_json(resp)

        if _video_info.get("status") != "success":
            return False

        _dtoken = _video_info["dtoken"]
        duration = int(_video_info["duration"])
        play_time = int(_job.get("playTime", 0)) // 1000
        last_iter = time.time()
        job_name = _job.get("name", _job.get("jobid", "unknown"))
        
        from nekro_agent.api.core import logger
        logger.info(f"[study_video] 开始: {job_name} | 总时长={duration}s 已播={play_time}s")
        
        # 初始检查：参考原版，先发 play_time 再发 duration——第一次注册连接，第二次尝试瞬间完成
        passed, state = await self.video_progress_log(_course, _job, _job_info, _dtoken, duration, play_time, _type, headers=headers)
        passed, state = await self.video_progress_log(_course, _job, _job_info, _dtoken, duration, duration, _type, headers=headers)
        if passed:
            logger.info(f"[study_video] 瞬间完成: {job_name}")
            return True
            
        import random
        last_log_time = 0
        wait_time = int(random.uniform(30, 90))
        forbidden_retry = 0
        end_retry_count = 0  # 满进度重试计数（仅用于日志和定期刷新 dtoken）

        while not passed:
            play_time_int = int(play_time)
            is_at_end = play_time_int >= duration

            if play_time_int - last_log_time >= wait_time or is_at_end:
                # 满进度时：与参考项目一致，持续重试直到服务器确认通过
                if is_at_end:
                    end_retry_count += 1
                    # 满进度重试时增加随机等待间隔，避免刷请求
                    await asyncio.sleep(random.uniform(3, 8))
                    # 每 5 次刷新一次 dtoken，防止 token 过期
                    if end_retry_count % 5 == 0:
                        logger.info(f"[study_video] 满进度重试第{end_retry_count}次，尝试刷新 dtoken: {job_name}")
                        refreshed_meta = await self._refresh_video_status(_job, _type)
                        if refreshed_meta:
                            _dtoken = refreshed_meta.get("dtoken", _dtoken)

                passed, state = await self.video_progress_log(_course, _job, _job_info, _dtoken, duration, play_time_int, _type, headers=headers)
                if state == 403:
                    if forbidden_retry >= 2:
                        return False
                    forbidden_retry += 1
                    await asyncio.sleep(random.uniform(2, 4))
                    refreshed_meta = await self._refresh_video_status(_job, _type)
                    if refreshed_meta:
                        _dtoken = refreshed_meta.get("dtoken", _dtoken)
                        duration = refreshed_meta.get("duration", duration)
                        play_time = refreshed_meta.get("playTime", play_time)
                        continue
                elif not passed and state != 200:
                    return False

                wait_time = int(random.uniform(30, 90))
                last_log_time = play_time_int

            dt = (time.time() - last_iter) * speed
            last_iter = time.time()
            play_time = min(duration, play_time + dt)
            
            # 每次循环都报告视频播放进度
            if report_func and duration > 0:
                video_pct = int((play_time / duration) * 100)
                mins_done = int(play_time) // 60
                secs_done = int(play_time) % 60
                mins_total = int(duration) // 60
                secs_total = int(duration) % 60
                prog_str = f"{mins_done:02d}:{secs_done:02d}/{mins_total:02d}:{secs_total:02d} ({video_pct}%)"
                
                await report_func(
                    f"▶️ {job_name} [{prog_str}]",
                    course_percent,
                    current_video_progress=prog_str,
                )
                
            await asyncio.sleep(1.0)  # 与参考项目 THRESHOLD=1 保持一致

        logger.info(f"[study_video] 完成: {job_name}")

        return True

    async def study_document(self, _course, _job):
        _url = f"https://mooc1.chaoxing.com/ananas/job/document?jobid={_job['jobid']}&knowledgeid={re.findall(r'nodeId_(.*?)-', _job['otherinfo'])[0]}&courseid={_course['courseId']}&clazzid={_course['clazzId']}&jtoken={_job['jtoken']}&_dc={get_timestamp()}"
        _resp = await self.client.get(_url)
        return _resp.status_code == 200
        
    async def study_read(self, _course, _job, _job_info):
        _resp = await self.client.get(
            url="https://mooc1.chaoxing.com/ananas/job/readv2",
            params={
                "jobid": _job["jobid"],
                "knowledgeid": _job_info["knowledgeid"],
                "jtoken": _job["jtoken"],
                "courseid": _course["courseId"],
                "clazzid": _course["clazzId"],
            },
        )
        return _resp.status_code == 200

    async def study_work(self, _course, _job, _job_info, report_func=None):
        from nekro_agent.api.core import logger
        if self.tiku is not None and getattr(self.tiku, "DISABLE", False):
            return True

        def random_answer(q_options: str, q_type: str) -> str:
            import random
            answer = ""
            if not q_options:
                return answer

            if q_type == "multiple":
                _op_list = [o for o in re.split(r'[\n,，|\r\t#*\-_+@~/\.\&、]', q_options) if o.strip()]
                if not _op_list:
                    return answer
                available_options = len(_op_list)
                select_count = 0
                if available_options <= 1:
                    select_count = available_options
                else:
                    max_possible = min(4, available_options)
                    min_possible = min(2, available_options)
                    weights_map = {
                        2: [1.0],
                        3: [0.3, 0.7],
                        4: [0.1, 0.5, 0.4],
                        5: [0.1, 0.4, 0.3, 0.2],
                    }
                    weights = weights_map.get(max_possible, [0.3, 0.4, 0.3])
                    possible_counts = list(range(min_possible, max_possible + 1))
                    weights = weights[:len(possible_counts)]
                    weights_sum = sum(weights)
                    if weights_sum > 0:
                        weights = [w / weights_sum for w in weights]
                    select_count = random.choices(possible_counts, weights=weights, k=1)[0]
                selected_options = random.sample(_op_list, select_count) if select_count > 0 else []
                for option in selected_options:
                    answer += option[:1]
                answer = "".join(sorted(answer))
            elif q_type == "single":
                answer = random.choice(q_options.split("\n"))[:1]
            elif q_type == "judgement":
                answer = "true" if random.choice([True, False]) else "false"
            return answer

        def multi_cut(answer: str):
            res = [o for o in re.split(r'[\n,，|\r\t#*\-_+@~/\.\&、\s]', answer) if o.strip()]
            if not res:
                return None
            return res

        def clean_res(res):
            cleaned_res = []
            if isinstance(res, str):
                res = [res]
            for c in res:
                cleaned = re.sub(r'^[A-Za-z]|[.,!?;:，。！？；：]', '', c) if len(c) > 1 else c
                cleaned_res.append(cleaned.strip())
            return cleaned_res

        def is_subsequence(a, o):
            iter_o = iter(o)
            return all(c in iter_o for c in a)

        _url = "https://mooc1.chaoxing.com/mooc-ans/api/work"
        _params = {
            "api": "1",
            "workId": _job["jobid"].replace("work-", ""),
            "jobid": _job["jobid"],
            "originJobId": _job["jobid"],
            "needRedirect": "true",
            "skipHeader": "true",
            "knowledgeid": str(_job_info["knowledgeid"]),
            "ktoken": _job_info["ktoken"],
            "cpi": _job_info["cpi"],
            "ut": "s",
            "clazzId": _course["clazzId"],
            "type": "",
            "enc": _job["enc"],
            "mooc2": "1",
            "courseid": _course["courseId"],
        }
        
        retries = 0
        max_retries = 3
        questions = {}
        
        while retries < max_retries:
            try:
                resp = await self.client.get(_url, params=_params)
                if "教师未创建完成该测验" in resp.text:
                    if report_func: await report_func(f"[{_course['title']}] 教师未创建完成测验", 100)
                    return True
                
                questions = await asyncio.to_thread(decode_questions_info, resp.text)
                if resp.status_code == 200 and questions.get("questions"):
                    break
                logger.warning(f"无效响应, 重试中... ({retries + 1}/{max_retries})")
            except Exception as e:
                logger.warning(f"请求失败: {str(e)[:50]}, 重试中... ({retries + 1}/{max_retries})")
            retries += 1
            await asyncio.sleep(1 * (2 ** retries))
            
        if not questions or not questions.get("questions"):
            if report_func: await report_func(f"[{_course['title']}] 读取测验失败或题库为空", 100)
            return False

        total_questions = len(questions["questions"])
        found_answers = 0
        
        has_ai = self.tiku is not None
        mode_label = "AI答题" if has_ai else "随机答题(无AI模型组)"
        if report_func: await report_func(f"[{_course['title']}] 正在{mode_label}，共 {total_questions} 题", 0)

        for i, q in enumerate(questions["questions"]):
            await asyncio.sleep(0.5)
            answer = ""
            qid = q["id"]
            
            # 记录题目和选项用于调试
            logger.info(f"[study_work] 题目 {i+1}/{total_questions} (ID:{qid}): type={q['type']}, title={q['title'][:30]}...")
            logger.info(f"[study_work] 选项：{q['options'][:100]}...")
            
            res = None
            if has_ai:
                try:
                    res = await self.tiku.query(q["title"], q["options"], q["type"])
                    res = res["answer"] if res and res.get("success") else None
                    logger.info(f"[study_work] AI 答案：{res}")
                except Exception as e:
                    logger.warning(f"[study_work] AI 答题失败：{e}")
                    res = None
            
            if not res:
                logger.info(f"[study_work] 无 AI 答案，使用随机选择")
                answer = random_answer(q["options"], q["type"])
                q[f'answerSource{qid}'] = "random"
            else:
                if q["type"] == "multiple":
                    options_list = multi_cut(q["options"])
                    res_list = multi_cut(res)
                    logger.info(f"[study_work] 多选切割：options={options_list}, res={res_list}")
                    if res_list is not None and options_list is not None:
                        for _a in clean_res(res_list):
                            for o in options_list:
                                if is_subsequence(_a, o):
                                    answer += o[:1]
                                    break
                        answer = "".join(sorted(answer))
                elif q["type"] == "single":
                    options_list = multi_cut(q["options"])
                    logger.info(f"[study_work] 单选切割：options={options_list}, res={res}")
                    if options_list is not None:
                        t_res = clean_res(res)
                        logger.info(f"[study_work] clean_res 后：t_res={t_res}")
                        for o in options_list:
                            if t_res and is_subsequence(t_res[0], o):
                                answer = o[:1]
                                logger.info(f"[study_work] 匹配成功：{t_res[0]} in {o} -> answer={answer}")
                                break
                        if not answer:
                            logger.warning(f"[study_work] 匹配失败，使用随机选择")
                elif q["type"] == "judgement":
                    answer = "true" if ("正确" in res or "true" in res.lower()) else "false"
                elif q["type"] == "completion":
                    answer = res if isinstance(res, str) else "".join(res)
                else:
                    logger.warning(f"[study_work] 未知题型：{q['type']}，使用原始答案")
                    answer = res

                if not answer:
                    logger.info(f"[study_work] 答案为空，使用随机选择")
                    answer = random_answer(q["options"], q["type"])
                    q[f'answerSource{qid}'] = "random"
                else:
                    q[f'answerSource{qid}'] = "cover"
                    found_answers += 1
            
            logger.info(f"[study_work] 最终答案：{answer}")
            
            q["answerField"][f'answer{q["id"]}'] = answer
            if report_func: await report_func(f"[{_course['title']}] 答题 ({i+1}/{total_questions}): {answer}", int((i+1)/total_questions*100))

        # 计算覆盖率并决定是提交还是保存
        cover_rate = (found_answers / total_questions) * 100 if total_questions > 0 else 0
        logger.info(f"章节检测题库覆盖率： {cover_rate:.0f}%")
        
        submit_threshold = self.config.get("ai_submit_threshold", 80)
        
        if cover_rate >= submit_threshold:
            questions["pyFlag"] = "" 
            action_name = "提交"
        else:
            questions["pyFlag"] = "1"
            action_name = "保存"
            logger.info(f"章节检测题库覆盖率低于 {submit_threshold}%，不予自动提交，仅做保存处理")
            if report_func: await report_func(f"[{_course['title']}] 覆盖率不足{submit_threshold}%，当前仅保存答案", 100)
        
        # 组建提交表单 - 参考 chaoxing 项目 base.py:838-854
        if questions.get("pyFlag") == "1":
            # 保存模式：只填充有答案的题目，没有答案的留空
            for q in questions["questions"]:
                questions.update({
                    f'answer{q["id"]}': q["answerField"][f'answer{q["id"]}'] if q.get(f'answerSource{q["id"]}') == "cover" else '',
                    f'answertype{q["id"]}': q["answerField"][f'answertype{q["id"]}'],
                })
        else:
            # 提交模式：填充所有答案
            for q in questions["questions"]:
                questions.update({
                    f'answer{q["id"]}': q["answerField"][f'answer{q["id"]}'],
                    f'answertype{q["id"]}': q["answerField"][f'answertype{q["id"]}'],
                })
            
        del questions["questions"]
        
        post_url = "https://mooc1.chaoxing.com/mooc-ans/work/addStudentWorkNew"
        post_headers = {
            "Host": "mooc1.chaoxing.com",
            "sec-ch-ua-platform": '"Windows"',
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "sec-ch-ua": '"Microsoft Edge";v="129", "Not=A?Brand";v="8", "Chromium";v="129"',
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "sec-ch-ua-mobile": "?0",
            "Origin": "https://mooc1.chaoxing.com",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6,ja;q=0.5"
        }
        
        logger.info(f"[study_work] 准备 {action_name} 测验，共 {total_questions} 题。使用URL: {post_url}")
        logger.info(f"[study_work] pyFlag={questions.get('pyFlag')!r}")
        answer_keys = [k for k in questions.keys() if k.startswith('answer') and k != 'answerwqbid']
        logger.info(f"[study_work] 答案数量：{len(answer_keys)}, 示例：{answer_keys[:3]}")
        for ak in answer_keys[:3]:
            logger.info(f"[study_work] {ak}={questions.get(ak, 'N/A')!r}")
        
        resp_post = await self.client.post(post_url, data=questions, headers=post_headers)

        # 输出完整响应用于调试
        logger.info(f"[study_work] 响应状态码：{resp_post.status_code}")
        resp_text = resp_post.text[:500] if len(resp_post.text) > 500 else resp_post.text
        logger.info(f"[study_work] 响应内容：{resp_text}")

        
        if resp_post.status_code == 200:
            res_json = _safe_json(resp_post)
            if res_json.get("status"):
                if report_func: await report_func(f"[{_course['title']}] 测验{action_name}成功: {res_json.get('msg', '')}", 100)
                return True
            else:
                logger.error(f"测验{action_name}失败: {res_json.get('msg', '未知错误')} | HTTP状态: 200 | Payload keys: {list(questions.keys())}")
                if report_func: await report_func(f"[{_course['title']}] 测验{action_name}失败: {res_json.get('msg', '')}", 100)
                return False
                
        logger.error(f"测验{action_name}遇到网络错误: HTTP {resp_post.status_code}")
        if report_func: await report_func(f"[{_course['title']}] 测验{action_name}遇到网络错误", 100)
        return False

    async def study_emptypage(self, _course, point):
        _resp = await self.client.get(
            url="https://mooc1.chaoxing.com/mooc-ans/mycourse/studentstudyAjax",
            params={
                "courseId": _course["courseId"],
                "clazzid": _course["clazzId"],
                "chapterId": point["id"],
                "cpi": _course["cpi"],
                "verificationcode": "",
                "mooc2": 1,
                "microTopicId": 0,
                "editorPreview": 0,
            },
        )
        return _resp.status_code == 200
