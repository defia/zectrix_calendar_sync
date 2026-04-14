#!/usr/bin/env python3
import os
import argparse
import requests
import datetime
import json
import sys
import time
from typing import List, Dict, Optional
from icalendar import Calendar, Event, vDatetime, vText
import uuid

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

# 本地映射文件路径（存储待办ID与日历UID的映射）
MAPPING_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "todo_mapping.json")


class CalendarSyncer:
    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self.headers = {
            "X-API-Key": API_KEY,
            "Content-Type": "application/json"
        }
        self.existing_todos = []
        self._uid_map: Dict[str, Dict] = {}
        self._todo_to_uid_map: Dict[int, Dict] = {}  # todo_id -> {uid, title, due_date, due_time}
        self._caldav_client = None
        self._caldav_calendar = None
        self.max_retries = 3
        self._load_mapping()

    def _load_mapping(self):
        """加载本地映射文件"""
        try:
            if os.path.exists(MAPPING_FILE):
                with open(MAPPING_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    # 兼容旧格式 (只存 uid 字符串) 和新格式 (存字典)
                    loaded = data.get("todo_to_uid", {})
                    for k, v in loaded.items():
                        todo_id = int(k)
                        if isinstance(v, str):
                            # 旧格式：只有 uid
                            self._todo_to_uid_map[todo_id] = {"uid": v}
                        else:
                            # 新格式：包含完整信息
                            self._todo_to_uid_map[todo_id] = v
        except Exception as e:
            print(f"加载映射文件失败: {e}")
            self._todo_to_uid_map = {}

    def _save_mapping(self):
        """保存本地映射文件"""
        try:
            with open(MAPPING_FILE, 'w', encoding='utf-8') as f:
                json.dump({
                    "todo_to_uid": {str(k): v for k, v in self._todo_to_uid_map.items()}
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"保存映射文件失败: {e}")

    def _get_caldav_calendar(self):
        """获取 CalDAV 日历连接（懒加载）"""
        if self._caldav_calendar is None:
            import caldav
            client = caldav.DAVClient(
                url=CALDAV_URL,
                username=CALDAV_USER,
                password=CALDAV_PASS
            )
            principal = client.principal()
            calendars = principal.calendars()
            if calendars:
                # 使用第一个日历，或寻找名为 "home" 的日历
                for cal in calendars:
                    if "home" in str(cal.url).lower():
                        self._caldav_calendar = cal
                        break
                if self._caldav_calendar is None:
                    self._caldav_calendar = calendars[0]
        return self._caldav_calendar

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
        if self.dry_run:
            print(f"  [DRY RUN] 会标记待办为完成: id={todo_id}")
            return True

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
        if self.dry_run:
            print(f"  [DRY RUN] 会删除待办: id={todo_id}")
            return True

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
        if self.dry_run:
            print(f"  [DRY RUN] 会创建日程: {CALENDAR_PREFIX} {title} {dueDate} {dueTime}")
            return True

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
        if self.dry_run:
            print(f"  [DRY RUN] 会更新日程: id={todo_id} title={title} {dueDate} {dueTime}")
            return True

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

    # ─────────────────────────────────────────────────────
    # 便利贴 → 日历 同步方法
    # ─────────────────────────────────────────────────────

    def _local_todos(self):
        """Yield 本地创建的待办（非日历同步的）"""
        for todo in self.existing_todos:
            if todo.get("status", 1) != 0:
                continue
            title = todo.get("title", "")
            # 不是日历同步的待办
            if not title.startswith(CALENDAR_PREFIX):
                yield todo

    def create_caldav_event(self, title: str, due_date: str, due_time: str) -> Optional[str]:
        """在 CalDAV 日历中创建事件，返回 UID。due_time 为 None 时创建全天事件。"""
        if self.dry_run:
            print(f"  [DRY RUN] 会在日历创建事件: {title} {due_date} {due_time}")
            return f"dry-run-{uuid.uuid4()}"

        try:
            cal = self._get_caldav_calendar()
            if not cal:
                print("无法获取 CalDAV 日历")
                return None

            # 生成唯一 UID
            uid = f"{uuid.uuid4()}@zectrix-sync"

            # 构建日期对象（用于全天事件）
            dt_date = datetime.datetime.strptime(due_date, "%Y-%m-%d").date()
            next_date = dt_date + datetime.timedelta(days=1)

            if due_time:
                # 有具体时间：创建定时事件
                dt_str = f"{due_date}T{due_time}:00"
                dt = datetime.datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S")

                ical_content = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Zectrix Sync//CN
BEGIN:VEVENT
UID:{uid}
DTSTART;TZID=Asia/Shanghai:{dt.strftime("%Y%m%dT%H%M%S")}
DTEND;TZID=Asia/Shanghai:{(dt + datetime.timedelta(hours=1)).strftime("%Y%m%dT%H%M%S")}
SUMMARY:{title}
END:VEVENT
END:VCALENDAR
"""
            else:
                # 没有时间：创建全天事件
                ical_content = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Zectrix Sync//CN
BEGIN:VEVENT
UID:{uid}
DTSTART;VALUE=DATE:{dt_date.strftime("%Y%m%d")}
DTEND;VALUE=DATE:{next_date.strftime("%Y%m%d")}
SUMMARY:{title}
END:VEVENT
END:VCALENDAR
"""

            cal.add_event(ical_content)
            time_str = f" {due_time}" if due_time else " (全天)"
            print(f"在日历创建事件成功: {title} {due_date}{time_str}")
            return uid
        except Exception as e:
            print(f"创建日历事件失败: {e}")
            return None

    def delete_caldav_event(self, uid: str) -> bool:
        """从 CalDAV 日历中删除事件"""
        if self.dry_run:
            print(f"  [DRY RUN] 会从日历删除事件: uid={uid}")
            return True

        try:
            cal = self._get_caldav_calendar()
            if not cal:
                return False

            # 通过 UID 查找事件
            events = cal.events()
            for event in events:
                if uid in event.data:
                    event.delete()
                    print(f"从日历删除事件成功: uid={uid}")
                    return True
            print(f"未找到要删除的日历事件: uid={uid}")
            return False
        except Exception as e:
            print(f"删除日历事件失败: {e}")
            return False

    def update_caldav_event(self, uid: str, title: str, due_date: str, due_time: str) -> Optional[str]:
        """更新 CalDAV 日历中的事件（删除旧的，创建新的），返回新 UID"""
        if self.dry_run:
            print(f"  [DRY RUN] 会更新日历事件: {title} {due_date} {due_time}")
            return f"dry-run-{uuid.uuid4()}"

        # 先删除旧事件
        if not self.delete_caldav_event(uid):
            print(f"更新失败：无法删除旧事件 uid={uid}")
            return None

        # 创建新事件
        new_uid = self.create_caldav_event(title, due_date, due_time)
        return new_uid

    def sync_local_todos_to_calendar(self):
        """将本地待办同步到日历"""
        created_count = 0
        updated_count = 0

        # 获取日历中所有 UID（用于检测删除）
        cal_events = self.fetch_aliyun_calendar_events()
        cal_uids = {e["uid"] for e in cal_events if e.get("uid")}

        # 本地待办 ID 集合
        local_todo_ids = set()
        need_save = False

        for todo in self._local_todos():
            todo_id = todo.get("id")
            local_todo_ids.add(todo_id)

            title = todo.get("title", "")
            due_date = todo.get("dueDate", "")
            due_time = todo.get("dueTime", "")

            # 没有截止时间的待办跳过
            if not due_date:
                continue

            # 检查是否已经有映射
            existing_mapping = self._todo_to_uid_map.get(todo_id)

            if existing_mapping:
                # 已有映射，检查是否需要更新
                old_uid = existing_mapping.get("uid")
                old_title = existing_mapping.get("title", "")
                old_due_date = existing_mapping.get("due_date", "")
                old_due_time = existing_mapping.get("due_time", "")

                # 检查标题、日期或时间是否变化
                if title != old_title or due_date != old_due_date or due_time != old_due_time:
                    # 有变化，更新事件
                    new_uid = self.update_caldav_event(old_uid, title, due_date, due_time)
                    if new_uid:
                        # 更新映射
                        self._todo_to_uid_map[todo_id] = {
                            "uid": new_uid,
                            "title": title,
                            "due_date": due_date,
                            "due_time": due_time
                        }
                        updated_count += 1
                        need_save = True
            else:
                # 没有映射，创建新事件
                uid = self.create_caldav_event(title, due_date, due_time)
                if uid:
                    self._todo_to_uid_map[todo_id] = {
                        "uid": uid,
                        "title": title,
                        "due_date": due_date,
                        "due_time": due_time
                    }
                    created_count += 1
                    need_save = True

        # 保存映射
        if need_save and not self.dry_run:
            self._save_mapping()

        print(f"本地待办同步到日历完成，新增 {created_count}，更新 {updated_count} 个事件")

    def run(self):
        """运行完整同步流程"""
        print("=" * 50)
        print(f"开始双向同步 时间: {datetime.datetime.now()}")
        print("=" * 50)

        # 1. 获取现有待办
        self.get_existing_todos()

        # 2. 将过期的日历待办标记为完成
        print("\n步骤1: 标记过期日程为完成...")
        self.complete_expired_calendar_todos()

        # 3. 获取邮箱日程
        print("\n步骤2: 获取邮箱日程...")
        events = self.fetch_aliyun_calendar_events()

        # 4. 日历 → 便利贴 同步
        if events:
            print("\n步骤3: 日历 → 便利贴 同步...")
            self.sync_new_events(events)

        # 5. 便利贴 → 日历 同步
        print("\n步骤4: 便利贴 → 日历 同步...")
        self.sync_local_todos_to_calendar()

        print("\n" + "=" * 50)
        print("双向同步完成!")
        print("=" * 50)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="双向同步邮箱日历和Zectrix待办事项")
    parser.add_argument("--dry-run", action="store_true",
                        help="模拟运行，不实际执行写入操作")
    args = parser.parse_args()

    syncer = CalendarSyncer(dry_run=args.dry_run)
    if args.dry_run:
        print("***** DRY RUN 模式 - 不会执行任何写入操作 *****")
    syncer.run()
