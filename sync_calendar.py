#!/usr/bin/env python3
import requests
import datetime
import json
import sys
import time
from typing import List, Dict
from icalendar import Calendar

# 配置信息
API_BASE = "https://cloud.zectrix.com/open/v1"
API_KEY = "your_api_key"
DEVICE_ID = "your_device_id"
EXPIRE_HOURS = 1  # 超过1小时删除

# CalDAV配置 - 需要用户填写
CALDAV_URL = "https://caldav.mxhichina.com/dav/your-email@example.com/"
CALDAV_USER = "your-email@example.com"  # 你的CalDAV用户名
CALDAV_PASS = "your_mail_password"  # 你的CalDAV密码/授权码


class CalendarSyncer:
    def __init__(self):
        self.headers = {
            "X-API-Key": API_KEY,
            "Content-Type": "application/json"
        }
        self.existing_todos = []
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

    def delete_expired_calendar_todos(self):
        """删除过期的日历待办"""
        deleted_count = 0
        for todo in self.existing_todos:
            title = todo.get("title", "")
            if not title.startswith("[日历]"):
                continue

            dueDate = todo.get("dueDate", "")
            dueTime = todo.get("dueTime", "")

            if self.is_expired(dueDate, dueTime):
                todo_id = todo.get("id")
                if self.delete_todo(todo_id):
                    print(f"已删除过期日程: id={todo_id} title={title} {dueDate} {dueTime}")
                    deleted_count += 1

        print(f"清理完成，共删除 {deleted_count} 个过期日程")

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

        today = datetime.datetime.now()
        today_str = today.strftime("%Y-%m-%d")
        start_dt = datetime.datetime(today.year, today.month, today.day)
        end_dt = start_dt + datetime.timedelta(days=1)

        def _fetch():
            import caldav

            # 连接CalDAV服务器 (兼容老版本API)
            client = caldav.DAVClient(
                url=CALDAV_URL,
                username=CALDAV_USER,
                password=CALDAV_PASS,
                auth=requests.auth.HTTPBasicAuth(CALDAV_USER, CALDAV_PASS)
            )

            # 获取所有日历
            principal = client.principal()
            calendars = principal.calendars()
            if not calendars:
                print("未找到日历")
                return []

            print(f"找到 {len(calendars)} 个日历")

            # 在每个日历中搜索接下来24小时内的事件
            events = []
            now = datetime.datetime.now().astimezone()
            # 搜索范围从现在开始到未来24小时
            start_search = now - datetime.timedelta(minutes=10)  # 给点缓冲
            end_search = now + datetime.timedelta(hours=24)

            for calendar in calendars:
                events_found = calendar.date_search(
                    start=start_search,
                    end=end_search
                )
                for event in events_found:
                    parsed_list = self.parse_caldav_event(event)
                    events.extend(parsed_list)

            print(f"解析出 {len(events)} 个未来24小时内的日程")
            return events

        result = self.retry_with_backoff(_fetch)
        return result if result is not None else []

    def parse_caldav_event(self, event) -> List[Dict]:
        """解析CalDAV事件，处理时区转换，只返回未来24小时内的未取消日程"""
        events = []
        try:
            cal = Calendar.from_ical(event.data)
            now = datetime.datetime.now().astimezone()  # 当前本地时间带时区
            # 只同步未来24小时内的日程
            end_range = now + datetime.timedelta(hours=24)

            for component in cal.walk():
                if component.name == "VEVENT":
                    summary = str(component.get('SUMMARY', ''))
                    dtstart = component.get('DTSTART')

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
                        # 如果有时区信息，转换为本地时间，否则假设已是本地时间
                        if dt.tzinfo is None:
                            dt = dt.astimezone()
                        else:
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

                    # 只保留未来24小时内的日程
                    if dt < now or dt > end_range:
                        if dt < now:
                            print(f"跳过已过去日程: {summary.strip()} {date_str} {time_str}")
                        else:
                            print(f"跳过超过24小时日程: {summary.strip()} {date_str} {time_str}")
                        continue

                    events.append({
                        "title": summary.strip(),
                        "dueDate": date_str,
                        "dueTime": time_str
                    })

            return events
        except Exception as e:
            print(f"解析CalDAV事件异常: {e}")
            return []

    def create_todo(self, title: str, dueDate: str, dueTime: str) -> bool:
        """创建新的待办事项，带重试"""
        def _create():
            url = f"{API_BASE}/todos"
            data = {
                "title": f"[日历] {title}".strip(),
                "description": "从邮箱日历同步",
                "dueDate": dueDate,
                "dueTime": dueTime,
                "repeatType": "none",
                "priority": 1,
                "deviceId": DEVICE_ID
            }
            resp = requests.post(url, headers=self.headers, json=data, timeout=10)
            resp.raise_for_status()
            result = resp.json()
            if result.get("code") == 0:
                print(f"创建日程成功: {data['title']} {dueDate} {dueTime}")
                return True
            else:
                print(f"创建日程失败: {result.get('msg')}")
                return False

        result = self.retry_with_backoff(_create)
        return result if result is not None else False

    def event_exists(self, event: Dict) -> bool:
        """检查日程是否已存在 (根据title和dueDate匹配)"""
        event_title = event["title"].strip()
        event_date = event["dueDate"]

        for todo in self.existing_todos:
            todo_title = todo.get("title", "").replace("[日历]", "").strip()
            todo_date = todo.get("dueDate", "")

            if todo_title == event_title and todo_date == event_date:
                return True
        return False

    def sync_new_events(self, events: List[Dict]):
        """同步不存在的新日程"""
        created_count = 0
        for event in events:
            if not self.event_exists(event):
                if self.create_todo(event["title"], event["dueDate"], event["dueTime"]):
                    created_count += 1
        print(f"同步完成，新增 {created_count} 个日程")

    def run(self):
        """运行完整同步流程"""
        print("=" * 50)
        print(f"开始同步日历 时间: {datetime.datetime.now()}")
        print("=" * 50)

        # 1. 获取现有待办
        self.get_existing_todos()

        # 2. 删除过期的日历待办
        print("\n步骤1: 删除过期日程...")
        self.delete_expired_calendar_todos()

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
