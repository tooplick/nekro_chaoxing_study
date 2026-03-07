"""刷课任务管理器

管理刷课任务的生命周期：
- 创建/更新/归档任务
- 任务状态与进度追踪

参考 nekro-plugin-webapp-main 的 TaskManager 模式。
"""

import time
from dataclasses import dataclass, field
from typing import Dict, List, Literal, Optional

from nekro_agent.api.core import logger

TaskStatus = Literal["pending", "running", "success", "failed", "cancelled"]


@dataclass
class StudyTask:
    """刷课任务"""

    task_id: str
    chat_key: str
    username: str
    course_ids: str = ""  # 逗号分隔的课程ID，空表示全部
    status: TaskStatus = "pending"
    progress: int = 0
    detail: str = ""
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    # 运行时统计
    total_courses: int = 0
    finished_courses: int = 0
    current_course: str = ""
    current_chapter: str = ""
    current_video_progress: str = ""

    def elapsed_seconds(self) -> float:
        """已运行时间（秒）"""
        return time.time() - self.created_at

    def elapsed_formatted(self) -> str:
        """格式化的运行时间"""
        elapsed = int(self.elapsed_seconds())
        if elapsed < 60:
            return f"{elapsed}秒"
        if elapsed < 3600:
            return f"{elapsed // 60}分{elapsed % 60}秒"
        return f"{elapsed // 3600}时{(elapsed % 3600) // 60}分"


class TaskManager:
    """任务管理器

    全局单例，管理每个 chat_key 下的多个刷课任务。
    """

    def __init__(self) -> None:
        # chat_key -> {task_id -> StudyTask}
        self._tasks: Dict[str, Dict[str, StudyTask]] = {}

    def create_task(
        self,
        chat_key: str,
        task_id: str,
        username: str,
        course_ids: str = "",
    ) -> StudyTask:
        """创建新任务"""
        task = StudyTask(
            task_id=task_id,
            chat_key=chat_key,
            username=username,
            course_ids=course_ids,
            status="pending",
            detail="任务已创建，等待启动...",
        )

        if chat_key not in self._tasks:
            self._tasks[chat_key] = {}
        self._tasks[chat_key][task_id] = task

        logger.info(f"[TaskManager] 创建刷课任务 {task_id}: 用户 {username}")
        return task

    def get_task(self, chat_key: str, task_id: str) -> Optional[StudyTask]:
        """获取任务"""
        return self._tasks.get(chat_key, {}).get(task_id)

    def update_status(
        self,
        chat_key: str,
        task_id: str,
        status: TaskStatus,
        progress: int = -1,
        detail: str = "",
        error: Optional[str] = None,
        current_course: Optional[str] = None,
        current_chapter: Optional[str] = None,
        total_courses: int = -1,
        finished_courses: int = -1,
        current_video_progress: Optional[str] = None
    ) -> bool:
        """统一状态更新接口"""
        task = self.get_task(chat_key, task_id)
        if not task:
            return False

        task.status = status
        task.updated_at = time.time()
        if progress >= 0:
            task.progress = progress
        if detail:
            task.detail = detail
        if error:
            task.error = error
        if current_course is not None:
            task.current_course = current_course
        if current_chapter is not None:
            task.current_chapter = current_chapter
        if current_video_progress is not None:
            task.current_video_progress = current_video_progress
        if total_courses >= 0:
            task.total_courses = total_courses
        if finished_courses >= 0:
            task.finished_courses = finished_courses
        return True

    def list_active_tasks(self, chat_key: str) -> List[StudyTask]:
        """列出活跃任务（非已取消状态）"""
        tasks = self._tasks.get(chat_key, {})
        return list(tasks.values())

    def list_running_tasks(self, chat_key: str) -> List[StudyTask]:
        """列出运行中的任务"""
        tasks = self._tasks.get(chat_key, {})
        return [t for t in tasks.values() if t.status in ("pending", "running")]

    def remove_task(self, chat_key: str, task_id: str) -> bool:
        """移除任务记录"""
        tasks = self._tasks.get(chat_key, {})
        if task_id in tasks:
            del tasks[task_id]
            return True
        return False


# 全局单例
task_manager = TaskManager()
