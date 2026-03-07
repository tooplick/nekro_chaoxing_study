import asyncio
from typing import AsyncGenerator, Literal, Any
from pydantic import Field, field_validator

from nekro_agent.api import i18n
from nekro_agent.api.core import logger
from nekro_agent.api.plugin import ConfigBase, ExtraField, NekroPlugin, SandboxMethodType
from nekro_agent.api.schemas import AgentCtx
from nekro_agent.services.plugin.task import AsyncTaskHandle, TaskCtl, TaskSignal, task

from .chaoxing_api import AsyncChaoxing
from .task_manager import task_manager

# 插件元信息
plugin = NekroPlugin(
    name="超星学习助手",
    module_name="nekro_chaoxing_study",
    description="自动完成超星学习通课程任务，支持视频、文档、测验等。支持多账号并发，提供灵活的后台任务能力。",
    version="1.0.0",
    author="QeQian",
    url="https://github.com/tooplick/nekro_chaoxing_study",
)

@plugin.mount_config()
class ChaoxingConfig(ConfigBase):
    """超星学习插件配置"""

    speed: float = Field(
        default=1.0, title="视频播放倍速",
        description="视频播放倍速 (建议 1.0~2.0 之间)",
        json_schema_extra=ExtraField(
            i18n_title=i18n.i18n_text(zh_CN="视频播放倍速", en_US="Video Speed"),
            i18n_description=i18n.i18n_text(zh_CN="视频播放倍速 (建议 1.0~2.0 之间)", en_US="Video Speed (1.0~2.0)"),
        ).model_dump()
    )
    
    max_concurrent: int = Field(
        default=1, title="最大并发章节数",
        description="同时处理的最大章节数量 (针对单一课)",
        json_schema_extra=ExtraField(
            i18n_title=i18n.i18n_text(zh_CN="最大并发章节数", en_US="Max Concurrent Chapters"),
        ).model_dump()
    )
    
    notify_level: Literal["Chapter", "Course", "None"] = Field(
        default="Chapter", title="通知提醒级别",
        description="向 AI 推送任务进度的频率: Chapter=每章节完成提醒, Course=仅整门课完成提醒, None=不提醒",
        json_schema_extra=ExtraField(
            i18n_title=i18n.i18n_text(zh_CN="通知提醒级别", en_US="Notify Level"),
            i18n_description=i18n.i18n_text(zh_CN="Chapter=章节级, Course=课程级, None=关闭", en_US="Chapter, Course, None")
        ).model_dump()
    )
    
    @field_validator("notify_level", mode="before")
    @classmethod
    def _convert_old_notify_level(cls, v: Any) -> Any:
        # 向下兼容以前的 1, 2, 3 数字配置
        if isinstance(v, int) or (isinstance(v, str) and v.isdigit()):
            v = int(v)
            if v == 1:
                return "Chapter"
            elif v == 2:
                return "Course"
            elif v == 3:
                return "None"
        return v
    
    
    ai_model_group: str = Field(
        default="default", title="AI 题库模型组",
        description="选择用于答题的系统大模型组",
        json_schema_extra=ExtraField(
            ref_model_groups=True, 
            model_type="chat",
            i18n_title=i18n.i18n_text(zh_CN="AI 题库模型组", en_US="AI Model Group"),
        ).model_dump()
    )


# 获取配置实例和存储
config: ChaoxingConfig = plugin.get_config(ChaoxingConfig)
store = plugin.store


# ==================== 生命周期 ====================

@plugin.mount_init_method()
async def init_plugin():
    """初始化"""
    plugin_dir = plugin.get_plugin_data_dir()
    (plugin_dir / "logs").mkdir(parents=True, exist_ok=True)
    logger.info("超星学习助手插件初始化完成")

@plugin.mount_cleanup_method()
async def cleanup_plugin():
    """清理"""
    logger.info("超星学习助手插件正在清理...")


# ==================== 内联辅助方法 ====================

