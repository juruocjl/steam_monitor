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
    return jsonify({"status": "success", "data": list(friends_cache.values())})

def run_flask():
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)

# ==========================================
# 核心逻辑：继承 steam.Client (官方 Example 范式)
# ==========================================
class SteamMonitor(steam.Client):
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
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
                        game_name TEXT,
                        rich_display TEXT,
                        party_id TEXT,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                    )''')
        conn.commit()
        conn.close()

    def log_to_db(self, data):
        try:
            conn = sqlite3.connect(DB_NAME)
            c = conn.cursor()
            c.execute('''INSERT INTO status_log 
                         (steam_id, name, state, game_appid, game_name, rich_display, party_id) 
                         VALUES (?, ?, ?, ?, ?, ?, ?)''',
                      (data['steam_id'], data['name'], data['state'], 
                       data['game_appid'], data['game_name'], 
                       data['rich_display'], data['party_id']))
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

        state_obj = getattr(user, 'state', None)
        if state_obj is None:
            state_text = "unknown"
        elif hasattr(state_obj, 'name'):
            state_text = str(state_obj.name).lower()
        else:
            state_text = str(state_obj)

        rich_display = ""
        if isinstance(rp, dict):
            rich_display = (
                rp.get('status')
                or rp.get('steam_display')
                or rp.get('display')
                or ''
            )
        elif hasattr(rp, 'get'):
            rich_display = (
                rp.get('status')
                or rp.get('steam_display')
                or rp.get('display')
                or ''
            )

        party_id = ""
        if isinstance(rp, dict):
            party = rp.get('party') or {}
            if isinstance(party, dict):
                party_id = str(party.get('id') or '')
        elif hasattr(rp, 'get'):
            party = rp.get('party') or {}
            if isinstance(party, dict):
                party_id = str(party.get('id') or '')
        
        return {
            "steam_id": str(getattr(user, 'id64', getattr(user, 'id', ''))),
            "name": getattr(user, 'name', '') or '',
            "state": state_text,
            "game_appid": str(game_appid),
            "game_name": str(game_name),
            "rich_display": rich_display,
            "party_id": party_id,
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
            
            game_msg = f" | 🎮 {new_data['game_name']}" if new_data['game_name'] else ""
            rp_msg = f" ({new_data['rich_display']})" if new_data['rich_display'] else ""
            print(f"✨ [变动] {new_data['name']} -> {new_data['state']}{game_msg}{rp_msg}")

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