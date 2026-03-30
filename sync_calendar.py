#!/usr/bin/env python3
import os
import requests
import datetime
import json
import sys
import time
from typing import List, Dict, Optional
from icalendar import Calendar

# 尝试从 .env 文件加载环境变量
try:
    from dotenv import load_dotenv
    load_dotenv()  # 加载 .env 文件
except ImportError:
    # 如果没安装 python-dotenv 就跳过，直接使用系统环境变量
    pass

# 配置信息 - 从环境变量读取
API_BASE = os.getenv("API_BASE", "https://cloud.zectrix.com/open/v1")
API_KEY = os.getenv("API_KEY", "")
DEVICE_ID = os.getenv("DEVICE_ID", "")
EXPIRE_HOURS = int(os.getenv("EXPIRE_HOURS", "1"))  # 超过N小时删除

# CalDAV配置 - 从环境变量读取
CALDAV_URL = os.getenv("CALDAV_URL", "https://caldav.mxhichina.com/dav/")
CALDAV_USER = os.getenv("CALDAV_USER", "")  # 你的CalDAV用户名
CALDAV_PASS = os.getenv("CALDAV_PASS", "")  # 你的CalDAV密码/授权码


CALENDAR_PREFIX = "[日历]"


class CalendarSyncer:
    def __init__(self):
        self.headers = {
            "X-API-Key": API_KEY,
            "Content-Type": "application/json"
        }
        self.existing_todos = []
        self._uid_map: Dict[str, Dict] = {}
        self.max_retries = 3

    def retry_with_backoff(self, func, *args, **kwargs):
        """指数退避重试"""
        for attempt in range(self.max_retries):
            try:
                result = func(*args, **kwargs)
                # 如果返回False表示失败，继续重试
                if result is False:
                    delay = 2 ** attempt
                    print(f"  重试 {attempt + 1}/{self.max_retries}, 等待 {delay} 秒...")
                    time.sleep(delay)
                    continue
                return result
            except Exception as e:
                delay = 2 ** attempt
                print(f"  尝试 {attempt + 1}/{self.max_retries} 失败: {e}, 等待 {delay} 秒...")
                time.sleep(delay)
        print(f"  已达到最大重试次数 ({self.max_retries}), 放弃")
        return None

    def get_existing_todos(self) -> List[Dict]:
        """获取现有待办事项列表"""
        def _get():
            url = f"{API_BASE}/todos"
            params = {
                "status": 0,
                "deviceId": DEVICE_ID
            }
            resp = requests.get(url, headers=self.headers, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") == 0:
                self.existing_todos = data.get("data", [])
                self._uid_map = {
                    uid: todo
                    for todo in self.existing_todos
                    if (uid := self.extract_uid_from_description(todo.get("description", "")))
                }
                print(f"获取到 {len(self.existing_todos)} 个待办事项")
                return self.existing_todos
            else:
                print(f"获取列表失败: {data.get('msg')}")
                return False

        result = self.retry_with_backoff(_get)
        return result if result is not None else []

    def is_expired(self, dueDate: str, dueTime: str) -> bool:
        """检查是否已经过期超过EXPIRE_HOURS"""
        try:
            due_datetime = datetime.datetime.strptime(f"{dueDate} {dueTime}", "%Y-%m-%d %H:%M")
            now = datetime.datetime.now()
            diff = now - due_datetime
            return diff.total_seconds() >= EXPIRE_HOURS * 3600
        except Exception as e:
            print(f"解析时间出错 {dueDate} {dueTime}: {e}")
            return False

    def _calendar_todos(self):
        """Yield active calendar todos (未完成 + [日历] 开头)"""
        for todo in self.existing_todos:
            if todo.get("status", 1) != 0:
                continue
            if not todo.get("title", "").startswith(CALENDAR_PREFIX):
                continue
            yield todo

    def complete_expired_calendar_todos(self):
        """将过期的日历待办标记为完成"""
        completed_count = 0
        for todo in self._calendar_todos():
            title = todo.get("title", "")
            dueDate = todo.get("dueDate", "")
            dueTime = todo.get("dueTime", "")

            if self.is_expired(dueDate, dueTime):
                todo_id = todo.get("id")
                print(f"  发现过期日程，正在标记为完成: id={todo_id} title={title} {dueDate} {dueTime}")
                if self.complete_todo(todo_id):
                    print(f"  ✓ 已标记过期日程为完成: id={todo_id} title={title} {dueDate} {dueTime}")
                    completed_count += 1
            else:
                print(f"  日程未过期，跳过: id={todo.get('id')} title={title} {dueDate} {dueTime}")

        print(f"清理完成，共标记 {completed_count} 个过期日程为完成")

    def complete_todo(self, todo_id: int) -> bool:
        """标记待办为完成，带重试"""
        def _complete():
            url = f"{API_BASE}/todos/{todo_id}/complete"
            resp = requests.put(url, headers=self.headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 0:
                print(f"  标记待办 {todo_id} 完成失败: {data.get('msg')}")
                return False
            return True

        result = self.retry_with_backoff(_complete)
        return result if result is not None else False

    def delete_todo(self, todo_id: int) -> bool:
        """删除单个待办，带重试"""
        def _delete():
            url = f"{API_BASE}/todos/{todo_id}"
            resp = requests.delete(url, headers=self.headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 0:
                print(f"  删除待办 {todo_id} 失败: {data.get('msg')}")
                return False
            return True

        result = self.retry_with_backoff(_delete)
        return result if result is not None else False

    def fetch_aliyun_calendar_events(self) -> List[Dict]:
        """通过CalDAV获取邮箱今天的日程，带重试"""
        if not CALDAV_PASS:
            print("请先配置CalDAV密码 (CALDAV_PASS)")
            return []

        def _fetch():
            import caldav
            import requests

            # 连接CalDAV服务器
            client = caldav.DAVClient(
                url=CALDAV_URL,
                username=CALDAV_USER,
                password=CALDAV_PASS
            )

            try:
                # 获取所有日历
                principal = client.principal()
                calendars = principal.calendars()

                if not calendars:
                    print("未找到日历")
                    return []

                print(f"找到 {len(calendars)} 个日历")

                # 在每个日历中搜索今天剩余的事件
                events = []
                now = datetime.datetime.now().astimezone()
                # 搜索范围：现在到今天结束（明天00:00）
                start_search = now - datetime.timedelta(minutes=10)  # 给点缓冲
                today_end = now.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1)

                for calendar in calendars:
                    print(f"  搜索日历: {calendar.url}")
                    print(f"  搜索时间范围: {start_search.strftime('%Y-%m-%d %H:%M')} ~ {today_end.strftime('%Y-%m-%d %H:%M')}")
                    events_found = calendar.date_search(
                        start=start_search,
                        end=today_end
                    )
                    print(f"  CalDAV返回 {len(events_found)} 个事件")
                    for event in events_found:
                        parsed_list = self.parse_caldav_event(event)
                        events.extend(parsed_list)

                print(f"解析出 {len(events)} 个今天剩余的日程")
                return events
            except Exception as e:
                print(f"  CalDAV错误: {type(e).__name__}: {e}")
                if hasattr(e, 'response') and e.response is not None:
                    print(f"  状态码: {e.response.status_code}")
                    print(f"  响应内容: {e.response.text}")
                raise

        result = self.retry_with_backoff(_fetch)
        return result if result is not None else []

    def parse_caldav_event(self, event) -> List[Dict]:
        """解析CalDAV事件，处理时区转换，只返回今天剩余时间内的未取消日程"""
        events = []
        try:
            cal = Calendar.from_ical(event.data)
            now = datetime.datetime.now().astimezone()  # 当前本地时间带时区
            # 只同步今天剩余时间的日程
            today_end = now.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1)

            for component in cal.walk():
                if component.name == "VEVENT":
                    summary = str(component.get('SUMMARY', ''))
                    dtstart = component.get('DTSTART')
                    uid = str(component.get('UID', ''))

                    if not summary or not dtstart:
                        continue

                    # 过滤掉已取消的日程
                    summary_lower = summary.lower()
                    if "已取消" in summary or "cancelled" in summary_lower or "canceled" in summary_lower:
                        print(f"跳过已取消日程: {summary.strip()}")
                        continue

                    # 获取开始时间并转换为本地时区
                    dt = dtstart.dt
                    if isinstance(dt, datetime.datetime):
                        dt = dt.astimezone()
                        date_str = dt.strftime("%Y-%m-%d")
                        time_str = dt.strftime("%H:%M")
                    elif isinstance(dt, datetime.date):
                        # 全天事件，当作当天 09:00
                        dt = datetime.datetime.combine(dt, datetime.time(9, 0)).astimezone()
                        date_str = dt.strftime("%Y-%m-%d")
                        time_str = "09:00"
                    else:
                        continue

                    # 只保留今天剩余时间的日程
                    if dt < now or dt > today_end:
                        if dt < now:
                            print(f"跳过已过去日程: {summary.strip()} {date_str} {time_str}")
                        else:
                            print(f"跳过非今天日程: {summary.strip()} {date_str} {time_str}")
                        continue

                    events.append({
                        "uid": uid,
                        "title": summary.strip(),
                        "dueDate": date_str,
                        "dueTime": time_str
                    })

            return events
        except Exception as e:
            print(f"解析CalDAV事件异常: {e}")
            return []

    @staticmethod
    def _build_description(uid: str) -> str:
        desc = "从邮箱日历同步"
        if uid:
            desc += f"\nUID: {uid}"
        return desc

    def create_todo(self, uid: str, title: str, dueDate: str, dueTime: str) -> bool:
        """创建新的待办事项，带重试"""
        def _create():
            data = {
                "title": f"{CALENDAR_PREFIX} {title}".strip(),
                "description": self._build_description(uid),
                "dueDate": dueDate,
                "dueTime": dueTime,
                "repeatType": "none",
                "priority": 1,
                "deviceId": DEVICE_ID
            }
            resp = requests.post(f"{API_BASE}/todos", headers=self.headers, json=data, timeout=10)
            resp.raise_for_status()
            result = resp.json()
            if result.get("code") == 0:
                print(f"创建日程成功: {data['title']} {dueDate} {dueTime}")
                return True
            print(f"创建日程失败: {result.get('msg')}")
            return False

        result = self.retry_with_backoff(_create)
        return result if result is not None else False

    def extract_uid_from_description(self, description: str) -> str:
        """从description中提取UID"""
        if not description:
            return ""
        for line in description.split('\n'):
            line = line.strip()
            if line.startswith('UID:'):
                return line[4:].strip()
        return ""

    def find_existing_todo_by_uid(self, uid: str) -> Optional[Dict]:
        return self._uid_map.get(uid)

    def update_todo(self, todo_id: int, uid: str, title: str, dueDate: str, dueTime: str) -> bool:
        """更新现有待办事项，带重试"""
        def _update():
            data = {
                "title": f"{CALENDAR_PREFIX} {title}".strip(),
                "description": self._build_description(uid),
                "dueDate": dueDate,
                "dueTime": dueTime
            }
            resp = requests.put(f"{API_BASE}/todos/{todo_id}", headers=self.headers, json=data, timeout=10)
            resp.raise_for_status()
            result = resp.json()
            if result.get("code") == 0:
                print(f"更新日程成功: id={todo_id} title={title} {dueDate} {dueTime}")
                return True
            print(f"更新日程失败: {result.get('msg')}")
            return False

        result = self.retry_with_backoff(_update)
        return result if result is not None else False

    def sync_new_events(self, events: List[Dict]):
        """同步日程：创建新日程、更新已变更、清理已删除"""
        created_count = 0
        updated_count = 0
        # 收集当前所有有效日程的UID集合
        current_uids = {event["uid"] for event in events if event.get("uid")}

        # 第一步：同步新建/更新
        for event in events:
            uid = event.get("uid", "")
            if not uid:
                continue

            existing = self.find_existing_todo_by_uid(uid)
            if not existing:
                # 新建
                if self.create_todo(uid, event["title"], event["dueDate"], event["dueTime"]):
                    created_count += 1
            else:
                # 检查标题或时间是否变更
                existing_title = existing.get("title", "").replace(CALENDAR_PREFIX, "").strip()
                existing_dueDate = existing.get("dueDate", "")
                existing_dueTime = existing.get("dueTime", "")

                if (existing_title != event["title"] or
                    existing_dueDate != event["dueDate"] or
                    existing_dueTime != event["dueTime"]):
                    # 有变更，更新
                    if self.update_todo(
                        existing["id"], uid,
                        event["title"], event["dueDate"], event["dueTime"]
                    ):
                        updated_count += 1

        # 第二步：清理已在CalDAV中删除/取消的日程
        cleaned_count = 0
        for todo in self._calendar_todos():
            uid = self.extract_uid_from_description(todo.get("description", ""))
            if uid and uid not in current_uids:
                todo_id = todo.get("id")
                if self.delete_todo(todo_id):
                    print(f"已删除已取消/删除日程: id={todo_id} title={todo.get('title')} uid={uid}")
                    cleaned_count += 1

        print(f"同步完成，新增 {created_count}，更新 {updated_count}，删除 {cleaned_count} 个日程")

    def run(self):
        """运行完整同步流程"""
        print("=" * 50)
        print(f"开始同步日历 时间: {datetime.datetime.now()}")
        print("=" * 50)

        # 1. 获取现有待办
        self.get_existing_todos()

        # 2. 将过期的日历待办标记为完成
        print("\n步骤1: 标记过期日程为完成...")
        self.complete_expired_calendar_todos()

        # 3. 获取邮箱日程
        print("\n步骤2: 获取邮箱日程...")
        events = self.fetch_aliyun_calendar_events()

        # 4. 同步新增日程
        if events:
            print("\n步骤3: 同步新日程...")
            self.sync_new_events(events)

        print("\n" + "=" * 50)
        print("同步完成!")
        print("=" * 50)


if __name__ == "__main__":
    syncer = CalendarSyncer()
    syncer.run()