def _dump_cookies(cookies) -> list:
    """序列化 httpx.Cookies，保留域名和路径，防止跨域不同名 JSESSIONID 冲突"""
    return [
        {
            "name": c.name,
            "value": c.value,
            "domain": c.domain,
            "path": c.path,
        }
        for c in cookies.jar
    ]

def _load_cookies(cookie_data) -> Any:
    import httpx
    jar = httpx.Cookies()
    if isinstance(cookie_data, dict):
        for k, v in cookie_data.items():
            jar.set(k, v)
    elif isinstance(cookie_data, list):
        for c in cookie_data:
            jar.set(c["name"], c["value"], domain=c.get("domain", ""), path=c.get("path", "/"))
    return jar

async def _get_client_for_user(chat_key: str, username: str, password: str = None) -> AsyncChaoxing:
    """辅助方法：依据 username 提取该频道的该用户 cookies，返回初始化好的 client 实例。
    
    如果提供了 password 或者 KV 中保存了该用户的密码，则尝试对应进行登录。
    """
    import json
    
    if not username:
        raise ValueError("请提供目标 username")
        
    cookies_json_str = await store.get(chat_key=chat_key, store_key=f"account_cookies_{username}")
    
    # 如果明确传了新密码，或者没有 cookie 时尝试自动登录
    if password or not cookies_json_str:
        saved_password = password or await store.get(chat_key=chat_key, store_key=f"account_password_{username}")
        if not saved_password:
            raise ValueError(f"未找到用户 {username} 的登录凭证，请先登录该账号。")
        
        logger.info(f"[自动登录] 正在为 {username} 尝试登录...")
        client = AsyncChaoxing()
        result = await client.login(username, saved_password)
        
        if not result["status"]:
            await client.close()
            raise ValueError(f"登录失败: {result['msg']}，请检查密码或重试。")
        
        # 登录成功，保存新的 cookies 和 password
        await store.set(
            chat_key=chat_key,
            store_key=f"account_cookies_{username}",
            value=json.dumps(_dump_cookies(client.client.cookies))
        )
        await store.set(
            chat_key=chat_key,
            store_key=f"account_password_{username}",
            value=saved_password
        )
        
        uid = await client.get_uid()
        if uid:
            client.uid = uid
            await store.set(chat_key=chat_key, store_key=f"account_uid_{username}", value=str(uid))
        
        client.account = {"username": username}
        logger.info(f"[自动登录] {username} 登录成功")
        return client
    
    cookies_data = json.loads(cookies_json_str)
    client = AsyncChaoxing(cookies=_load_cookies(cookies_data))
    client.account = {"username": username}
    
    # 获取 UID 并恢复到 client 中
    uid_str = await store.get(chat_key=chat_key, store_key=f"account_uid_{username}")
    if uid_str:
        client.uid = uid_str
        
    return client


def _status_icon(status: str) -> str:
    """状态图标"""
    return {
        "running": "🔄",
        "pending": "⏳",
        "success": "✅",
        "failed": "❌",
        "cancelled": "🛑",
    }.get(status, "❓")


def _progress_bar(percent: int, width: int = 10) -> str:
    """生成进度条"""
    filled = int(width * percent / 100)
    empty = width - filled
    return "▓" * filled + "░" * empty


# ==================== 沙盒方法 ====================


@plugin.mount_sandbox_method(
    SandboxMethodType.AGENT, 
    name="get_courses", 
    description="获取指定节点服务器（用户）下属的所有可用资源列表（API连通性测试必备前置）"
)
async def get_courses(_ctx: AgentCtx, target_username: str, password: str = None) -> str:
    """
    获取指定账号节点的所有可测资源列表数据
    
    Args:
        target_username (str): 目标操作用户的账号(上次登录时使用的手机号)
        password (str): 可选密码。如果传入则强制重新热连并刷新鉴权。

    Returns:
        str: 包含所有课程信息（课程名称、课程ID等）格式化的字符串
    """
    client = None
    try:
        logger.info(f"[get_courses] 开始获取 {target_username} 的课程列表...")
        client = await _get_client_for_user(_ctx.from_chat_key, target_username, password=password)
        logger.info(f"[get_courses] 客户端就绪，正在调用 get_course_list...")
        courses = await client.get_course_list()
        logger.info(f"[get_courses] 获取到 {len(courses)} 门课程")
        
        if not courses:
            return f"用户 {target_username} 没有检索到任何课程。"
            
        output = [f"【{target_username} 的课程列表】"]
        for i, c in enumerate(courses):
            output.append(f"{i+1}. {c['title']} (ID: {c['courseId']}, clazzId: {c['clazzId']}, cpi: {c.get('cpi','')})")
            
        return chr(10).join(output)
        
    except Exception as e:
        import traceback
        logger.error(f"[get_courses] 获取课程失败: {e}\n{traceback.format_exc()}")
        return f"获取课程失败: {str(e)}"
    finally:
        if client:
            try:
                await client.close()
            except Exception:
                pass


