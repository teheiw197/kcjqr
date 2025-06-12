from typing import Dict, List, Optional, Any
import asyncio
import re
import json
import yaml
import os
from datetime import datetime, timedelta
from pathlib import Path

from astrbot.api import AstrBot, AstrMessageEvent, AstrMessage, logger
from astrbot.api.message_components import Comp
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register

# 课程消息模板
COURSE_TEMPLATE = """【姓名同学学年学期课程安排】

📚 基本信息

• 学校：XX大学（没有则不显示）
• 班级：XX班（没有则不显示）
• 专业：XX专业（没有则不显示）
• 学院：XX学院（没有则不显示）

🗓️ 每周课程详情
{weekly_courses}

🌙 晚间课程
{evening_courses}

📌 重要备注
{notes}"""

# 课程提醒模板
REMINDER_TEMPLATE = """同学你好，待会有课哦
上课时间（节次和时间）：{time}
课程名称：{course_name}
教师：{teacher}
上课地点：{location}"""

class CourseStorage:
    def __init__(self, config: dict):
        self.config = config
        self.storage_type = config['storage']['type']
        self.file_path = config['storage']['file_path']
        self.data: Dict[str, Any] = {}
        self.load_data()

    def load_data(self):
        if self.storage_type == 'file':
            try:
                if os.path.exists(self.file_path):
                    with open(self.file_path, 'r', encoding='utf-8') as f:
                        self.data = json.load(f)
            except Exception as e:
                logger.error(f"加载数据失败: {e}")
                self.data = {}

    def save_data(self):
        if self.storage_type == 'file':
            try:
                os.makedirs(os.path.dirname(self.file_path), exist_ok=True)
                with open(self.file_path, 'w', encoding='utf-8') as f:
                    json.dump(self.data, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.error(f"保存数据失败: {e}")

    def get_user_courses(self, user_id: str) -> List[dict]:
        return self.data.get(user_id, {}).get('courses', [])

    def save_user_courses(self, user_id: str, courses: List[dict]):
        if user_id not in self.data:
            self.data[user_id] = {}
        self.data[user_id]['courses'] = courses
        self.save_data()

    def get_user_settings(self, user_id: str) -> dict:
        return self.data.get(user_id, {}).get('settings', {})

    def save_user_settings(self, user_id: str, settings: dict):
        if user_id not in self.data:
            self.data[user_id] = {}
        self.data[user_id]['settings'] = settings
        self.save_data()

@register("kcjqr", "特嘿工作室", "智能课程提醒插件", "1.0.0")
class CourseReminder(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.config = self.load_config()
        self.templates = self.load_templates()
        self.storage = CourseStorage(self.config)
        self.reminder_tasks: Dict[str, asyncio.Task] = {}
        self.user_states: Dict[str, str] = {}

    def load_config(self) -> dict:
        config_path = Path(__file__).parent / 'config.yaml'
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f)
        except Exception as e:
            logger.error(f"加载配置文件失败: {e}")
            return {}

    def load_templates(self) -> dict:
        template_path = Path(__file__).parent / 'templates' / 'messages.yaml'
        try:
            with open(template_path, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f)
        except Exception as e:
            logger.error(f"加载模板文件失败: {e}")
            return {}

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def handle_message(self, event: AstrMessageEvent):
        sender = event.message_obj.sender
        user_id = sender.id
        message = event.message_obj.message
        text = ""
        for seg in message:
            if isinstance(seg, Comp.Plain):
                text += seg.text

        # 检查用户状态
        if user_id in self.user_states:
            async for result in self.handle_state_message(event):
                yield result
            return

        # 检查消息类型
        has_media = False
        for seg in message:
            if isinstance(seg, Comp.Image) or isinstance(seg, Comp.File):
                has_media = True
                break

        if has_media:
            async for result in self.handle_media_message(event):
                yield result
        else:
            async for result in self.handle_text_message(event):
                yield result

    async def handle_state_message(self, event: AstrMessageEvent):
        sender = event.message_obj.sender
        user_id = sender.id
        message = event.message_obj.message
        text = ""
        for seg in message:
            if isinstance(seg, Comp.Plain):
                text += seg.text
        state = self.user_states[user_id]

        if state == 'waiting_confirmation':
            if text == '确认':
                courses = self.storage.get_user_courses(user_id)
                await self.setup_reminders(user_id, courses)
                yield event.plain_result("已开启课程提醒！")
            elif text == '取消':
                yield event.plain_result("已取消本次操作。")
            else:
                yield event.plain_result('请回复"确认"或"取消"。')
            del self.user_states[user_id]
        elif state == 'waiting_daily_preview':
            if text == '是':
                settings = self.storage.get_user_settings(user_id)
                settings['enable_daily_reminder'] = True
                self.storage.save_user_settings(user_id, settings)
                yield event.plain_result("已开启次日课程提醒！")
            elif text == '否':
                settings = self.storage.get_user_settings(user_id)
                settings['enable_daily_reminder'] = False
                self.storage.save_user_settings(user_id, settings)
                yield event.plain_result("已关闭次日课程提醒。")
            else:
                yield event.plain_result('请回复"是"或"否"。')
            del self.user_states[user_id]

    async def handle_media_message(self, event: AstrMessageEvent):
        template = self.templates.get('errors', {}).get('media_not_supported', '')
        yield event.plain_result(template)

    async def handle_text_message(self, event: AstrMessageEvent):
        message = event.message_obj.message
        text = ""
        for seg in message:
            if isinstance(seg, Comp.Plain):
                text += seg.text
        user_id = event.message_obj.sender.id

        # 解析课程信息
        courses = self.parse_courses(text)
        if not courses:
            error_msg = self.templates.get('errors', {}).get('parse_failed', '')
            yield event.plain_result(error_msg)
            return

        # 保存课程信息
        self.storage.save_user_courses(user_id, courses)

        # 发送确认消息
        await self.send_course_confirmation(event, courses)

        # 设置用户状态
        self.user_states[user_id] = 'waiting_confirmation'

    def parse_courses(self, text: str) -> List[dict]:
        courses = []
        current_course = {}
        current_weekday = None

        for line in text.split('\n'):
            line = line.strip()
            if not line:
                continue

            # 检查是否是星期几
            weekday_match = re.match(r'星期[一二三四五六日]', line)
            if weekday_match:
                if current_course:
                    courses.append(current_course)
                current_weekday = line
                current_course = {'weekday': current_weekday}
                continue

            # 解析课程信息
            if '上课时间' in line:
                if current_course:
                    courses.append(current_course)
                current_course = {'weekday': current_weekday, 'time': line.split('：')[1].strip()}
            elif '课程名称' in line:
                current_course['course_name'] = line.split('：')[1].strip()
            elif '教师' in line:
                current_course['teacher'] = line.split('：')[1].strip()
            elif '上课地点' in line:
                current_course['location'] = line.split('：')[1].strip()
            elif '周次' in line:
                current_course['weeks'] = line.split('：')[1].strip()

        if current_course:
            courses.append(current_course)

        return courses

    async def send_course_confirmation(self, event: AstrMessageEvent, courses: List[dict]):
        confirmation_template = self.templates.get('confirmation', '')
        courses_text = ""
        
        for course in courses:
            courses_text += f"星期{course['weekday']}\n"
            courses_text += f"上课时间：{course['time']}\n"
            courses_text += f"课程名称：{course['course_name']}\n"
            courses_text += f"教师：{course['teacher']}\n"
            courses_text += f"上课地点：{course['location']}\n"
            if 'weeks' in course:
                courses_text += f"周次：{course['weeks']}\n"
            courses_text += "\n"

        confirmation = confirmation_template.format(courses=courses_text)
        yield event.plain_result(confirmation)

    async def setup_reminders(self, user_id: str, courses: List[dict]):
        # 取消现有的提醒任务
        if user_id in self.reminder_tasks:
            self.reminder_tasks[user_id].cancel()

        # 创建新的提醒任务
        self.reminder_tasks[user_id] = asyncio.create_task(
            self.reminder_loop(user_id, courses)
        )

    async def reminder_loop(self, user_id: str, courses: List[dict]):
        while True:
            try:
                now = datetime.now()
                
                # 检查是否需要发送每日课程预览
                if self.config['reminder']['enable_daily_preview']:
                    preview_time_str = self.config['reminder']['daily_preview_time']
                    preview_hour, preview_minute = map(int, preview_time_str.split(':'))
                    if now.time().hour == preview_hour and now.time().minute == preview_minute:
                        await self.send_daily_preview(user_id, courses)

                # 检查是否需要发送课程提醒
                for course in courses:
                    course_time = self.parse_course_time(course['time'])
                    if course_time:
                        remind_time = course_time - timedelta(minutes=self.config['reminder']['advance_minutes'])
                        if now >= remind_time and now < course_time:
                            await self.send_reminder(user_id, course)

                # 等待1分钟再次检查
                await asyncio.sleep(60)
            except Exception as e:
                logger.error(f"提醒任务出错: {e}")
                await asyncio.sleep(60)

    def parse_course_time(self, time_str: str) -> Optional[datetime]:
        pattern = r'第(\d+)-(\d+)节\s*\((\d{2}):(\d{2})-(\d{2}):(\d{2})\)'
        match = re.search(pattern, time_str)
        if not match:
            return None

        start_hour = int(match.group(3))
        start_minute = int(match.group(4))

        now = datetime.now()
        course_time = now.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)

        return course_time

    async def send_reminder(self, user_id: str, course: dict):
        reminder_template = self.templates.get('reminder', '')
        reminder = reminder_template.format(
            time=course['time'],
            course_name=course['course_name'],
            teacher=course['teacher'],
            location=course['location']
        )

        try:
            await self.context.send_message(user_id, [Comp.Plain(reminder)])
        except Exception as e:
            logger.error(f"发送提醒失败: {e}")

    async def send_daily_preview(self, user_id: str, courses: List[dict]):
        settings = self.storage.get_user_settings(user_id)
        if not settings.get('enable_daily_reminder', True):
            return

        preview_template = self.templates.get('daily_preview', '')
        courses_text = ""

        for course in courses:
            courses_text += f"上课时间：{course['time']}\n"
            courses_text += f"课程名称：{course['course_name']}\n"
            courses_text += f"教师：{course['teacher']}\n"
            courses_text += f"上课地点：{course['location']}\n\n"

        preview = preview_template.format(courses=courses_text)
        
        try:
            await self.context.send_message(user_id, [Comp.Plain(preview)])
            self.user_states[user_id] = 'waiting_daily_preview'
        except Exception as e:
            logger.error(f"发送每日预览失败: {e}")

    async def terminate(self):
        """插件卸载时调用"""
        # 取消所有提醒任务
        for task in self.reminder_tasks.values():
            task.cancel()
        self.reminder_tasks.clear() 