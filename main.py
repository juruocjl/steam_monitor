import os
import sqlite3
import threading
import asyncio
from flask import Flask, jsonify
from dotenv import load_dotenv
import steam  # 注意：这必须是通过 uv add steamio 安装的

# 1. 加载配置
load_dotenv()
STEAM_USER = os.getenv("STEAM_USERNAME")
STEAM_PASS = os.getenv("STEAM_PASSWORD")

# 2. 全局数据存储
friends_cache = {}
DB_NAME = "steam_status.db"

# ==========================================
# Flask Web 服务 (独立运行)
# ==========================================
app = Flask(__name__)

@app.route('/api/friends', methods=['GET'])
def get_friends():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    result = []
    for item in friends_cache.values():
        data = dict(item)
        appid = str(data.get('game_appid') or '')
        if appid:
            c.execute('SELECT app_name, app_logo FROM app_name_map WHERE appid = ?', (appid,))
            row = c.fetchone()
            data['game_name'] = (row[0] if row and row[0] else '')
            data['game_logo'] = (row[1] if row and row[1] else '')
        else:
            data['game_name'] = ''
            data['game_logo'] = ''
        result.append(data)
    conn.close()
    return jsonify({"status": "success", "data": result})

def run_flask():
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)

# ==========================================
# 核心逻辑：继承 steam.Client (官方 Example 范式)
# ==========================================
class SteamMonitor(steam.Client):
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._app_meta_cooldown_seconds = 20
        self._app_meta_fetch_lock = asyncio.Lock()
        self.init_db()

    def init_db(self):
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS status_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        steam_id TEXT,
                        name TEXT,
                        state TEXT,
                        game_appid TEXT,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                    )''')
        c.execute('''CREATE TABLE IF NOT EXISTS app_name_map (
                        appid TEXT PRIMARY KEY,
                        app_name TEXT,
                        app_logo TEXT,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )''')
        conn.commit()
        conn.close()

    def get_db_app_meta(self, appid: str):
        """返回 (app_name, app_logo)。"""
        if not appid:
            return '', ''
        try:
            conn = sqlite3.connect(DB_NAME)
            c = conn.cursor()
            c.execute('SELECT app_name, app_logo FROM app_name_map WHERE appid = ?', (appid,))
            row = c.fetchone()
            conn.close()
            if row and (row[0] or row[1]):
                app_name = row[0] or ''
                app_logo = row[1] or ''
                return app_name, app_logo
        except Exception as e:
            print(f"❌ AppName Cache Read Error: {e}")
        return '', ''

    def save_app_meta(self, appid: str, app_name: str, app_logo: str):
        if not appid or (not app_name and not app_logo):
            return
        try:
            conn = sqlite3.connect(DB_NAME)
            c = conn.cursor()
            c.execute(
                '''INSERT INTO app_name_map (appid, app_name, app_logo, updated_at)
                   VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(appid) DO UPDATE SET
                     app_name = excluded.app_name,
                     app_logo = excluded.app_logo,
                     updated_at = CURRENT_TIMESTAMP''',
                (appid, app_name, app_logo),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"❌ AppName Cache Write Error: {e}")

    async def resolve_app_meta(self, appid: str):
        """异步通过 client.fetch_app 解析 app 元数据，并回填内存缓存/API 数据。"""
        if not appid:
            return

        try:
            cached_name, cached_logo = self.get_db_app_meta(appid)
            if cached_name or cached_logo:
                return

            async with self._app_meta_fetch_lock:
                # 双检，避免并发任务重复抓同一个 app
                cached_name, cached_logo = self.get_db_app_meta(appid)
                if cached_name or cached_logo:
                    return

                # 不同 app 的请求间隔冷却，降低触发风控概率
                await asyncio.sleep(self._app_meta_cooldown_seconds)
                fetch_target = int(appid) if str(appid).isdigit() else appid
                fetched_app: steam.FetchedApp = await self.fetch_app(fetch_target)
            if fetched_app is None:
                return

            app_name = str(getattr(fetched_app, 'name', '') or '')
            logo_obj = (
                getattr(fetched_app, 'logo', None)
                or getattr(fetched_app, 'logo_url', None)
                or getattr(fetched_app, 'header_image', None)
                or getattr(fetched_app, 'icon_url', None)
            )
            if isinstance(logo_obj, str):
                app_logo = logo_obj
            elif logo_obj is not None and hasattr(logo_obj, 'url'):
                app_logo = str(getattr(logo_obj, 'url') or '')
            else:
                app_logo = ''

            if not app_name and not app_logo:
                return

            self.save_app_meta(appid, app_name, app_logo)
        except Exception as e:
            print(f"⚠️ fetch_app 解析失败 appid={appid}: {e}")

    def log_to_db(self, data):
        try:
            conn = sqlite3.connect(DB_NAME)
            c = conn.cursor()
            c.execute('''INSERT INTO status_log 
                         (steam_id, name, state, game_appid) 
                         VALUES (?, ?, ?, ?)''',
                      (data['steam_id'], data['name'], data['state'], 
                       data['game_appid']))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"❌ DB Error: {e}")

    def _debug_dump_user(self, user: steam.User):
        """调试用：尽可能完整打印 User 信息，便于确认字段结构。"""
        print("\n========== [DEBUG] User Full Dump ==========")
        print(f"repr: {user!r}")

        # 优先打印 __dict__（如果存在）
        raw_dict = getattr(user, '__dict__', None)
        if isinstance(raw_dict, dict):
            print("__dict__:")
            for k in sorted(raw_dict.keys()):
                print(f"  - {k}: {raw_dict[k]!r}")

        # 兜底：遍历属性，过滤私有和可调用对象
        print("attributes:")
        for attr in sorted(dir(user)):
            if attr.startswith('_'):
                continue
            try:
                value = getattr(user, attr)
            except Exception as e:
                print(f"  - {attr}: <error: {e}>")
                continue
            if callable(value):
                continue
            print(f"  - {attr}: {value!r}")

        print("===========================================\n")

    def parse_user_to_dict(self, user: steam.User):
        rp = getattr(user, 'rich_presence', None)
        if rp is None:
            rp = {}

        self._debug_dump_user(user)

        app = getattr(user, 'app', None)
        game_appid = (
            getattr(user, 'game_appid', None)
            or getattr(user, 'game_id', None)
            or getattr(user, 'app_id', None)
            or getattr(app, 'id', None)
            or ''
        )
        game_name = (
            getattr(user, 'game_name', None)
            or getattr(user, 'current_game_name', None)
            or getattr(app, 'name', None)
            or ''
        )

        if game_appid:
            cached_name, cached_logo = self.get_db_app_meta(str(game_appid))
            if (not game_name and not cached_name) or (not cached_logo):
                try:
                    asyncio.get_running_loop().create_task(self.resolve_app_meta(str(game_appid)))
                except RuntimeError:
                    pass

        state_obj = getattr(user, 'state', None)
        if state_obj is None:
            state_text = "unknown"
        elif hasattr(state_obj, 'name'):
            state_text = str(state_obj.name).lower()
        else:
            state_text = str(state_obj)

        rich_display = rp if isinstance(rp, dict) else {}
        
        return {
            "steam_id": str(getattr(user, 'id64', getattr(user, 'id', ''))),
            "name": getattr(user, 'name', '') or '',
            "state": state_text,
            "game_appid": str(game_appid),
            "rich_display": rich_display,
        }

    # --- 事件监听 ---
    
    async def on_ready(self):
        print(f"\n--- ✅ 登录成功！账号: {self.user.name} ---")
        
        # ✅ 修复：改用 steam.FriendRelationship.Friend
        # 使用 getattr 获取 relationship，因为 self.user 没有这个属性
        friends = [u for u in self.users]
        
        for friend in friends:
            data = self.parse_user_to_dict(friend)
            friends_cache[data['steam_id']] = data
        
        print(f"👥 正在监视 {len(friends)} 名好友数据。")
        
        # 启动 Flask 
        threading.Thread(target=run_flask, daemon=True).start()
        print("🌐 API 已就绪: http://localhost:5000/api/friends\n")

    async def on_user_update(self, before, after):
        """当好友状态变动时触发"""
        # ✅ 修复：排除机器人自己 (self.user)
        if after.id64 == self.user.id64:
            return

        new_data = self.parse_user_to_dict(after)
        old_data = friends_cache.get(new_data['steam_id'], {})

        # 变动检测逻辑
        if (new_data['state'] != old_data.get('state') or 
            new_data['game_appid'] != old_data.get('game_appid') or 
            new_data['rich_display'] != old_data.get('rich_display')):
            
            friends_cache[new_data['steam_id']] = new_data
            self.log_to_db(new_data)

            game_name, _ = self.get_db_app_meta(new_data['game_appid'])
            
            game_msg = f" | 🎮 {game_name}" if game_name else ""
            rp_msg = f" ({new_data['rich_display']})" if new_data['rich_display'] else ""
            print(f"✨ [变动] {new_data['name']} -> {new_data['state']}{game_msg}{rp_msg}")

    async def on_invite(self, invite):
        """自动通过好友邀请（仅 UserInvite）。"""
        if isinstance(invite, steam.UserInvite):
            try:
                await invite.accept()
                inviter = getattr(invite, 'author', None)
                inviter_name = getattr(inviter, 'name', 'unknown') if inviter else 'unknown'
                print(f"✅ 已自动通过好友请求: {inviter_name}")
            except Exception as e:
                print(f"❌ 自动通过好友请求失败: {e}")

# ==========================================
# 启动
# ==========================================
if __name__ == "__main__":
    # 根据 steamio 官方 Example 的启动方式
    client = SteamMonitor()
    try:
        # 这个 run 会阻塞当前线程，并自动处理 asyncio 循环
        client.run(STEAM_USER, STEAM_PASS)
    except KeyboardInterrupt:
        print("\n🛑 程序已手动停止。")