@plugin.mount_sandbox_method(
    SandboxMethodType.AGENT, 
    name="create_study_task", 
    description="创建异步持久化协程，开始对指定资源的连通性进行压测与同步"
)
async def create_study_task(_ctx: AgentCtx, target_username: str, course_ids: str = "", password: str = None) -> str:
    """
    创建异步后台任务开始自动学习指定账号的指定课程。此任务将在后台独立沙盒长期运行。
    
    【重要指示 - 节点必读】:
    如果系统下达了同步名称（例如：“同步 Spring Boot” 或 “刷这门课”），你**必须先调用 get_courses 获取该节点下的全部资源列表**，找到对应资源真实的数字 `courseId`，然后精准填入 `course_ids` 参数中。绝对不能将自然语言中文填入 `course_ids`
    Args:
        target_username (str): 目标鉴权账号(手机号)
        course_ids (str): 选填选项，为逗号分隔的资源ID（纯数字组合）。如果请求明确表示要学习所有课程，再留空。
        password (str): 可选密码。如果传入则在运行时自动使用此密码鉴权并获取生命周期凭证。

    Returns:
        str: 任务抛出成功后的提示信息与TaskID
    """
    import uuid
    import json
    
    # 验证账号凭证（支持自动验证登录/保存凭证）
    try:
        client = await _get_client_for_user(_ctx.from_chat_key, target_username, password=password)
        cookies = _dump_cookies(client.client.cookies)
        await client.close()
    except ValueError as e:
        return str(e)
    except Exception as e:
        return f"提取凭证异常: {e}"

    task_id = str(uuid.uuid4())[:8]
    
    # 在 TaskManager 中创建任务记录
    study_task = task_manager.create_task(
        chat_key=_ctx.from_chat_key,
        task_id=task_id,
        username=target_username,
        course_ids=course_ids,
    )
    
    # 终态回调：同步更新 TaskManager（参考 webapp 模式）
    _chat_key = _ctx.from_chat_key
    def _on_terminal(ctl: TaskCtl) -> None:
        if ctl.signal == TaskSignal.SUCCESS:
            task_manager.update_status(_chat_key, task_id, "success", progress=100, detail="所有目标课程学习完成")
        elif ctl.signal == TaskSignal.CANCEL:
            task_manager.update_status(_chat_key, task_id, "cancelled", detail="任务已取消")
        else:
            task_manager.update_status(_chat_key, task_id, "failed", error=ctl.message)
        
    try:
        # 官方插件模式：直接从全局配置读取模型组，不走频道级别数据库查询
        from nekro_agent.api import core
        ai_group_info = None
        if config.ai_model_group:
            try:
                model_group = core.config.MODEL_GROUPS[config.ai_model_group]
                ai_group_info = model_group.model_dump()
            except (KeyError, Exception) as cfg_err:
                logger.warning(f"获取模型组配置失败(AI答题不可用): {cfg_err}")
                
        await task.start(
            task_type="course_study_task",
            task_id=task_id,
            chat_key=_ctx.from_chat_key,
            plugin=plugin,
            on_terminal=_on_terminal,
            
            target_username=target_username,
            cookies=cookies,
            course_ids=course_ids,
            app_config=config.model_dump(),
            ai_group_info=ai_group_info
        )
        
        # 标记为 running
        task_manager.update_status(_chat_key, task_id, "running", detail="任务已启动")
        
        return f"学习任务已在后台成功开始。任务ID: {task_id}。请耐心等待后台运行，将随时报告进度。"
        
    except Exception as e:
        logger.exception(f"启动异步任务异常: {e}")
        task_manager.update_status(_chat_key, task_id, "failed", error=str(e))
        return f"创建任务失败: {e}"


@plugin.mount_sandbox_method(
    SandboxMethodType.AGENT, 
    name="list_study_tasks", 
    description="查看当前会话中所有后台课程学习任务状态"
)
async def list_study_tasks(_ctx: AgentCtx = None) -> str:
    """
    查看当前会话中所有后台课程学习任务状态，了解正在运行的、已完成的以及所有用户相关的后台状态。
    
    Returns:
        str: 当前所有活动和完成的后台任务列表
    """
    try:
        chat_key = _ctx.from_chat_key if _ctx else "default"
        tasks = task_manager.list_active_tasks(chat_key)
        if not tasks:
            return "当前没有任何后台学习任务记录。"
            
        res = ["【所有后台学习任务状态】"]
        for t in tasks:
            icon = _status_icon(t.status)
            line = f"{icon} 任务ID: {t.task_id} | 用户: {t.username} | 课程: {t.course_ids or '所有'} | 状态: {t.status}"
            
            if t.status == "running":
                line += f"\n   ► 总进度: {_progress_bar(t.progress)} {t.progress}%"
                if t.current_course:
                    line += f"\n   ► 当前课程: {t.current_course}"
                if t.current_chapter:
                    line += f"\n   ► 当前章节: {t.current_chapter}"
                if t.current_video_progress:
                    line += f"\n   ► 视频进度: {t.current_video_progress}"
                line += f"\n   ► 已运行: {t.elapsed_formatted()}"
            elif t.status == "success":
                line += f" | 用时: {t.elapsed_formatted()}"
            elif t.status == "failed" and t.error:
                line += f"\n   💥 {t.error}"
            
            res.append(line)
        
        return chr(10).join(res)
    except Exception as e:
        import traceback
        return f"查询任务列表时发生异常: {e}\n{traceback.format_exc()}"

# ==================== 任务管理 ====================

@plugin.mount_sandbox_method(
    SandboxMethodType.TOOL, 
    name="cancel_study_task", 
    description="取消指定的后台学习任务"
)
async def cancel_study_task(_ctx: AgentCtx, task_id: str) -> str:
    """
    取消指定ID的后台学习任务。

    Args:
        task_id (str): 要取消的任务ID

    Returns:
        str: 取消结果提示信息
    """
    study_task = task_manager.get_task(_ctx.from_chat_key, task_id)
    if not study_task:
        return f"未找到任务 {task_id}，请检查任务ID是否正确。"
    
    if study_task.status in ("success", "failed", "cancelled"):
        return f"任务 {task_id} 已处于终态 ({study_task.status})，无需取消。"
    
    success = await task.cancel("course_study_task", task_id)
    if success:
        task_manager.update_status(_ctx.from_chat_key, task_id, "cancelled", detail="用户手动取消")
        return f"✅ 任务 {task_id} 已成功取消。"
    else:
        return f"❌ 取消任务 {task_id} 失败，该任务可能已经完成或不存在。"


@plugin.mount_sandbox_method(
    SandboxMethodType.TOOL, 
    name="stop_all_study_tasks", 
    description="停止当前所有正在运行的后台学习任务"
)
async def stop_all_study_tasks(_ctx: AgentCtx) -> str:
    """
    停止所有正在运行的后台学习任务。

    Returns:
        str: 停止结果提示信息
    """
    active_tasks = task_manager.list_active_tasks(_ctx.from_chat_key)
    running_tasks = [t for t in active_tasks if t.status in ("pending", "running")]
    
    if not running_tasks:
        return "当前没有正在运行的学习任务。"
    
    cancelled_count = 0
    for t in running_tasks:
        success = await task.cancel("course_study_task", t.task_id)
        if success:
            task_manager.update_status(_ctx.from_chat_key, t.task_id, "cancelled", detail="批量停止")
            cancelled_count += 1
    
    return f"✅ 已停止 {cancelled_count}/{len(running_tasks)} 个运行中的任务。"


@plugin.mount_sandbox_method(
    SandboxMethodType.TOOL, 
    name="get_running_tasks", 
    description="获取当前所有正在运行的后台学习任务列表"
)
async def get_running_tasks(_ctx: AgentCtx = None) -> str:
    """
    获取当前正在运行的所有后台学习任务详情。

    Returns:
        str: 运行中的任务列表
    """
    try:
        chat_key = _ctx.from_chat_key if _ctx else "default"
        active_tasks = task_manager.list_active_tasks(chat_key)
        running_tasks = [t for t in active_tasks if t.status in ("pending", "running")]
        
        if not running_tasks:
            return "当前没有正在运行的学习任务。"
        
        res = [f"【正在运行的学习任务: {len(running_tasks)} 个】"]
        for t in running_tasks:
            icon = _status_icon(t.status)
            line = f"{icon} 任务ID: {t.task_id} | 用户: {t.username} | 课程: {t.course_ids or '所有'}"
            line += f"\n   ► 总进度: {_progress_bar(t.progress)} {t.progress}%"
            if t.current_course:
                line += f"\n   ► 当前课程: {t.current_course}"
            if t.current_chapter:
                line += f"\n   ► 当前章节: {t.current_chapter}"
            if t.current_video_progress:
                line += f"\n   ► 视频进度: {t.current_video_progress}"
            line += f"\n   ► 已运行: {t.elapsed_formatted()}"
            is_alive = task.is_running("course_study_task", t.task_id)
            line += f" | {'🟢 运行中' if is_alive else '🔴 已停止'}"
            res.append(line)
        
        return chr(10).join(res)
    except Exception as e:
        import traceback
        return f"获取运行中任务列表失败: {e}\n{traceback.format_exc()}"


# ==================== 提示词注入 ====================

@plugin.mount_prompt_inject_method(
    name="study_status",
    description="向 AI 注入当前学习任务状态"
)
async def study_status_inject(_ctx: AgentCtx) -> str:
    """注入任务状态视图，供 AI 自动感知学习进度"""
    tasks = task_manager.list_active_tasks(_ctx.from_chat_key)
    
    if not tasks:
        return ""
    
    running = [t for t in tasks if t.status in ("pending", "running")]
    
    lines = [f"[超星学习助手] 活跃任务状态 (请严格提取字段，切勿将底层日志中的 mp4 当作课程名): {len(running)}/{len(tasks)}"]
    for t in tasks[:5]:
        icon = _status_icon(t.status)
        desc = f"{t.username} {t.course_ids or '全部课程'}"
        lines.append(f"  {icon} task_id={t.task_id} | {desc}")
        
        if t.status == "running":
            lines.append(f"     - 总进度: {_progress_bar(t.progress)} {t.progress}%")
            if t.current_course:
                lines.append(f"     - 课程: {t.current_course}")
            if t.current_chapter:
                lines.append(f"     - 章节: {t.current_chapter}")
            if t.current_video_progress:
                lines.append(f"     - 视频: {t.current_video_progress}")
        if t.status == "failed" and t.error:
            err = t.error[:40] + "..." if len(t.error) > 40 else t.error
            lines.append(f"     └─ 错误: {err}")
    
    return "\n".join(lines)


# ==================== 异步任务 ====================

@plugin.mount_async_task("course_study_task")
async def _course_study_task(
    handle: AsyncTaskHandle, 
    target_username: str,
    cookies: Any,
    course_ids: str,
    app_config: dict,
    ai_group_info: dict = None
) -> AsyncGenerator[TaskCtl, None]:
    """后台异步学习任务本身"""
    chat_key = handle.chat_key
    tid = handle.task_id

    yield TaskCtl.report_progress(f"[{target_username}] 正在初始化学习环境...", 0)
    task_manager.update_status(chat_key, tid, "running", progress=0, detail="正在初始化学习环境...")
    
    client = None  # 确保 finally 中可以安全引用
    try:
        import traceback
        logger.info(f"[异步任务] 开始初始化 | 用户={target_username} | 课程={course_ids or '所有'} | task_id={tid}")
        client = AsyncChaoxing(cookies=_load_cookies(cookies))
        client.account = {"username": target_username}
        client.config = app_config
        
        if ai_group_info:
            from .tiku import AITiku
            client.tiku = AITiku(ai_group_info)
        
        # 1. 查询目标课程
        logger.info(f"[异步任务] 开始获取课程列表...")
        courses = await client.get_course_list()
        logger.info(f"[异步任务] 获取到 {len(courses)} 门课程")
        
        target_ids = [cid.strip() for cid in course_ids.split(",")] if course_ids.strip() else []
        courses_to_study = []
        for c in courses:
            if not target_ids or str(c["courseId"]) in target_ids:
                courses_to_study.append(c)
                
        if not courses_to_study:
            task_manager.update_status(chat_key, tid, "success", progress=100, detail="没有找到要学习的课程")
            yield TaskCtl.success("没有找到要学习的课程", {"status": "none"})
            return
        
        total = len(courses_to_study)
        msg = f"找到 {total} 门课程准备学习"
        yield TaskCtl.report_progress(f"[{target_username}] {msg}", 5)
        task_manager.update_status(chat_key, tid, "running", progress=5, detail=msg, total_courses=total)
        
        # 开始逐门课程刷
        for ci, course in enumerate(courses_to_study):
            if handle.is_cancelled:
                task_manager.update_status(chat_key, tid, "cancelled", detail="任务已取消")
                yield TaskCtl.cancel("任务已取消")
                return

            overall_pct = 0
            course_msg = f"正在学习: {course['title']} ({ci+1}/{total})"
            yield TaskCtl.report_progress(f"[{target_username}] {course_msg}", overall_pct)
            task_manager.update_status(
                chat_key, tid, "running",
                progress=overall_pct,
                detail=course_msg,
                current_course=course['title'],
                finished_courses=ci,
            )
            
            async def report_func(msg, pct, **kwargs):
                task_manager.update_status(
                    chat_key, tid, "running", 
                    progress=pct, 
                    detail=msg, 
                    **kwargs
                )
                await handle.notify_agent(f"[{target_username}] {msg} ({pct}%)", trigger=False)

            logger.info(f"[异步任务] 开始处理课程: {course['title']} (ID={course['courseId']})")
            await client.process_course(course, handle, report_func)
            logger.info(f"[异步任务] 课程完成: {course['title']}")
            
            # 单门课程完成通知 AI 及其用户 (当 notify_level 为 Chapter 或 Course 时)
            if app_config.get("notify_level", "Chapter") in ("Chapter", "Course"):
                await handle.notify_agent(
                    f"🎉 课程学习完毕: {course['title']}\n当前进度: {ci+1}/{total} 门",
                    trigger=True
                )
            
        # 完成
        task_manager.update_status(
            chat_key, tid, "success",
            progress=100,
            detail="所有目标课程学习完成",
            finished_courses=total,
        )
        yield TaskCtl.success("所有目标课程学习完成", {"status": "success"})

    except Exception as e:
        import traceback
        err_detail = repr(e) or type(e).__name__
        tb = traceback.format_exc()
        logger.error(f"[异步任务] 崩溃: {err_detail}")
        logger.error(f"[异步任务] Traceback:\n{tb}")
        task_manager.update_status(chat_key, tid, "failed", error=err_detail, detail=f"异常崩溃: {err_detail}")
        yield TaskCtl.fail(f"异常崩溃: {err_detail}")
    finally:
        # 确保 httpx.AsyncClient 连接池被释放，防止连接泄漏导致后续任务 ConnectError
        if client:
            try:
                await client.close()
                logger.info(f"[异步任务] 客户端已关闭 | task_id={tid}")
            except Exception:
                pass
